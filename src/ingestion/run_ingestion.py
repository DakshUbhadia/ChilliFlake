import argparse
import datetime
import logging
import os
import shutil
import sys
import time
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
from db.connection import get_connection
from src.project_config import get_project
from src.ingestion.github_client import download_artifact_zip, list_artifacts, list_workflow_runs, make_session
from src.ingestion.report_parser import extract_xml_files_from_zip, ingest_run
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(name)s -- %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
logger = logging.getLogger('chilliflake.ingestion')

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='ChilliFlake ingestion: pull CI run reports into SQLite.')
    parser.add_argument('--runs', type=int, default=20, metavar='N', help='Max workflow runs to fetch (default: 20).')
    parser.add_argument('--repo', default=None, help='owner/name; defaults to the configured project repository.')
    parser.add_argument('--project', default=None, help='Configured project key (default: home-assistant-core).')
    parser.add_argument('--workflow', default=None, help='Comma-separated workflow name(s); defaults to project config.')
    parser.add_argument('--branch', default=None, help='Branch filter. Defaults to the project branch; pass an empty string to include all branches.')
    parser.add_argument('--created-after', default=None, metavar='DATE', help="ISO date (e.g. '2026-08-01'). Defaults to 7 days ago. 'all' disables the filter.")
    parser.add_argument('--artifact-name', default=None, help='Artifact name prefix; defaults to project config.')
    parser.add_argument('--db', default=None, help='SQLite DB path (overrides DB_PATH env var).')
    return parser.parse_args()

def _cleanup(zip_paths: list[Path], temp_dirs: list[Path]) -> None:
    for p in zip_paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug('Could not delete ZIP %s: %s', p, exc)
    for d in temp_dirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except OSError as exc:
            logger.debug('Could not delete temp dir %s: %s', d, exc)

def _already_ingested(conn, github_run_id: int) -> bool:
    row = conn.execute(
        'SELECT 1 FROM builds b JOIN test_runs tr ON tr.build_id = b.id WHERE b.github_run_id = ? LIMIT 1',
        (github_run_id,),
    ).fetchone()
    return row is not None

