import json, os, subprocess, sys
from db.connection import get_connection
from src.analyzer.quarantine import add_manual, export, release, sync

def test_manual_rows_not_auto_released_and_sync_cap(tmp_path):
    c=get_connection(':memory:')
    c.execute("INSERT INTO tests(project,file_path,test_name) VALUES('p','x.py','test_x')"); tid=c.execute('SELECT id FROM tests').fetchone()[0]
    c.execute("INSERT INTO flakiness_scores(test_id,sample_size,pass_rate,flip_rate,wilson_lower_bound,verdict,computed_at) VALUES(?,5,1,0,0,'stable','2026-01-01')",(tid,))
    c.execute("INSERT INTO builds(github_run_id,commit_sha,branch,created_at) VALUES(1,'a','dev','2026-01-01')")
    add_manual(c,tid,'keep manually')
    assert sync(c,dry_run=True,max_quarantine=0)['added']==0
    sync(c); assert c.execute('SELECT status FROM quarantine').fetchone()[0]=='active'
    release(c,tid); assert c.execute('SELECT status FROM quarantine').fetchone()[0]=='released'

def test_exported_class_node_id_and_plugin_marks_xfail(tmp_path):
    c=get_connection(':memory:'); c.execute("INSERT INTO tests(project,file_path,test_name) VALUES('p','tests/test_a.py','TestFoo::test_bad')"); tid=c.execute('SELECT id FROM tests').fetchone()[0]
    c.execute("INSERT INTO flakiness_scores(test_id,sample_size,pass_rate,flip_rate,wilson_lower_bound,verdict,computed_at) VALUES(?,10,.5,.5,.2,'flaky','2026-01-01')",(tid,)); c.commit()
    from src.analyzer.quarantine import sync
    c.execute("INSERT INTO builds(github_run_id,commit_sha,branch,created_at) VALUES(1,'a','dev','2026-01-01')"); c.commit()
    assert sync(c)['added']==1
    assert sync(c)['added']==0
    out=tmp_path/'quarantine.json'; export(c,out)
    data=json.loads(out.read_text()); assert data['quarantined_tests'][0]['node_id']=='tests/test_a.py::TestFoo::test_bad'
    project=tmp_path/'project'; project.mkdir(); (project/'tests').mkdir(); (project/'tests'/'test_a.py').write_text('class TestFoo:\n def test_bad(self):\n  assert False\n')
    env=os.environ.copy(); env['CHILLIFLAKE_QUARANTINE_FILE']=str(out)
    repo=str(__import__('pathlib').Path(__file__).resolve().parents[1]); env['PYTHONPATH']=repo+os.pathsep+env.get('PYTHONPATH','')
    result=subprocess.run([sys.executable,'-m','pytest','-q','--rootdir',str(project),'-p','src.analyzer.pytest_plugin'],cwd=project,env=env,text=True,capture_output=True)
    assert result.returncode==0 and '1 xfailed' in result.stdout, result.stdout
