import json
import sqlite3

import pytest
import yaml

from db.connection import get_connection
from scripts.migrate_v3 import migrate
from src.analyzer.quarantine import (
    Changes,
    diff_files,
    export_yaml,
    plan_changes,
    sync,
    validate_doc,
)
from src.analyzer.run_analysis import run_analysis
from src.project_config import get_project, project_slug
from src.ingestion.report_parser import ingest_run


def _test(conn, project, name='test_x'):
    conn.execute('INSERT INTO tests(project,file_path,test_name) VALUES(?,?,?)',
                 (project, f'{project}/{name}.py', name))
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _score(conn, test_id, verdict, samples=10, branch='dev'):
    conn.execute('''INSERT INTO flakiness_scores
        (test_id,analysis_branch,sample_size,pass_rate,flip_rate,wilson_lower_bound,
         verdict,computed_at) VALUES(?,?,?,.5,.5,.2,?,'2026-10-23T00:00:00+00:00')
        ON CONFLICT(test_id,analysis_branch) DO UPDATE SET sample_size=excluded.sample_size,
        verdict=excluded.verdict,computed_at=excluded.computed_at''',
                 (test_id, branch, samples, verdict))


def test_repository_project_slug_is_stable():
    assert project_slug('dask/dask') == 'dask'
    assert project_slug('home-assistant/core') == 'home-assistant-core'


def test_configured_repository_resolves_to_its_project():
    name, settings = get_project(repo='home-assistant/core')
    assert name == 'home-assistant-core'
    assert settings['workflow'] == 'CI'


def test_plan_changes_distinguishes_new_regression_and_release():
    changes = plan_changes({2, 3}, {4}, {1, 3}, {1, 4}, {2})
    assert changes == Changes([], [4], [2])


def test_project_scoped_analysis_keeps_other_project_scores():
    conn = get_connection(':memory:')
    first, second = _test(conn, 'alpha'), _test(conn, 'beta')
    for run_id, test_id in ((1, first), (2, second)):
        conn.execute('INSERT INTO builds(github_run_id,commit_sha,branch,created_at) VALUES(?,?,?,?)',
                     (run_id, str(run_id), 'main', f'2026-10-0{run_id}'))
        build_id = conn.execute('SELECT id FROM builds WHERE github_run_id=?', (run_id,)).fetchone()[0]
        conn.execute("INSERT INTO test_runs(build_id,test_id,status) VALUES(?,?,'failed')", (build_id, test_id))
    scores = run_analysis(conn, min_samples=1, branch='main', project='alpha')
    assert [score['test_id'] for score in scores] == [first]
    assert conn.execute('SELECT COUNT(*) FROM flakiness_scores WHERE test_id=?', (second,)).fetchone()[0] == 0
    assert conn.execute('SELECT project FROM analysis_runs').fetchone()[0] == 'alpha'
    assert scores.analysis_run_id == conn.execute('SELECT MAX(id) FROM analysis_runs').fetchone()[0]


def test_ingestion_keeps_project_test_identities_separate(tmp_path):
    report = tmp_path / 'results.xml'
    report.write_text('<testsuite><testcase file="tests/test_x.py" name="test_x" /></testsuite>', encoding='utf-8')
    conn = get_connection(':memory:')
    for project, run_id in (('alpha', 1), ('beta', 2)):
        ingest_run(conn, {'github_run_id': run_id, 'repo': f'org/{project}', 'commit_sha': 'a',
                          'branch': 'main', 'created_at': f'2026-10-0{run_id}'}, [report], {}, {}, project)
    assert conn.execute('SELECT COUNT(*) FROM tests WHERE file_path=? AND test_name=?',
                        ('tests/test_x.py', 'test_x')).fetchone()[0] == 2
    assert {row['repo'] for row in conn.execute('SELECT repo FROM builds')} == {'org/alpha', 'org/beta'}


def test_sync_release_then_requarantine_is_regressed_and_idempotent():
    conn = get_connection(':memory:')
    test_id = _test(conn, 'alpha')
    _score(conn, test_id, 'flaky', 10)
    first = sync(conn, project='alpha', branch='dev')
    assert first.newly_quarantined == [test_id]
    assert sync(conn, project='alpha', branch='dev').newly_quarantined == []
    _score(conn, test_id, 'stable', 15)
    released = sync(conn, project='alpha', branch='dev')
    assert released.released == [test_id]
    _score(conn, test_id, 'flaky', 20)
    regressed = sync(conn, project='alpha', branch='dev')
    assert regressed.regressed == [test_id]
    assert [row['event'] for row in conn.execute('SELECT event FROM quarantine_events ORDER BY id')] == [
        'quarantined', 'released', 'regressed']