def main() -> None:
    args = _parse_args()
    project_name, project_settings = get_project(getattr(args, 'project', None), args.repo)
    repo = args.repo or project_settings.get('repo')
    if not repo:
        logger.error('Project %s has no repository configured. Pass --repo owner/name.', project_name)
        sys.exit(2)
    workflow = args.workflow or project_settings.get('workflow')
    artifact_name = args.artifact_name or project_settings.get('artifact_prefix')
    if not workflow or not artifact_name:
        logger.error('Project %s has no confirmed workflow/artifact prefix. Pass --workflow and --artifact-name explicitly after checking live artifacts.', project_name)
        sys.exit(2)
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        logger.warning('GITHUB_TOKEN is not set; using lower-rate anonymous access (public repositories only).')
    workflow_names = [w.strip() for w in workflow.split(',') if w.strip()]
    if args.created_after is None:
        created_after: str | None = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    elif args.created_after.lower() == 'all':
        created_after = None
    else:
        created_after = args.created_after
    branch_arg = project_settings.get('branch', '') if args.branch is None else args.branch
    branch_filter: str | None = branch_arg.strip() or None
    logger.info('Repo=%s Workflows=%s Branch=%s CreatedAfter=%s MaxRuns=%d ArtifactPrefix=%s', repo, workflow_names, branch_filter or '(all)', created_after or '(all time)', args.runs, artifact_name)
    start_time = time.monotonic()
    conn = get_connection(args.db)
    try:
        session = make_session(token)
        build_id_cache: dict[int, int] = {}
        test_id_cache: dict[tuple[str, str, str], int] = {}
        runs = list_workflow_runs(session, repo=repo, workflow_names=workflow_names, branch=branch_filter, max_runs=args.runs, created_after=created_after)
        logger.info('Fetched %d run(s) to process.', len(runs))
        total_builds_inserted = total_test_runs_inserted = 0
        total_skipped_bad_status = total_files_parsed = total_files_skipped = 0
        total_runs_errored = total_runs_no_artifact = total_already_ingested = 0
        artifact_counts_per_run: list[int] = []
        for run in runs:
            run_id: int = run['id']
            commit_sha: str = run.get('head_sha', 'unknown')[:40]
            branch: str | None = run.get('head_branch')
            created_at: str = run.get('created_at', '')
            logger.info('Processing run %d (%s) @ %s ...', run_id, run.get('name', '?'), created_at)

            if _already_ingested(conn, run_id):
                logger.info('Run %d: already ingested -- skipping (no API calls spent).', run_id)
                total_already_ingested += 1
                continue

            target_artifacts = list_artifacts(session, repo=repo, run_id=run_id, name_prefix=artifact_name)
            if not target_artifacts:
                logger.warning('Run %d: no artifact matching prefix %r. Skipping.', run_id, artifact_name)
                total_runs_no_artifact += 1
                continue
            artifact_counts_per_run.append(len(target_artifacts))
            logger.info('Run %d: found %d artifact(s) to merge.', run_id, len(target_artifacts))
            all_xml_paths: list[Path] = []
            temp_dirs: list[Path] = []
            zip_paths: list[Path] = []
            download_errors = 0
            for artifact in target_artifacts:
                artifact_id: int = artifact['id']
                try:
                    zip_path = download_artifact_zip(session, artifact_id, repo=repo)
                    zip_paths.append(zip_path)
                    temp_dir, xml_paths = extract_xml_files_from_zip(zip_path)
                    temp_dirs.append(temp_dir)
                    all_xml_paths.extend(xml_paths)
                except Exception as exc:
                    logger.error('Run %d | artifact %s: download/extract failed: %s', run_id, artifact.get('name', artifact_id), exc)
                    download_errors += 1
            if download_errors:
                logger.error('Run %d: %d artifact download(s) failed; not ingesting a partial run.', run_id, download_errors)
                total_runs_errored += 1
                _cleanup(zip_paths, temp_dirs)
                continue
            if not all_xml_paths:
                logger.warning('Run %d: no XML extracted from %d artifact(s). Skipping.', run_id, len(target_artifacts))
                total_runs_no_artifact += 1
                _cleanup(zip_paths, temp_dirs)
                continue
            logger.info('Run %d: %d XML file(s) across %d artifact(s), %d download error(s).', run_id, len(all_xml_paths), len(target_artifacts), download_errors)
            run_meta = {'github_run_id': run_id, 'commit_sha': commit_sha, 'branch': branch, 'created_at': created_at, 'repo': repo}
            try:
                counts = ingest_run(conn, run_meta, all_xml_paths, test_id_cache, build_id_cache, project=project_name)
            except Exception as exc:
                logger.error('Run %d: ingest failed: %s', run_id, exc)
                total_runs_errored += 1
                continue
            finally:
                _cleanup(zip_paths, temp_dirs)
            if counts['already_ingested']:
                total_already_ingested += 1
            else:
                total_builds_inserted += 1
            total_test_runs_inserted += counts['inserted']
            total_skipped_bad_status += counts['skipped_bad_status']
            total_files_parsed += counts['files_parsed']
            total_files_skipped += counts['files_skipped']
        elapsed = time.monotonic() - start_time
        if artifact_counts_per_run:
            art_min, art_max = (min(artifact_counts_per_run), max(artifact_counts_per_run))
            art_avg = sum(artifact_counts_per_run) / len(artifact_counts_per_run)
        else:
            art_min = art_max = art_avg = 0
        logger.info('=' * 60)
        logger.info('INGESTION COMPLETE')
        logger.info('  Runs fetched=%d  no_artifact=%d  already_ingested=%d  errored=%d', len(runs), total_runs_no_artifact, total_already_ingested, total_runs_errored)
        logger.info('  Builds newly ingested=%d  test_runs inserted=%d', total_builds_inserted, total_test_runs_inserted)
        logger.info('  XML files parsed=%d  skipped=%d  bad-status rows=%d', total_files_parsed, total_files_skipped, total_skipped_bad_status)
        logger.info('  Artifacts/run -- min=%d max=%d avg=%.1f', art_min, art_max, art_avg)
        logger.info('  Elapsed=%.1fs', elapsed)
        logger.info('=' * 60)
        row = conn.execute('SELECT (SELECT COUNT(*) FROM builds) AS builds, (SELECT COUNT(*) FROM tests) AS tests, (SELECT COUNT(*) FROM test_runs) AS test_runs').fetchone()
        logger.info('DB totals -- builds=%d tests=%d test_runs=%d', row['builds'], row['tests'], row['test_runs'])
        if total_runs_errored:
            logger.error('%d run(s) failed; refusing to report a successful ingestion.', total_runs_errored)
            raise SystemExit(1)
    finally:
        conn.close()

if __name__ == '__main__':
    main()
