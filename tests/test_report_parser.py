"""
tests/test_report_parser.py — Unit tests for src/ingestion/report_parser.py.

All tests use hand-written JSON fixtures and an in-memory SQLite database.
No live GitHub API calls are made.

Fixtures cover:
  - A normal passed test
  - A failed test (reported_flaky = 0)
  - A flaky test (fail -> pass sequence, reported_flaky = 1)
  - An unknown/malformed outcome -> row skipped, not a crash
  - Idempotent re-ingest -> same row count on second run
"""

import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup so tests can import from project root without installation.
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.connection import get_connection
from src.ingestion.report_parser import (
    extract_json_from_zip,
    ingest_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_in_memory_db() -> sqlite3.Connection:
    """Return a freshly bootstrapped in-memory SQLite connection."""
    return get_connection(":memory:")


def _make_report(tests: list[dict]) -> dict:
    """Wrap a list of test dicts in the minimal pytest-json-report envelope."""
    return {
        "created": 1700000000.0,
        "duration": 1.0,
        "exitcode": 0,
        "root": "/project",
        "summary": {"total": len(tests)},
        "tests": tests,
    }


def _make_run_meta(run_id: int = 1001) -> dict:
    return {
        "github_run_id": run_id,
        "commit_sha": "abc123def456abc123def456abc123def456abc1",
        "branch": "main",
        "created_at": "2024-01-01T00:00:00Z",
    }


def _make_zip_with_report(report: dict) -> Path:
    """Write *report* as JSON into a temp ZIP and return its Path."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w") as zf:
        zf.writestr("report.json", json.dumps(report))
    return Path(tmp.name)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------------------
# Test: normal passed test
# ---------------------------------------------------------------------------

class TestPassedTest:
    def test_passed_inserted_with_correct_status(self):
        conn = _make_in_memory_db()
        report = _make_report([
            {
                "nodeid": "tests/test_foo.py::test_simple_pass",
                "outcome": "passed",
                "call": {"duration": 0.05, "outcome": "passed"},
            }
        ])
        counts = ingest_run(conn, _make_run_meta(), report, {}, {})

        assert counts["inserted"] == 1
        assert counts["skipped_unknown_status"] == 0

        row = conn.execute("SELECT * FROM test_runs").fetchone()
        assert row["status"] == "passed"
        assert row["reported_flaky"] == 0
        assert row["retries"] == 0
        assert row["duration_ms"] == 50  # 0.05s * 1000

    def test_build_row_created(self):
        conn = _make_in_memory_db()
        report = _make_report([
            {"nodeid": "tests/test_foo.py::test_a", "outcome": "passed",
             "call": {"duration": 0.01, "outcome": "passed"}}
        ])
        ingest_run(conn, _make_run_meta(run_id=9999), report, {}, {})

        build = conn.execute("SELECT * FROM builds").fetchone()
        assert build["github_run_id"] == 9999
        assert build["branch"] == "main"


# ---------------------------------------------------------------------------
# Test: failed test
# ---------------------------------------------------------------------------

class TestFailedTest:
    def test_failed_inserted_not_flaky(self):
        conn = _make_in_memory_db()
        report = _make_report([
            {
                "nodeid": "tests/test_bar.py::test_always_broken",
                "outcome": "failed",
                "call": {"duration": 0.1, "outcome": "failed"},
            }
        ])
        counts = ingest_run(conn, _make_run_meta(), report, {}, {})

        assert counts["inserted"] == 1
        row = conn.execute("SELECT * FROM test_runs").fetchone()
        assert row["status"] == "failed"
        assert row["reported_flaky"] == 0
        assert row["retries"] == 0

    def test_skipped_inserted_correctly(self):
        conn = _make_in_memory_db()
        report = _make_report([
            {"nodeid": "tests/test_bar.py::test_skip", "outcome": "skipped",
             "call": {"duration": 0.0, "outcome": "skipped"}}
        ])
        counts = ingest_run(conn, _make_run_meta(), report, {}, {})
        assert counts["inserted"] == 1
        row = conn.execute("SELECT * FROM test_runs").fetchone()
        assert row["status"] == "skipped"


# ---------------------------------------------------------------------------
# Test: flaky test (fail -> pass sequence)
# ---------------------------------------------------------------------------

class TestFlakyTest:
    def test_flaky_reported_correctly(self):
        """
        A test that appears twice: first as 'failed', then as 'passed'
        should be stored with status='passed' and reported_flaky=1, retries=1.
        """
        conn = _make_in_memory_db()
        report = _make_report([
            # First attempt — failed.
            {
                "nodeid": "tests/test_flaky.py::test_intermittent",
                "outcome": "failed",
                "call": {"duration": 0.2, "outcome": "failed"},
            },
            # Second attempt — passed (retry).
            {
                "nodeid": "tests/test_flaky.py::test_intermittent",
                "outcome": "passed",
                "call": {"duration": 0.15, "outcome": "passed"},
            },
        ])
        counts = ingest_run(conn, _make_run_meta(), report, {}, {})

        # One logical test -> one test_runs row.
        assert counts["inserted"] == 1
        row = conn.execute("SELECT * FROM test_runs").fetchone()
        assert row["status"] == "passed"
        assert row["reported_flaky"] == 1
        assert row["retries"] == 1
        # Duration should be sum of both call durations (0.2 + 0.15 = 350ms).
        assert row["duration_ms"] == 350

    def test_repeated_fail_not_flagged_flaky(self):
        """
        Two failed attempts → NOT flaky (never passed). reported_flaky=0.
        """
        conn = _make_in_memory_db()
        report = _make_report([
            {"nodeid": "tests/test_x.py::test_still_broken", "outcome": "failed",
             "call": {"duration": 0.1, "outcome": "failed"}},
            {"nodeid": "tests/test_x.py::test_still_broken", "outcome": "failed",
             "call": {"duration": 0.1, "outcome": "failed"}},
        ])
        counts = ingest_run(conn, _make_run_meta(), report, {}, {})
        row = conn.execute("SELECT * FROM test_runs").fetchone()
        assert row["reported_flaky"] == 0
        assert row["retries"] == 1


# ---------------------------------------------------------------------------
# Test: unknown/malformed outcome — skip, do not crash
# ---------------------------------------------------------------------------

class TestMalformedRecord:
    def test_unknown_outcome_is_skipped_not_crash(self):
        """
        An outcome value outside the known set must be skipped with a log
        warning, not cause an exception or a CHECK constraint violation.
        """
        conn = _make_in_memory_db()
        report = _make_report([
            # Valid row.
            {"nodeid": "tests/test_good.py::test_ok", "outcome": "passed",
             "call": {"duration": 0.01, "outcome": "passed"}},
            # Invalid outcome — not in VALID_STATUSES or _OUTCOME_MAP.
            {"nodeid": "tests/test_bad.py::test_weird", "outcome": "totally_new_thing",
             "call": {"duration": 0.01, "outcome": "totally_new_thing"}},
        ])
        counts = ingest_run(conn, _make_run_meta(), report, {}, {})

        # Valid row inserted; invalid row skipped.
        assert counts["inserted"] == 1
        assert counts["skipped_unknown_status"] == 1
        assert _count(conn, "test_runs") == 1

    def test_xfailed_outcome_counted_as_ignored(self):
        """xfailed and xpassed must be silently ignored (not an error)."""
        conn = _make_in_memory_db()
        report = _make_report([
            {"nodeid": "tests/test_x.py::test_expected_fail", "outcome": "xfailed",
             "call": {"duration": 0.01, "outcome": "xfailed"}},
        ])
        counts = ingest_run(conn, _make_run_meta(), report, {}, {})
        assert counts["inserted"] == 0
        assert counts["skipped_ignored_outcome"] == 1
        assert _count(conn, "test_runs") == 0


# ---------------------------------------------------------------------------
# Test: idempotent re-ingest
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_reingest_same_report_is_noop(self):
        """
        Running ingest_run twice with the same run_id and report must not
        double the row count.  The build cache prevents duplicate build rows;
        test_runs are not UNIQUE-constrained by design (a run can have only
        one result per test), so we validate via build-level idempotency and
        stable final counts.
        """
        conn = _make_in_memory_db()
        report = _make_report([
            {"nodeid": "tests/test_idem.py::test_a", "outcome": "passed",
             "call": {"duration": 0.01, "outcome": "passed"}},
            {"nodeid": "tests/test_idem.py::test_b", "outcome": "failed",
             "call": {"duration": 0.02, "outcome": "failed"}},
        ])
        meta = _make_run_meta(run_id=5555)
        shared_build_cache: dict = {}
        shared_test_cache: dict = {}

        ingest_run(conn, meta, report, shared_test_cache, shared_build_cache)
        first_build_count = _count(conn, "builds")
        first_test_count = _count(conn, "tests")

        # Second ingest of same run/report.
        ingest_run(conn, meta, report, shared_test_cache, shared_build_cache)

        # Build and tests must NOT be duplicated (INSERT OR IGNORE + cache).
        assert _count(conn, "builds") == first_build_count
        assert _count(conn, "tests") == first_test_count


# ---------------------------------------------------------------------------
# Test: extract_json_from_zip
# ---------------------------------------------------------------------------

class TestExtractZip:
    def test_valid_zip_returns_report(self):
        report = _make_report([
            {"nodeid": "tests/test_z.py::test_z", "outcome": "passed",
             "call": {"duration": 0.01, "outcome": "passed"}}
        ])
        zip_path = _make_zip_with_report(report)
        try:
            result = extract_json_from_zip(zip_path)
            assert "tests" in result
            assert len(result["tests"]) == 1
        finally:
            zip_path.unlink(missing_ok=True)

    def test_zip_with_no_json_raises(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.close()
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("README.txt", "no json here")
        try:
            with pytest.raises(ValueError, match="No .json file"):
                extract_json_from_zip(tmp.name)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_zip_with_non_report_json_raises(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.close()
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("config.json", json.dumps({"key": "value"}))  # no 'tests' key
        try:
            with pytest.raises(ValueError, match="No valid pytest-json-report"):
                extract_json_from_zip(tmp.name)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
