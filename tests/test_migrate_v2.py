import hashlib, sqlite3
from db.connection import get_connection
from scripts.migrate_v2 import migrate

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def test_migration_rewrites_class_path_and_is_idempotent(tmp_path):
    path=tmp_path/'old.db'; c=sqlite3.connect(path); c.executescript('''
      CREATE TABLE builds(id INTEGER PRIMARY KEY,github_run_id INTEGER,commit_sha TEXT,branch TEXT,created_at TEXT);
      CREATE TABLE tests(id INTEGER PRIMARY KEY,project TEXT,file_path TEXT,test_name TEXT);
      CREATE TABLE test_runs(id INTEGER PRIMARY KEY,build_id INTEGER,test_id INTEGER,status TEXT);
      CREATE TABLE flakiness_scores(test_id INTEGER PRIMARY KEY,sample_size INTEGER,pass_rate REAL,flip_rate REAL,wilson_lower_bound REAL,duration_cv REAL,verdict TEXT,computed_at TEXT);
      INSERT INTO builds VALUES(1,1,'a','dev','1');
      INSERT INTO tests VALUES(1,'p','tests/a/test_b/TestFoo.py','test_x');
      INSERT INTO test_runs VALUES(1,1,1,'passed');
    '''); c.commit(); c.close()
    migrate(str(path))
    c=sqlite3.connect(path); assert c.execute('SELECT file_path,test_name FROM tests').fetchone()==('tests/a/test_b.py','TestFoo::test_x'); c.close()
    before=digest(path); migrate(str(path)); assert digest(path)==before
