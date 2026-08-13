"""
report_parser.py — Parse pytest-json-report ZIPs and load results into SQLite.

Responsibilities:
  - Extract the JSON report from a downloaded artifact ZIP.
  - Upsert builds and tests using an in-memory cache (INSERT OR IGNORE + SELECT).
  - Insert test_runs with correct status mapping and reported_flaky detection.
  - Validate status against the schema CHECK constraint before insert.
  - Wrap each run's inserts in a single transaction (commit-per-run).
  - Skip invalid rows with structured log output; never crash mid-batch.

No GitHub API calls are made from this file.
"""

import json
import logging
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema-valid status values (mirrors the CHECK constraint in schema.sql).
# ---------------------------------------------------------------------------
VALID_STATUSES = frozenset({"passed", "failed", "timedOut", "skipped", "interrupted"})

# pytest-json-report outcome → schema status.
# Outcomes not in this map are logged and skipped.
_OUTCOME_MAP: dict[str, str] = {
    "passed":  "passed",
    "failed":  "failed",
    "skipped": "skipped",
    "error":   "failed",   # treat collection/setup errors as failed
}

# Outcomes that are intentionally ignored (not schema violations — just not
# meaningful run results we want to store).
_IGNORED_OUTCOMES = frozenset({"xfailed", "xpassed"})


# ---------------------------------------------------------------------------
# ZIP extraction
# ---------------------------------------------------------------------------

def extract_json_from_zip(zip_path: str | Path) -> dict[str, Any]:
    """
    Open *zip_path* and return the first parseable pytest-json-report dict.

    Handles ZIPs with multiple files gracefully — tries each .json file in
    order and returns on the first successfully parsed report.  Warns if the
    ZIP contains unexpected structure.

    Args:
        zip_path: Path to the downloaded artifact ZIP.

    Returns:
        Parsed JSON report dict.

    Raises:
        ValueError:  if no valid JSON report is found in the ZIP.
        zipfile.BadZipFile: if the file is not a valid ZIP.
    """
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        logger.debug("ZIP %s contains %d file(s): %s", zip_path.name, len(names), names)

        json_names = [n for n in names if n.lower().endswith(".json")]
        if not json_names:
            raise ValueError(
                f"No .json file found in artifact ZIP {zip_path.name}. "
                f"Contents: {names}"
            )

        if len(json_names) > 1:
            logger.warning(
                "ZIP %s has %d JSON files; using the first: %s",
                zip_path.name,
                len(json_names),
                json_names,
            )

        for name in json_names:
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("Could not parse %s in ZIP: %s", name, exc)
                continue

            if "tests" not in data:
                logger.warning(
                    "%s does not look like a pytest-json-report (no 'tests' key). "
                    "Skipping.",
                    name,
                )
                continue

            logger.debug("Using JSON report: %s", name)
            return data

    raise ValueError(
        f"No valid pytest-json-report found in {zip_path.name}."
    )


# ---------------------------------------------------------------------------
# Cache helpers (INSERT OR IGNORE + SELECT pattern)
# ---------------------------------------------------------------------------

