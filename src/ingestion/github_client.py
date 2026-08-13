"""
github_client.py — GitHub REST API client for ChilliFlake ingestion.

Responsibilities:
  - List completed workflow runs for a given repo + workflow name(s), paginated.
  - List artifacts for a given run ID.
  - Stream-download an artifact ZIP to a temp file.
  - Rate-limit awareness: sleep when X-RateLimit-Remaining < RATE_LIMIT_BUFFER.
  - Exponential backoff on 403/429 responses.
  - Wrap all network calls; log and re-raise on non-retryable errors.

No database code lives here. All functions accept a requests.Session as their
first argument so callers and tests can inject a pre-configured (or mocked) session.
"""

import logging
import math
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

# If remaining rate-limit calls drop below this, sleep until reset.
RATE_LIMIT_BUFFER = 50

# Backoff: retry up to MAX_RETRIES times on 403/429, doubling each time,
# capped at MAX_BACKOFF_SECONDS.
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 64


def make_session(token: str) -> requests.Session:
    """
    Build a requests.Session pre-configured with authentication headers.

    Args:
        token: GitHub Personal Access Token (public_repo scope required).

    Returns:
        Configured requests.Session.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
    )
    return session


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_rate_limit(response: requests.Response) -> None:
    """
    Inspect rate-limit headers on *response*. If remaining calls are below
    RATE_LIMIT_BUFFER, sleep until the reset timestamp.
    """
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    if remaining is None:
        return
    try:
        remaining_int = int(remaining)
    except ValueError:
        return

    if remaining_int < RATE_LIMIT_BUFFER and reset:
        try:
            sleep_seconds = max(0, int(reset) - int(time.time())) + 1
            logger.warning(
                "Rate limit low (%d remaining). Sleeping %ds until reset.",
                remaining_int,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
        except (ValueError, TypeError):
            pass


def _get_with_backoff(
    session: requests.Session,
    url: str,
    **kwargs: Any,
) -> requests.Response:
    """
    GET *url* with exponential backoff on 403/429, up to MAX_RETRIES attempts.

    Raises:
        requests.HTTPError: after all retries are exhausted or on non-retryable
            HTTP errors.
        requests.exceptions.RequestException: on network-level failures.
    """
    backoff = 1
    for attempt in range(1, MAX_RETRIES + 2):  # +1 so last attempt can raise
        try:
            response = session.get(url, **kwargs)
        except requests.exceptions.RequestException as exc:
            logger.error("Network error on GET %s: %s", url, exc)
            raise

        _check_rate_limit(response)

        if response.status_code in (403, 429):
            if attempt > MAX_RETRIES:
                logger.error(
                    "Giving up on GET %s after %d retries (status %d).",
                    url,
                    MAX_RETRIES,
                    response.status_code,
                )
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            if retry_after:
                wait = int(retry_after)
            else:
                wait = min(backoff, MAX_BACKOFF_SECONDS)

            logger.warning(
                "HTTP %d on GET %s. Retrying in %ds (attempt %d/%d).",
                response.status_code,
                url,
                wait,
                attempt,
                MAX_RETRIES,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue

        response.raise_for_status()
        return response

    # Should never reach here, but satisfy type checkers.
    raise RuntimeError("Unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_workflow_runs(
    session: requests.Session,
    repo: str,
    workflow_names: list[str],
    branch: str = "main",
    max_runs: int = 20,
) -> list[dict[str, Any]]:
    """
    Return up to *max_runs* completed workflow runs for *repo* whose
    workflow name is in *workflow_names*, from newest to oldest.

    GitHub caps results at 100 per page; this function handles pagination
    automatically.

    Args:
        session:        Authenticated requests.Session (from make_session()).
        repo:           'owner/repo' string, e.g. 'DakshUbhadia/ChilliFlake'.
        workflow_names: List of workflow display names to include.
                        Case-sensitive. E.g. ['ChilliFlake CI'].
        branch:         Branch to filter on (default 'main').
        max_runs:       Stop collecting after this many matching runs.

    Returns:
        List of workflow run dicts (GitHub API shape).
    """
    url = f"{GITHUB_API_BASE}/repos/{repo}/actions/runs"
    params: dict[str, Any] = {
        "branch": branch,
        "status": "completed",
        "per_page": 100,
        "page": 1,
    }

    collected: list[dict[str, Any]] = []
    workflow_name_set = set(workflow_names)

    while len(collected) < max_runs:
        logger.debug("Fetching workflow runs page %d for %s", params["page"], repo)
        try:
            response = _get_with_backoff(session, url, params=params)
        except requests.exceptions.RequestException as exc:
            logger.error("Failed to list workflow runs for %s: %s", repo, exc)
            break

        data = response.json()
        runs = data.get("workflow_runs", [])
        if not runs:
            break  # No more pages.

        for run in runs:
            if len(collected) >= max_runs:
                break
            if run.get("name") in workflow_name_set:
                collected.append(run)

        # Pagination: if fewer than 100 were returned, we're on the last page.
        if len(runs) < 100:
            break
        params["page"] += 1

    logger.info(
        "Found %d matching run(s) for %s (workflows: %s, branch: %s).",
        len(collected),
        repo,
        workflow_names,
        branch,
    )
    return collected


def list_artifacts(
    session: requests.Session,
    repo: str,
    run_id: int,
) -> list[dict[str, Any]]:
    """
    Return metadata for all artifacts attached to *run_id*.

    Args:
        session: Authenticated requests.Session.
        repo:    'owner/repo' string.
        run_id:  GitHub Actions run ID (integer).

    Returns:
        List of artifact metadata dicts. Empty list if the run has no
        artifacts or if the request fails.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo}/actions/runs/{run_id}/artifacts"
    try:
        response = _get_with_backoff(session, url, params={"per_page": 100})
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to list artifacts for run %d: %s", run_id, exc)
        return []

    data = response.json()
    artifacts = data.get("artifacts", [])
    logger.debug("Run %d has %d artifact(s).", run_id, len(artifacts))
    return artifacts


def download_artifact_zip(
    session: requests.Session,
    artifact_id: int,
    repo: str,
    dest_dir: str | None = None,
) -> Path:
    """
    Stream-download the ZIP for *artifact_id* to a temporary file.

    The artifact download endpoint issues a redirect to the actual ZIP URL.
    requests follows the redirect automatically; we stream the response to
    avoid loading potentially tens of MB into memory.

    Args:
        session:     Authenticated requests.Session.
        artifact_id: GitHub artifact ID.
        repo:        'owner/repo' string.
        dest_dir:    Directory for the temp file. Defaults to the system
                     temp directory.

    Returns:
        Path to the downloaded .zip file. Caller is responsible for cleanup.

    Raises:
        requests.exceptions.RequestException: on network failure.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    logger.info("Downloading artifact %d from %s ...", artifact_id, repo)

    try:
        response = _get_with_backoff(session, url, stream=True)
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to download artifact %d: %s", artifact_id, exc)
        raise

    suffix = f"_artifact_{artifact_id}.zip"
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix, dir=dest_dir
    )
    bytes_written = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
                bytes_written += len(chunk)
    finally:
        tmp.close()

    logger.info(
        "Artifact %d downloaded to %s (%.1f KB).",
        artifact_id,
        tmp.name,
        bytes_written / 1024,
    )
    return Path(tmp.name)
