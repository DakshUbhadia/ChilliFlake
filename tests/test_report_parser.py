import sys
import zipfile
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_connection
from src.ingestion.report_parser import extract_xml_files_from_zip, ingest_run

def _db():
    return get_connection(':memory:')

def _run_meta(run_id=1001):
    return {'github_run_id': run_id, 'commit_sha': 'a' * 40, 'branch': 'dev', 'created_at': '2026-08-01T00:00:00Z'}

def _suite(*testcases, wrap=False):
    body = f"""<testsuite name="s">{''.join(testcases)}</testsuite>"""
    return f'<testsuites>{body}</testsuites>' if wrap else body

def _write(tmp_path, content, name='r.xml'):
    p = tmp_path / name
    p.write_text(content)
    return p

@pytest.mark.parametrize('testcase_xml, expected_status', [('<testcase classname="a.b" name="t" time="0.05"/>', 'passed'), ('<testcase classname="a.b" name="t" time="0.05"><failure message="x"/></testcase>', 'failed'), ('<testcase classname="a.b" name="t" time="0.05"><error message="x"/></testcase>', 'failed'), ('<testcase classname="a.b" name="t" time="0.05"><skipped/></testcase>', 'skipped')])
def test_status_mapping(tmp_path, testcase_xml, expected_status):
    conn = _db()
    counts = ingest_run(conn, _run_meta(), [_write(tmp_path, _suite(testcase_xml))], {}, {})
    assert counts == {'inserted': 1, 'skipped_bad_status': 0, 'files_parsed': 1, 'files_skipped': 0, 'already_ingested': 0}
    row = conn.execute('SELECT * FROM test_runs').fetchone()
    assert row['status'] == expected_status
    assert row['reported_flaky'] == 0 and row['retries'] == 0
    assert row['duration_ms'] == 50

def test_file_attribute_preferred_over_classname(tmp_path):
    conn = _db()
    xml = _suite('<testcase classname="a.b.TestFoo" name="t" file="a/b.py" time="0.01"/>')
    ingest_run(conn, _run_meta(), [_write(tmp_path, xml)], {}, {})
    assert conn.execute('SELECT file_path FROM tests').fetchone()['file_path'] == 'a/b.py'

def test_classname_fallback_when_no_file_attribute(tmp_path):
    conn = _db()
    xml = _suite('<testcase classname="a.b" name="t" time="0.01"/>')
    ingest_run(conn, _run_meta(), [_write(tmp_path, xml)], {}, {})
    assert conn.execute('SELECT file_path FROM tests').fetchone()['file_path'] == 'a/b.py'

def test_testsuites_wrapper_root_supported(tmp_path):
    conn = _db()
    xml = _suite('<testcase classname="a" name="t" time="0.01"/>', wrap=True)
    counts = ingest_run(conn, _run_meta(), [_write(tmp_path, xml)], {}, {})
    assert counts['inserted'] == 1

def test_multiple_shard_files_merge_into_one_build(tmp_path):
    conn = _db()
    f1 = _write(tmp_path, _suite('<testcase classname="a" name="t1" time="0.01"/>'), 'r1.xml')
    f2 = _write(tmp_path, _suite('<testcase classname="a" name="t2" time="0.01"/>'), 'r2.xml')
    counts = ingest_run(conn, _run_meta(), [f1, f2], {}, {})
    assert counts == {'inserted': 2, 'skipped_bad_status': 0, 'files_parsed': 2, 'files_skipped': 0, 'already_ingested': 0}
    assert conn.execute('SELECT COUNT(*) c FROM builds').fetchone()['c'] == 1

def test_malformed_file_skipped_not_fatal(tmp_path):
    conn = _db()
    good = _write(tmp_path, _suite('<testcase classname="a" name="t" time="0.01"/>'), 'good.xml')
    bad = _write(tmp_path, '<not><valid', 'bad.xml')
    counts = ingest_run(conn, _run_meta(), [good, bad], {}, {})
    assert counts['inserted'] == 1
    assert counts['files_parsed'] == 1
    assert counts['files_skipped'] == 1

def test_no_testsuite_data_skipped(tmp_path):
    conn = _db()
    counts = ingest_run(conn, _run_meta(), [_write(tmp_path, '<somethingelse/>')], {}, {})
    assert counts == {'inserted': 0, 'skipped_bad_status': 0, 'files_parsed': 0, 'files_skipped': 1, 'already_ingested': 0}

def test_reingest_same_run_is_true_noop(tmp_path):
    conn = _db()
    xml_path = _write(tmp_path, _suite('<testcase classname="a" name="t" time="0.01"/>'))
    meta = _run_meta(run_id=5555)
    build_cache, test_cache = ({}, {})
    first = ingest_run(conn, meta, [xml_path], test_cache, build_cache)
    second = ingest_run(conn, meta, [xml_path], test_cache, build_cache)
    assert first['inserted'] == 1 and first['already_ingested'] == 0
    assert second['inserted'] == 0 and second['already_ingested'] == 1
    assert conn.execute('SELECT COUNT(*) c FROM test_runs').fetchone()['c'] == 1

class TestExtractZip:

    def test_extracts_all_xml_files(self, tmp_path):
        zip_path = tmp_path / 'a.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('one.xml', _suite('<testcase classname="a" name="t" time="0.01"/>'))
            zf.writestr('two.xml', _suite('<testcase classname="a" name="t2" time="0.01"/>'))
            zf.writestr('readme.txt', 'ignore me')
        _, xml_paths = extract_xml_files_from_zip(zip_path)
        assert len(xml_paths) == 2
        assert all((p.suffix == '.xml' for p in xml_paths))

    def test_no_xml_files_returns_empty_list(self, tmp_path):
        zip_path = tmp_path / 'b.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('readme.txt', 'no xml here')
        _, xml_paths = extract_xml_files_from_zip(zip_path)
        assert xml_paths == []

    def test_corrupt_zip_cleans_up_temp_dir(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / 'chilliflake_xml_test'
        fake_dir.mkdir()
        monkeypatch.setattr('src.ingestion.report_parser.tempfile.mkdtemp', lambda prefix='': str(fake_dir))
        bad_zip = tmp_path / 'corrupt.zip'
        bad_zip.write_bytes(b'not a real zip')
        with pytest.raises(zipfile.BadZipFile):
            extract_xml_files_from_zip(bad_zip)
        assert not fake_dir.exists()