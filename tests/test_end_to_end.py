import yaml

from db.connection import get_connection
from src.analyzer.pytest_plugin import _load_ids
from src.analyzer.quarantine import export_yaml, sync, validate_doc
from src.analyzer.run_analysis import run_analysis
from src.ingestion.report_parser import ingest_run


def test_ingestion_analysis_quarantine_export_and_consumer(tmp_path):
    conn = get_connection(':memory:')
    statuses = ['passed', 'failed', 'passed', 'passed', 'passed']
    commits = ['same-code', 'same-code', 'c', 'd', 'e']

    for index, (flaky_status, commit) in enumerate(zip(statuses, commits), 1):
        failure = '<failure />' if flaky_status == 'failed' else ''
        report = tmp_path / f'{index}.xml'
        report.write_text(
            '<testsuite>'
            f'<testcase file="tests/test_app.py" name="test_flaky">{failure}</testcase>'
            '<testcase file="tests/test_app.py" name="test_broken"><failure /></testcase>'
            '<testcase file="tests/test_app.py" name="test_stable" />'
            '</testsuite>',
            encoding='utf-8',
        )
        ingest_run(
            conn,
            {'github_run_id': index, 'commit_sha': commit, 'branch': 'main',
             'created_at': f'2026-10-{index:02d}T00:00:00Z', 'repo': 'org/app'},
            [report],
            {},
            {},
            project='app',
        )

    scores = run_analysis(conn, min_samples=5, branch='main', project='app')
    names = {row['test_name']: row['id'] for row in conn.execute('SELECT id,test_name FROM tests')}
    by_id = {score['test_id']: score for score in scores}
    assert by_id[names['test_flaky']]['verdict'] == 'flaky'
    assert by_id[names['test_flaky']]['commit_conflicts'] == 1
    assert by_id[names['test_broken']]['verdict'] == 'broken'
    assert by_id[names['test_stable']]['verdict'] == 'stable'

    changes = sync(conn, min_samples=5, project='app', branch='main')
    assert changes.newly_quarantined == [names['test_flaky']]
    output = tmp_path / 'quarantine.yaml'
    export_yaml(
        conn,
        output,
        'app',
        changes=changes,
        source_meta={'repo': 'org/app', 'branch': 'main', 'builds_analyzed': 5,
                     'window': {'from': '2026-10-01', 'to': '2026-10-05'}},
        branch='main',
    )
    document = validate_doc(yaml.safe_load(output.read_text(encoding='utf-8')))
    assert document['summary']['quarantined'] == 1
    assert _load_ids(output) == {'tests/test_app.py::test_flaky'}
