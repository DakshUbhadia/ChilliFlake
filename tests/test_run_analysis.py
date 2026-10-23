import sqlite3
from db.connection import get_connection
from src.analyzer.run_analysis import _score_runs, run_analysis

def test_score_runs_pure_metrics():
    score=_score_runs(4,['passed','failed','passed'],[10,20,30],commit_conflicts=1,min_samples=2)
    assert score['sample_size']==3 and score['isolated_failures']==1
    assert score['commit_conflicts']==1 and score['verdict']=='flaky'

def test_null_file_path_identity_is_unique():
    c=get_connection(':memory:')
    c.execute("INSERT INTO tests(project,file_path,test_name) VALUES('p',NULL,'test_x')")
    c.execute("INSERT OR IGNORE INTO tests(project,file_path,test_name) VALUES('p',NULL,'test_x')")
    assert c.execute('SELECT COUNT(*) FROM tests').fetchone()[0]==1

def test_branch_scope_and_same_commit_conflict():
    c=get_connection(':memory:')
    c.execute("INSERT INTO tests(project,file_path,test_name) VALUES('p','x.py','test_x')")
    tid=c.execute('SELECT id FROM tests').fetchone()[0]
    for run,branch,sha,status in [(1,'dev','a','passed'),(2,'dev','b','failed'),(3,'main','a','failed'),(4,'main','b','passed')]:
        c.execute('INSERT INTO builds(github_run_id,commit_sha,branch,created_at) VALUES(?,?,?,?)',(run,sha,branch,str(run)))
        bid=c.execute('SELECT id FROM builds WHERE github_run_id=?',(run,)).fetchone()[0]
        c.execute('INSERT INTO test_runs(build_id,test_id,status) VALUES(?,?,?)',(bid,tid,status))
    scores=run_analysis(c,min_samples=2,branch='dev')
    assert len(scores)==1 and scores[0]['commit_conflicts']==0 and scores[0]['analysis_branch']=='dev'
    scores=run_analysis(c,min_samples=2,branch='')
    assert scores[0]['commit_conflicts']==2 and scores[0]['verdict']=='flaky' and scores[0]['analysis_branch']=='*'

def test_analysis_purges_stale_scores_and_ignores_skipped_only():
    c=get_connection(':memory:')
    c.execute("INSERT INTO tests(project,test_name) VALUES('p','only_skip')")
    tid=c.execute('SELECT id FROM tests').fetchone()[0]
    c.execute("INSERT INTO builds(github_run_id,commit_sha,branch,created_at) VALUES(1,'a','dev','1')")
    bid=c.execute('SELECT id FROM builds').fetchone()[0]
    c.execute("INSERT INTO test_runs(build_id,test_id,status) VALUES(?,?,'skipped')",(bid,tid))
    c.commit(); assert run_analysis(c)==[]
    assert c.execute('SELECT COUNT(*) FROM flakiness_scores').fetchone()[0]==0
