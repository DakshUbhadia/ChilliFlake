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
