"""
run_ingestion.py — Orchestration entry point for ChilliFlake ingestion.

Usage:
    python -m src.ingestion.run_ingestion [options]

Options:
    --runs N          Number of workflow runs to fetch (default: 20).
    --repo OWNER/NAME GitHub repository to pull from
                      (default: DakshUbhadia/ChilliFlake).
    --workflow NAME   Comma-separated workflow display names to filter on
                      (default: 'ChilliFlake CI').
    --branch BRANCH   Branch to filter on (default: main).
    --db PATH         Path to SQLite database (overrides DB_PATH env var).
    --artifact-name N Name prefix of the artifact to download
                      (default: 'pytest-json-report').

Environment variables (read from .env if python-dotenv is installed):
    GITHUB_TOKEN      Required. PAT with public_repo scope.
    DB_PATH           Optional. Defaults to db/chilliflake.db.

Re-running the script on overlapping data is safe: already-ingested builds
are skipped (idempotent via INSERT OR IGNORE + cache pattern).
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Load .env if python-dotenv is available (optional dependency).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # Fine — user can export vars manually.

# ---------------------------------------------------------------------------
# Bootstrap path so the script can be run from the project root as a module:
#   python -m src.ingestion.run_ingestion
# ---------------------------------------------------------------------------
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from db.connection import get_connection                        # noqa: E402
from src.ingestion.github_client import (                      # noqa: E402
    download_artifact_zip,
    list_artifacts,
    list_workflow_runs,
    make_session,
)
from src.ingestion.report_parser import extract_json_from_zip, ingest_run  # noqa: E402

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


logger = logging.getLogger("chilliflake.ingestion")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ChilliFlake ingestion: pull CI run reports into SQLite."
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=20,
        metavar="N",
        help="Max number of workflow runs to fetch (default: 20).",
    )
    parser.add_argument(
        "--repo",
        default="DakshUbhadia/ChilliFlake",
        help="GitHub repo in owner/name format (default: DakshUbhadia/ChilliFlake).",
    )
    parser.add_argument(
        "--workflow",
        default="ChilliFlake CI",
        help="Comma-separated workflow name(s) to filter on (default: 'ChilliFlake CI').",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Branch to filter on (default: main).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite DB path (overrides DB_PATH env var).",
    )
    parser.add_argument(
        "--artifact-name",
        default="pytest-json-report",
        help="Artifact name prefix to look for (default: pytest-json-report).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _setup_logging()
    args = _parse_args()

    # --- Token: fail fast if missing ---
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.error(
            "GITHUB_TOKEN environment variable is not set. "
            "Create a PAT with 'public_repo' scope and add it to your .env file. "
            "See .env.example for instructions."
        )
        sys.exit(1)

    workflow_names = [w.strip() for w in args.workflow.split(",") if w.strip()]
    logger.info("Starting ingestion run.")
    logger.info("  Repo:      %s", args.repo)
    logger.info("  Workflows: %s", workflow_names)
    logger.info("  Branch:    %s", args.branch)
    logger.info("  Max runs:  %d", args.runs)

    start_time = time.monotonic()

    # --- DB connection ---
    conn = get_connection(args.db)
    logger.info("Database ready.")

    # --- GitHub session ---
    session = make_session(token)

    # --- Shared caches (span the whole script run for efficiency) ---
    build_id_cache: dict[int, int] = {}
    test_id_cache: dict[tuple, int] = {}

    # --- Fetch run list ---
    runs = list_workflow_runs(
        session,
        repo=args.repo,
        workflow_names=workflow_names,
        branch=args.branch,
        max_runs=args.runs,
    )
    logger.info("Fetched %d run(s) to process.", len(runs))

    # --- Per-run counters ---
    total_builds_inserted = 0
    total_tests_seen = 0
    total_test_runs_inserted = 0
    total_skipped_unknown = 0
    total_skipped_ignored = 0
    total_runs_errored = 0
    total_runs_no_artifact = 0

    for run in runs:
        run_id: int = run["id"]
        run_name: str = run.get("name", "?")
        commit_sha: str = run.get("head_sha", "unknown")[:40]
        branch: str | None = run.get("head_branch")
        created_at: str = run.get("created_at", "")

        logger.info("Processing run %d (%s) @ %s ...", run_id, run_name, created_at)

        # --- Find the target artifact ---
        artifacts = list_artifacts(session, repo=args.repo, run_id=run_id)
        target_artifacts = [
            a for a in artifacts
            if not a.get("expired", False)
            and a.get("name", "").startswith(args.artifact_name)
        ]

        if not target_artifacts:
            logger.warning(
                "Run %d: no non-expired artifact matching '%s'. Skipping.",
                run_id,
                args.artifact_name,
            )
            total_runs_no_artifact += 1
            continue

        artifact = target_artifacts[0]
        artifact_id: int = artifact["id"]

        # --- Download ZIP ---
        zip_path = None
        try:
            zip_path = download_artifact_zip(session, artifact_id, repo=args.repo)
            report = extract_json_from_zip(zip_path)
        except Exception as exc:
            logger.error("Run %d: failed to get/parse report: %s", run_id, exc)
            total_runs_errored += 1
            continue
        finally:
            if zip_path and zip_path.exists():
                zip_path.unlink(missing_ok=True)

        # --- Ingest ---
        run_meta = {
            "github_run_id": run_id,
            "commit_sha": commit_sha,
            "branch": branch,
            "created_at": created_at,
        }

        # Track whether this build was new (pre-cache miss = first time seen).
        build_was_new = run_id not in build_id_cache

        try:
            counts = ingest_run(conn, run_meta, report, test_id_cache, build_id_cache)
        except Exception as exc:
            logger.error("Run %d: ingest failed: %s", run_id, exc)
            total_runs_errored += 1
            continue

        if build_was_new:
            total_builds_inserted += 1
        total_test_runs_inserted += counts["inserted"]
        total_skipped_unknown += counts["skipped_unknown_status"]
        total_skipped_ignored += counts["skipped_ignored_outcome"]
        total_tests_seen += counts["inserted"]  # approximate

    # --- Final summary ---
    elapsed = time.monotonic() - start_time
    logger.info("=" * 60)
    logger.info("INGESTION COMPLETE")
    logger.info("  Runs fetched:           %d", len(runs))
    logger.info("  Runs with no artifact:  %d", total_runs_no_artifact)
    logger.info("  Runs errored:           %d", total_runs_errored)
    logger.info("  Builds ingested:        %d", total_builds_inserted)
    logger.info("  test_runs inserted:     %d", total_test_runs_inserted)
    logger.info("  Rows skipped (status):  %d", total_skipped_unknown)
    logger.info("  Rows skipped (ignored): %d", total_skipped_ignored)
    logger.info("  Elapsed:                %.1fs", elapsed)
    logger.info("=" * 60)

    # Quick DB verification.
    row = conn.execute(
        "SELECT "
        "  (SELECT COUNT(*) FROM builds) AS builds,"
        "  (SELECT COUNT(*) FROM tests) AS tests,"
        "  (SELECT COUNT(*) FROM test_runs) AS test_runs"
    ).fetchone()
    logger.info(
        "DB totals — builds: %d, tests: %d, test_runs: %d",
        row["builds"],
        row["tests"],
        row["test_runs"],
    )
    conn.close()


if __name__ == "__main__":
    main()
