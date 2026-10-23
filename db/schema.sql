PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    github_run_id BIGINT UNIQUE NOT NULL,
    commit_sha VARCHAR(40) NOT NULL,
    branch VARCHAR(255),
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project VARCHAR(50) NOT NULL,
    file_path VARCHAR(255),
    test_name TEXT NOT NULL,
    UNIQUE(project, file_path, test_name)
);

CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id INTEGER NOT NULL,
    test_id INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL CHECK(status IN ('passed', 'failed', 'timedOut', 'skipped', 'interrupted')),
    reported_flaky BOOLEAN NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(build_id) REFERENCES builds(id),
    FOREIGN KEY(test_id) REFERENCES tests(id)
);

CREATE INDEX IF NOT EXISTS idx_test_runs_test_id ON test_runs(test_id);
CREATE INDEX IF NOT EXISTS idx_test_runs_test_build ON test_runs(test_id, build_id);
CREATE INDEX IF NOT EXISTS idx_test_runs_build_test ON test_runs(build_id, test_id, status);
CREATE INDEX IF NOT EXISTS idx_test_runs_build_id ON test_runs(build_id);
CREATE INDEX IF NOT EXISTS idx_builds_created_at ON builds(created_at);
CREATE INDEX IF NOT EXISTS idx_builds_branch_commit ON builds(branch, commit_sha);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tests_identity ON tests(project, COALESCE(file_path, ''), test_name);

CREATE TABLE IF NOT EXISTS flakiness_scores (
    test_id INTEGER PRIMARY KEY,
    analysis_branch TEXT NOT NULL DEFAULT 'dev',
    sample_size INTEGER NOT NULL,
    pass_rate REAL NOT NULL,
    flip_rate REAL NOT NULL,
    wilson_lower_bound REAL NOT NULL,
    duration_cv REAL,
    isolated_failures INTEGER NOT NULL DEFAULT 0,
    commit_conflicts INTEGER NOT NULL DEFAULT 0,
    verdict VARCHAR(20) NOT NULL CHECK(verdict IN ('stable','flaky','broken','insufficient_data')),
    computed_at TIMESTAMP NOT NULL,
    FOREIGN KEY(test_id) REFERENCES tests(id)
);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('auto','manual')),
    reason TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    created_at TIMESTAMP NOT NULL,
    released_at TIMESTAMP,
    sample_size INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(test_id) REFERENCES tests(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_quarantine_active_test ON quarantine(test_id) WHERE status='active';