def _get_or_insert_build(
    conn: Any,
    github_run_id: int,
    commit_sha: str,
    branch: str | None,
    created_at: str,
    build_id_cache: dict[int, int],
) -> int:
    """
    Upsert a build row and return its integer primary-key id.

    Uses the cache to avoid a SELECT on every test row.  On a cache miss:
      1. INSERT OR IGNORE (no-op if run already exists).
      2. SELECT id WHERE github_run_id = ? (always succeeds whether inserted or not).
      3. Store in cache.
    """
    if github_run_id in build_id_cache:
        return build_id_cache[github_run_id]

    conn.execute(
        """
        INSERT OR IGNORE INTO builds (github_run_id, commit_sha, branch, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (github_run_id, commit_sha, branch, created_at),
    )
    row = conn.execute(
        "SELECT id FROM builds WHERE github_run_id = ?",
        (github_run_id,),
    ).fetchone()
    build_id = row["id"]
    build_id_cache[github_run_id] = build_id
    return build_id


def _get_or_insert_test(
    conn: Any,
    project: str,
    file_path: str | None,
    test_name: str,
    test_id_cache: dict[tuple[str, str | None, str], int],
) -> int:
    """
    Upsert a test row and return its integer primary-key id.

    Cache key: (project, file_path, test_name).
    Same INSERT OR IGNORE + SELECT pattern as _get_or_insert_build.
    """
    cache_key = (project, file_path, test_name)
    if cache_key in test_id_cache:
        return test_id_cache[cache_key]

    conn.execute(
        """
        INSERT OR IGNORE INTO tests (project, file_path, test_name)
        VALUES (?, ?, ?)
        """,
        (project, file_path, test_name),
    )
    row = conn.execute(
        """
        SELECT id FROM tests
        WHERE project = ? AND (file_path = ? OR (file_path IS NULL AND ? IS NULL))
          AND test_name = ?
        """,
        (project, file_path, file_path, test_name),
    ).fetchone()
    test_id = row["id"]
    test_id_cache[cache_key] = test_id
    return test_id


# ---------------------------------------------------------------------------
# nodeid parsing
# ---------------------------------------------------------------------------

def _parse_nodeid(nodeid: str) -> tuple[str, str | None, str]:
    """
    Split a pytest nodeid into (project, file_path, test_name).

    pytest nodeid format:  'path/to/test_file.py::TestClass::test_method'
    or simply:             'path/to/test_file.py::test_function'

    - project:   the top-level directory component (first path segment),
                 or 'unknown' if the nodeid has no path separator.
    - file_path: the .py file path component (before '::').
    - test_name: everything after the first '::'.

    Examples:
        'tests/fixtures/test_seeded.py::test_flaky'
          -> project='tests', file_path='tests/fixtures/test_seeded.py',
             test_name='test_flaky'
    """
    if "::" not in nodeid:
        return ("unknown", None, nodeid)

    file_part, _, test_part = nodeid.partition("::")
    # Project = first directory component of the path.
    project = Path(file_part).parts[0] if file_part else "unknown"
    return (project, file_part if file_part else None, test_part)


# ---------------------------------------------------------------------------
# Flakiness detection
# ---------------------------------------------------------------------------

def _detect_flakiness(
    tests: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Group duplicate nodeids to detect retry sequences and flakiness.

    A test is 'flaky' when it appears more than once in the report AND
    the last occurrence has outcome 'passed' (implying it passed after retries).

    Returns a dict keyed by nodeid with:
        outcome:         final (last) outcome string
        reported_flaky:  1 if flaky, else 0
        retries:         number of retries (len(occurrences) - 1)
        duration_ms:     total call duration in ms across all occurrences
    """
    # Group all occurrences by nodeid in order.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in tests:
        nodeid = t.get("nodeid", "")
        groups[nodeid].append(t)

    result: dict[str, dict[str, Any]] = {}
    for nodeid, occurrences in groups.items():
        final = occurrences[-1]
        final_outcome = final.get("outcome", "")
        retries = len(occurrences) - 1

        # Flaky: more than one occurrence AND last outcome is 'passed',
        # meaning it recovered from an earlier failure.
        prior_outcomes = [o.get("outcome", "") for o in occurrences[:-1]]
        is_flaky = retries > 0 and final_outcome == "passed" and "failed" in prior_outcomes

        # Duration: sum of call.duration across all occurrences (seconds -> ms).
        total_duration_s = sum(
            occ.get("call", {}).get("duration", 0.0) for occ in occurrences
        )
        duration_ms = int(round(total_duration_s * 1000))

        result[nodeid] = {
            "outcome": final_outcome,
            "reported_flaky": 1 if is_flaky else 0,
            "retries": retries,
            "duration_ms": duration_ms,
        }

    return result


# ---------------------------------------------------------------------------
# Main ingestion entry point
# ---------------------------------------------------------------------------

def ingest_run(
    conn: Any,
    run_meta: dict[str, Any],
    report: dict[str, Any],
    test_id_cache: dict[tuple[str, str | None, str], int],
    build_id_cache: dict[int, int],
) -> dict[str, int]:
    """
    Ingest one workflow run's test results into the database.

    All inserts for this run are wrapped in a single transaction:
    commit on success, rollback on any unhandled exception.  A failure in
    one run does NOT affect other runs.

    Args:
        conn:           Open SQLite connection (from db.connection.get_connection).
        run_meta:       GitHub workflow run metadata dict with keys:
                          github_run_id, commit_sha, branch, created_at.
        report:         Parsed pytest-json-report dict.
        test_id_cache:  Shared in-memory cache: (project, file_path, test_name) -> id.
        build_id_cache: Shared in-memory cache: github_run_id -> id.

    Returns:
        Dict with counts: inserted, skipped_unknown_status, skipped_ignored_outcome.
    """
    github_run_id = run_meta["github_run_id"]
    counts = {"inserted": 0, "skipped_unknown_status": 0, "skipped_ignored_outcome": 0}

    tests_raw: list[dict[str, Any]] = report.get("tests", [])
    if not tests_raw:
        logger.warning("Run %d report has no tests.", github_run_id)
        return counts

    flakiness_map = _detect_flakiness(tests_raw)

    try:
        # --- Build upsert ---
        build_id = _get_or_insert_build(
            conn,
            github_run_id=github_run_id,
            commit_sha=run_meta.get("commit_sha", "unknown"),
            branch=run_meta.get("branch"),
            created_at=run_meta.get("created_at", ""),
            build_id_cache=build_id_cache,
        )

        # --- Test + test_run inserts ---
        for nodeid, info in flakiness_map.items():
            raw_outcome = info["outcome"]

            # Handle ignored outcomes first (not an error, just skip quietly).
            if raw_outcome in _IGNORED_OUTCOMES:
                logger.debug(
                    "Run %d | %s: outcome %r is ignored (xfailed/xpassed). Skipping.",
                    github_run_id,
                    nodeid,
                    raw_outcome,
                )
                counts["skipped_ignored_outcome"] += 1
                continue

            # Map outcome to a schema-valid status.
            status = _OUTCOME_MAP.get(raw_outcome)
            if status is None:
                logger.warning(
                    "Run %d | %s: unknown outcome %r (not in CHECK constraint). "
                    "Skipping row to avoid constraint violation.",
                    github_run_id,
                    nodeid,
                    raw_outcome,
                )
                counts["skipped_unknown_status"] += 1
                continue

            # Defensive double-check (guards against future _OUTCOME_MAP bugs).
            if status not in VALID_STATUSES:
                logger.error(
                    "Run %d | %s: mapped status %r is not in VALID_STATUSES. "
                    "This is a bug. Skipping.",
                    github_run_id,
                    nodeid,
                    status,
                )
                counts["skipped_unknown_status"] += 1
                continue

            project, file_path, test_name = _parse_nodeid(nodeid)
            test_id = _get_or_insert_test(
                conn, project, file_path, test_name, test_id_cache
            )

            conn.execute(
                """
                INSERT INTO test_runs
                    (build_id, test_id, status, reported_flaky, duration_ms, retries)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    build_id,
                    test_id,
                    status,
                    info["reported_flaky"],
                    info["duration_ms"],
                    info["retries"],
                ),
            )
            counts["inserted"] += 1

        conn.commit()
        logger.info(
            "Run %d committed: %d inserted, %d skipped (unknown status), "
            "%d skipped (ignored outcome).",
            github_run_id,
            counts["inserted"],
            counts["skipped_unknown_status"],
            counts["skipped_ignored_outcome"],
        )

    except Exception:
        conn.rollback()
        logger.exception("Run %d failed — rolled back.", github_run_id)
        raise

    return counts