def test_sync_dry_run_and_cap_do_not_write():
    conn = get_connection(':memory:')
    first, second = _test(conn, 'alpha', 'a'), _test(conn, 'alpha', 'b')
    _score(conn, first, 'flaky')
    _score(conn, second, 'flaky')
    changes = sync(conn, max_quarantine=1, dry_run=True, project='alpha', branch='dev')
    assert len(changes.newly_quarantined) == 1
    assert conn.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0] == 0
    committed = sync(conn, max_quarantine=1, project='alpha', branch='dev')
    assert len(committed.newly_quarantined) == 1
    assert conn.execute("SELECT COUNT(*) FROM quarantine WHERE status='active'").fetchone()[0] == 1


def test_sync_reuses_released_capacity_and_ranks_strongest_signal_first():
    conn = get_connection(':memory:')
    stable = _test(conn, 'alpha', 'stable')
    weaker = _test(conn, 'alpha', 'weaker')
    stronger = _test(conn, 'alpha', 'stronger')
    _score(conn, stable, 'stable', 15)
    _score(conn, weaker, 'flaky', 10)
    _score(conn, stronger, 'flaky', 10)
    conn.execute('UPDATE flakiness_scores SET wilson_lower_bound=.3 WHERE test_id=?', (stronger,))
    conn.execute("""INSERT INTO quarantine(test_id,source,reason,status,created_at,sample_size)
                    VALUES(?,'auto','old','active','2026-10-01',10)""", (stable,))
    conn.commit()
    changes = sync(conn, max_quarantine=1, project='alpha', branch='dev')
    assert changes.released == [stable]
    assert changes.newly_quarantined == [stronger]
    assert conn.execute("SELECT test_id FROM quarantine WHERE status='active'").fetchone()[0] == stronger


def test_yaml_export_is_atomic_deterministic_and_project_scoped(tmp_path, monkeypatch):
    import src.analyzer.quarantine as quarantine

    monkeypatch.setattr(quarantine, '_now', lambda: '2026-10-23T00:00:00+00:00')
    conn = get_connection(':memory:')
    alpha, beta = _test(conn, 'alpha', 'a'), _test(conn, 'beta', 'b')
    _score(conn, alpha, 'flaky')
    _score(conn, beta, 'flaky')
    from src.analyzer.quarantine import add_manual
    add_manual(conn, alpha, 'keep')
    add_manual(conn, beta, 'keep')
    out = tmp_path / 'alpha.yaml'
    doc = export_yaml(conn, out, 'alpha', source_meta={'repo': 'o/alpha', 'branch': 'dev'})
    first_bytes = out.read_bytes()
    export_yaml(conn, out, 'alpha', source_meta={'repo': 'o/alpha', 'branch': 'dev'})
    assert out.read_bytes() == first_bytes
    assert not out.with_name(out.name + '.tmp').exists()
    loaded = validate_doc(yaml.safe_load(out.read_text(encoding='utf-8')))
    assert {row['id'] for row in loaded['quarantine']} == {'alpha/a.py::a'}
    assert doc['summary']['quarantined'] == 1


def test_yaml_validation_and_file_diff(tmp_path, capsys):
    with pytest.raises(ValueError):
        validate_doc({'schema_version': 2})
    base = {'schema_version': 1, 'project': 'alpha', 'generated_at': 'now', 'source': {},
            'thresholds': {}, 'summary': {}, 'quarantine': [],
            'changes': {'newly_quarantined': [], 'released': [], 'regressed': []}}
    old = dict(base, quarantine=[{'id': 'a', 'source': 'auto', 'verdict': 'flaky', 'sample_size': 10}])
    old['source'] = {'builds_analyzed': 0, 'window': {}}
    old['summary'] = {'quarantined': 1, 'newly_quarantined': 0, 'released': 0, 'regressed': 0}
    old['quarantine'][0]['since'] = 'now'
    new = dict(base, source={'builds_analyzed': 0, 'window': {}},
               summary={'quarantined': 1, 'newly_quarantined': 1, 'released': 0, 'regressed': 1},
               quarantine=[{'id': 'b', 'source': 'manual', 'verdict': 'flaky', 'sample_size': 10, 'since': 'now'}],
               changes={'newly_quarantined': ['b'], 'released': [], 'regressed': ['b']})
    old_path, new_path = tmp_path / 'old.yaml', tmp_path / 'new.yaml'
    old_path.write_text(yaml.safe_dump(old), encoding='utf-8')
    new_path.write_text(yaml.safe_dump(new), encoding='utf-8')
    assert diff_files(old_path, new_path, fail_on_regressed=True) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {'added': ['b'], 'removed': ['a'], 'regressed': ['b']}


