from argparse import Namespace
import zipfile
import pytest
from db.connection import get_connection
import src.ingestion.run_ingestion as ingestion

def test_partial_artifact_download_does_not_ingest(monkeypatch,tmp_path):
    db_path=tmp_path/'partial.db'; conn=get_connection(str(db_path))
    monkeypatch.setattr(ingestion,'_parse_args',lambda:Namespace(runs=1,repo='o/r',workflow='CI',branch='',created_after='all',artifact_name='test-',db=None))
    monkeypatch.setattr(ingestion,'get_connection',lambda _:conn)
    monkeypatch.setattr(ingestion,'make_session',lambda token:object())
    monkeypatch.setattr(ingestion,'list_workflow_runs',lambda *a,**k:[{'id':77,'head_sha':'a','head_branch':'dev','created_at':'2026-01-01'}])
    monkeypatch.setattr(ingestion,'list_artifacts',lambda *a,**k:[{'id':1,'name':'one'},{'id':2,'name':'two'}])
    zip_path=tmp_path/'one.zip'
    with zipfile.ZipFile(zip_path,'w') as z: z.writestr('result.xml','<testsuite><testcase classname="a" name="test_x"/></testsuite>')
    paths=iter([zip_path])
    def download(*a,**k):
        if a[1]==2: raise OSError('failed download')
        return next(paths)
    monkeypatch.setattr(ingestion,'download_artifact_zip',download)
    monkeypatch.setenv('GITHUB_TOKEN','test')
    with pytest.raises(SystemExit) as exc:
        ingestion.main()
    assert exc.value.code == 1
    import sqlite3
    check=sqlite3.connect(db_path)
    assert check.execute('SELECT COUNT(*) FROM builds').fetchone()[0]==0
    assert check.execute('SELECT COUNT(*) FROM test_runs').fetchone()[0]==0
    check.close()

def test_already_ingested_short_circuits_before_api(monkeypatch):
    conn=get_connection(':memory:'); conn.execute("INSERT INTO builds(github_run_id,commit_sha,branch,created_at) VALUES(77,'a','dev','2026')")
    bid=conn.execute('SELECT id FROM builds').fetchone()[0]
    conn.execute("INSERT INTO tests(project,test_name) VALUES('p','t')"); tid=conn.execute('SELECT id FROM tests').fetchone()[0]
    conn.execute("INSERT INTO test_runs(build_id,test_id,status) VALUES(?,?,'passed')",(bid,tid)); conn.commit()
    assert ingestion._already_ingested(conn,77)
