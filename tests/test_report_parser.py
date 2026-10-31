import zipfile
import pytest
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
    assert conn.execute('SELECT test_name FROM tests').fetchone()['test_name'] == 't'

@pytest.mark.parametrize(('classname','expected'), [('tests.a.test_b.TestFoo','TestFoo::test_x'), ('tests.a.test_b.Outer.Inner','Outer::Inner::test_x')])
def test_classname_class_chain_is_node_id_compatible(tmp_path, classname, expected):
    conn=_db(); xml=_suite(f'<testcase classname="{classname}" name="test_x"/>')
    ingest_run(conn,_run_meta(),[_write(tmp_path,xml)],{}, {})
    row=conn.execute('SELECT file_path,test_name FROM tests').fetchone()
    assert row['file_path']=='tests/a/test_b.py' and row['test_name']==expected

def test_classname_identity_preserves_file_attribute_and_no_classname(tmp_path):
    conn=_db(); xml=_suite('<testcase classname="tests.a.TestFoo" name="test_x" file="custom/test.py"/>','<testcase name="test_y"/>')
    ingest_run(conn,_run_meta(),[_write(tmp_path,xml)],{}, {})
    rows=conn.execute('SELECT file_path,test_name FROM tests ORDER BY test_name').fetchall()
    assert (rows[0]['file_path'],rows[0]['test_name'])==('custom/test.py','TestFoo::test_x')
    assert (rows[1]['file_path'],rows[1]['test_name'])==('', 'test_y')

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

def test_caches_are_cleared_after_rollback(tmp_path,monkeypatch):
    real_conn=_db(); path=_write(tmp_path,_suite('<testcase classname="a" name="t"/>'))
    class Proxy:
        def __init__(self): self.calls=0; self.fail=True
        def execute(self,sql,*args):
            self.calls+=1
            if self.fail and self.calls==4: raise RuntimeError('forced failure')
            return real_conn.execute(sql,*args)
        def rollback(self): return real_conn.rollback()
        def commit(self): return real_conn.commit()
    conn=Proxy()
    builds={}; tests={}
    with pytest.raises(RuntimeError): ingest_run(conn,_run_meta(1),[path],tests,builds)
    assert not builds and not tests
    conn.fail=False; conn.calls=0
    result=ingest_run(conn,_run_meta(2),[path],tests,builds)
    assert result['inserted']==1

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

    def test_rejects_archive_path_traversal_and_cleans_up(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / 'chilliflake_xml_test'
        fake_dir.mkdir()
        monkeypatch.setattr('src.ingestion.report_parser.tempfile.mkdtemp', lambda prefix='': str(fake_dir))
        archive = tmp_path / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('../escaped.xml', '<testsuite/>')
        with pytest.raises(ValueError, match='Unsafe path'):
            extract_xml_files_from_zip(archive)
        assert not fake_dir.exists()
        assert not (tmp_path / 'escaped.xml').exists()