def test_yaml_plugin_xfails_listed_failure_and_keeps_unlisted_failure_failing(tmp_path):
    import os
    import subprocess
    import sys

    project = tmp_path / 'target'
    tests = project / 'tests'
    tests.mkdir(parents=True)
    (tests / 'test_sample.py').write_text(
        'def test_quarantined():\n    assert False\n\ndef test_unlisted():\n    assert False\n',
        encoding='utf-8')
    quarantine = {'schema_version': 1, 'project': 'target', 'generated_at': 'now',
                  'source': {'builds_analyzed': 0, 'window': {}}, 'thresholds': {},
                  'summary': {'quarantined': 1, 'newly_quarantined': 0, 'released': 0, 'regressed': 0},
                  'quarantine': [{'id': 'tests/test_sample.py::test_quarantined',
                                  'source': 'auto', 'verdict': 'flaky', 'sample_size': 20, 'since': 'now'}],
                  'changes': {'newly_quarantined': [], 'released': [], 'regressed': []}}
    path = project / 'quarantine.yaml'
    path.write_text(yaml.safe_dump(quarantine), encoding='utf-8')
    env = dict(os.environ)
    env['CHILLIFLAKE_QUARANTINE_FILE'] = str(path)
    repo = str(__import__('pathlib').Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = repo + os.pathsep + env.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--rootdir', str(project),
                             '-p', 'src.analyzer.pytest_plugin'], cwd=project, env=env,
                           text=True, capture_output=True)
    assert result.returncode != 0
    assert '1 xfailed' in result.stdout and '1 failed' in result.stdout


def test_plugin_treats_malformed_yaml_as_empty_quarantine(tmp_path, monkeypatch, caplog):
    from src.analyzer.pytest_plugin import pytest_collection_modifyitems
    path = tmp_path / 'invalid.yaml'
    path.write_text('schema_version: 99\n', encoding='utf-8')
    monkeypatch.setenv('CHILLIFLAKE_QUARANTINE_FILE', str(path))
    pytest_collection_modifyitems(object(), [])
    assert 'no tests will be quarantined' in caplog.text


def test_checked_in_quarantine_file_matches_current_plugin_schema():
    from pathlib import Path
    from src.analyzer.pytest_plugin import _load_ids

    assert _load_ids(Path(__file__).resolve().parents[1] / 'quarantine.yaml') == set()


def test_migrate_v3_adds_repo_and_branch_keyed_scores(tmp_path):
    db = tmp_path / 'old.db'
    conn = sqlite3.connect(db)
    conn.executescript('''
        CREATE TABLE builds(id INTEGER PRIMARY KEY,github_run_id INTEGER,commit_sha TEXT,branch TEXT,created_at TEXT);
        CREATE TABLE tests(id INTEGER PRIMARY KEY,project TEXT,file_path TEXT,test_name TEXT);
        CREATE TABLE test_runs(id INTEGER PRIMARY KEY,build_id INTEGER,test_id INTEGER,status TEXT);
        CREATE TABLE flakiness_scores(test_id INTEGER PRIMARY KEY,sample_size INTEGER,pass_rate REAL,
            flip_rate REAL,wilson_lower_bound REAL,duration_cv REAL,verdict TEXT,computed_at TEXT);
        INSERT INTO builds VALUES(1,1,'a','dev','2026-10-01');
        INSERT INTO tests VALUES(1,'home-assistant-core','x.py','test_x');
        INSERT INTO flakiness_scores VALUES(1,10,.5,.5,.2,NULL,'flaky','2026-10-02');
    ''')
    conn.commit(); conn.close()
    migrate(str(db))
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT repo FROM builds').fetchone()[0] == 'home-assistant/core'
    assert conn.execute('SELECT analysis_branch FROM flakiness_scores').fetchone()[0] == 'dev'
    pk = [row[1] for row in sorted(conn.execute('PRAGMA table_info(flakiness_scores)'), key=lambda row: row[5]) if row[5]]
    assert pk == ['test_id', 'analysis_branch']
    conn.close()


def test_migrate_v3_preserves_dask_provenance_and_normalizes_legacy_project(tmp_path):
    db = tmp_path / 'dask-old.db'
    conn = sqlite3.connect(db)
    conn.executescript('''
        CREATE TABLE builds(id INTEGER PRIMARY KEY,github_run_id INTEGER,commit_sha TEXT,branch TEXT,created_at TEXT);
        CREATE TABLE tests(id INTEGER PRIMARY KEY,project TEXT,file_path TEXT,test_name TEXT);
        CREATE TABLE test_runs(id INTEGER PRIMARY KEY,build_id INTEGER,test_id INTEGER,status TEXT);
        INSERT INTO builds VALUES(1,32105419778,'a','main','2026-08-18');
        INSERT INTO tests VALUES(1,'dask-dask','dask/tests/test_x.py','test_x');
        INSERT INTO test_runs VALUES(1,1,1,'passed');
    ''')
    conn.commit(); conn.close()
    migrate(str(db))
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT repo FROM builds').fetchone()[0] == 'dask/dask'
    assert conn.execute('SELECT project FROM tests').fetchone()[0] == 'dask'
    conn.close()
