from __future__ import annotations
import argparse, sqlite3
from pathlib import Path

def recompute(path, name):
    if not path or not path.endswith('.py'): return path,name
    segs=path[:-3].replace('\\','/').split('/')
    for i,s in enumerate(segs):
        if s[:1].isupper(): return ('/'.join(segs[:i])+'.py' if i else None),'::'.join(segs[i:]+[name])
    return path,name

def migrate(db_path):
    db=Path(db_path); backup=Path(str(db)+'.bak')
    if db.exists():
        check=sqlite3.connect(db_path)
        try:
            score_cols={r[1] for r in check.execute('PRAGMA table_info(flakiness_scores)')}
            q_cols={r[1] for r in check.execute('PRAGMA table_info(quarantine)')}
            has_quarantine={'sample_size'} <= q_cols
            has_identity=check.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tests_identity'").fetchone()
            if {'isolated_failures','commit_conflicts','analysis_branch'} <= score_cols and has_quarantine and has_identity:
                return
        finally: check.close()
    if db.exists():
        # SQLite's online backup API includes committed WAL data and works while
        # other processes have read connections open on Windows.
        with sqlite3.connect(db_path, timeout=30) as source, sqlite3.connect(backup, timeout=30) as target:
            source.backup(target)
    conn=sqlite3.connect(db_path); conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        original_test_count=conn.execute('SELECT COUNT(*) FROM tests').fetchone()[0]
        with conn:
            q_cols={r[1] for r in conn.execute('PRAGMA table_info(quarantine)')}
            if q_cols and 'sample_size' not in q_cols:
                conn.execute('ALTER TABLE quarantine ADD COLUMN sample_size INTEGER NOT NULL DEFAULT 0')
            conn.execute('DROP TABLE IF EXISTS flakiness_scores')
            rows=conn.execute('SELECT id,project,file_path,test_name FROM tests').fetchall()
            for r in rows:
                np,nn=recompute(r['file_path'],r['test_name'])
                if (np,nn)==(r['file_path'],r['test_name']): continue
                target=conn.execute("SELECT id FROM tests WHERE project=? AND COALESCE(file_path,'')=COALESCE(?,'') AND test_name=? AND id!=?",(r['project'],np,nn,r['id'])).fetchone()
                if target:
                    conn.execute('UPDATE test_runs SET test_id=? WHERE test_id=?',(target['id'],r['id'])); conn.execute('DELETE FROM tests WHERE id=?',(r['id'],))
                else: conn.execute('UPDATE tests SET file_path=?,test_name=? WHERE id=?',(np,nn,r['id']))
            for r in conn.execute("SELECT project,test_name,MIN(id) keep FROM tests WHERE file_path IS NULL GROUP BY project,test_name HAVING COUNT(*)>1").fetchall():
                dup=conn.execute('SELECT id FROM tests WHERE file_path IS NULL AND project=? AND test_name=? AND id!=?',(r['project'],r['test_name'],r['keep'])).fetchall()
                for d in dup:
                    conn.execute('UPDATE test_runs SET test_id=? WHERE test_id=?',(r['keep'],d['id'])); conn.execute('DELETE FROM tests WHERE id=?',(d['id'],))
        bad=conn.execute("SELECT id,file_path FROM tests WHERE file_path IS NOT NULL").fetchall()
        if any(any(part[:1].isupper() for part in p.replace('\\','/').split('/')) for r in bad for p in [r['file_path']]): raise RuntimeError('CapWords file path remains after migration')
        if conn.execute('PRAGMA foreign_key_check').fetchall(): raise RuntimeError('Foreign key check failed')
        if conn.execute('SELECT COUNT(*) FROM tests').fetchone()[0] > original_test_count:
            raise RuntimeError('Migration increased the test row count')
        schema=Path(__file__).resolve().parents[1]/'db'/'schema.sql'
        conn.executescript(schema.read_text(encoding='utf-8'))
    finally: conn.close()

def main():
    p=argparse.ArgumentParser(); p.add_argument('--db',default='db/chilliflake.db'); migrate(p.parse_args().db)
if __name__=='__main__': main()
