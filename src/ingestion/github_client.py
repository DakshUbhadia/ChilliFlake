import logging
import tempfile
import time
from pathlib import Path
from typing import Any
import requests
logger = logging.getLogger(__name__)
GITHUB_API_BASE = 'https://api.github.com'
GITHUB_API_VERSION = '2022-11-28'
RATE_LIMIT_BUFFER = 50
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 64

def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {token}', 'X-GitHub-Api-Version': GITHUB_API_VERSION})
    return session

def _check_rate_limit(response: requests.Response) -> None:
    remaining = response.headers.get('X-RateLimit-Remaining')
    reset = response.headers.get('X-RateLimit-Reset')
    if remaining is None:
        return
    try:
        remaining_int = int(remaining)
    except ValueError:
        return
    if remaining_int < RATE_LIMIT_BUFFER and reset:
        try:
            sleep_seconds = max(0, int(reset) - int(time.time())) + 1
            logger.warning('Rate limit low (%d left). Sleeping %ds.', remaining_int, sleep_seconds)
            time.sleep(sleep_seconds)
        except (ValueError, TypeError):
            pass

def _is_rate_limited(response: requests.Response) -> bool:
    if response.status_code == 429:
        return True
    remaining = response.headers.get('X-RateLimit-Remaining')
    return remaining == '0'

def _get_with_backoff(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    backoff = 1
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = session.get(url, **kwargs)
        except requests.exceptions.RequestException as exc:
            logger.error('Network error on GET %s: %s', url, exc)
            raise
        _check_rate_limit(response)
        if response.status_code in (403, 429) and _is_rate_limited(response):
            if attempt > MAX_RETRIES:
                logger.error('Giving up on GET %s after %d retries.', url, MAX_RETRIES)
                response.raise_for_status()
            wait = int(response.headers.get('Retry-After', min(backoff, MAX_BACKOFF_SECONDS)))
            logger.warning('Rate limited on %s. Retry in %ds (%d/%d).', url, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue
        if response.status_code == 403:
            logger.error('403 on %s — not rate-limiting, likely a bad token/scope: %s', url, response.text[:300])
        response.raise_for_status()
        return response
    raise RuntimeError('Unreachable')

def list_workflow_runs(session: requests.Session, repo: str, workflow_names: list[str], branch: str | None=None, max_runs: int=20, created_after: str | None=None) -> list[dict[str, Any]]:
    url = f'{GITHUB_API_BASE}/repos/{repo}/actions/runs'
    params: dict[str, Any] = {'status': 'completed', 'per_page': 100, 'page': 1}
    if branch:
        params['branch'] = branch
    if created_after:
        params['created'] = f'>{created_after}'
    collected: list[dict[str, Any]] = []
    workflow_name_set = set(workflow_names)
    while len(collected) < max_runs:
        try:
            response = _get_with_backoff(session, url, params=params)
        except requests.exceptions.RequestException as exc:
            logger.error('Failed to list workflow runs for %s: %s', repo, exc)
            break
        runs = response.json().get('workflow_runs', [])
        if not runs:
            break
        for run in runs:
            if len(collected) >= max_runs:
                break
            if run.get('name') in workflow_name_set:
                collected.append(run)
        if len(runs) < 100:
            break
        params['page'] += 1
    logger.info('Found %d matching run(s) for %s (workflows=%s, branch=%s, created_after=%s).', len(collected), repo, workflow_names, branch or '(all)', created_after or '(any)')
    return collected

def list_artifacts(session: requests.Session, repo: str, run_id: int, name_prefix: str='') -> list[dict[str, Any]]:
    url = f'{GITHUB_API_BASE}/repos/{repo}/actions/runs/{run_id}/artifacts'
    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            response = _get_with_backoff(session, url, params={'per_page': 100, 'page': page})
        except requests.exceptions.RequestException as exc:
            logger.error('Failed to list artifacts for run %d: %s', run_id, exc)
            break
        artifacts = response.json().get('artifacts', [])
        for artifact in artifacts:
            if artifact.get('expired', False):
                continue
            if name_prefix and (not artifact.get('name', '').startswith(name_prefix)):
                continue
            collected.append(artifact)
        if len(artifacts) < 100:
            break
        page += 1
    logger.debug('Run %d: %d artifact(s) matching prefix %r.', run_id, len(collected), name_prefix)
    return collected

def download_artifact_zip(session: requests.Session, artifact_id: int, repo: str, dest_dir: str | None=None) -> Path:
    url = f'{GITHUB_API_BASE}/repos/{repo}/actions/artifacts/{artifact_id}/zip'
    try:
        response = _get_with_backoff(session, url, stream=True)
    except requests.exceptions.RequestException as exc:
        logger.error('Failed to download artifact %d: %s', artifact_id, exc)
        raise
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f'_artifact_{artifact_id}.zip', dir=dest_dir)
    bytes_written = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
                bytes_written += len(chunk)
    finally:
        tmp.close()
    logger.info('Artifact %d downloaded (%.1f KB).', artifact_id, bytes_written / 1024)
    return Path(tmp.name)