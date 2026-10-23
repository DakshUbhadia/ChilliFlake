from __future__ import annotations
import argparse, datetime, json, logging
from pathlib import Path
from db.connection import get_connection

log = logging.getLogger('chilliflake.quarantine')

def _now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()

def sync(conn, min_samples=5, max_quarantine=200, min_new_samples=5, dry_run=False):
    latest_score = conn.execute('SELECT MAX(computed_at) FROM flakiness_scores').fetchone()[0]
    latest_build = conn.execute('SELECT MAX(created_at) FROM builds').fetchone()[0]
    if latest_build and (not latest_score or latest_score < latest_build):
        raise RuntimeError('Scores are stale relative to ingested builds; run analysis before sync.')
    current = conn.execute("SELECT q.*,fs.verdict,fs.sample_size,t.file_path,t.test_name FROM quarantine q JOIN tests t ON t.id=q.test_id LEFT JOIN flakiness_scores fs ON fs.test_id=q.test_id WHERE q.status='active'").fetchall()
    active_auto = [r for r in current if r['source']=='auto']
    candidates = conn.execute("SELECT fs.test_id,fs.verdict,fs.sample_size,fs.computed_at,t.file_path,t.test_name FROM flakiness_scores fs JOIN tests t ON t.id=fs.test_id WHERE fs.verdict='flaky' AND fs.sample_size>=? ORDER BY fs.wilson_lower_bound DESC", (min_samples,)).fetchall()
    active_ids = {r['test_id'] for r in current}
    add = [r for r in candidates if r['test_id'] not in active_ids]
    release=[]
    for r in active_auto:
        score = conn.execute('SELECT verdict,sample_size FROM flakiness_scores WHERE test_id=?', (r['test_id'],)).fetchone()
        if score and score['verdict']=='stable' and score['sample_size'] >= r['sample_size'] + min_new_samples:
            release.append(r)
    capacity=max(0,max_quarantine-len(current)); add=add[:capacity]
    if not dry_run:
        now=_now()
        for r in add:
            conn.execute("INSERT INTO quarantine(test_id,source,reason,status,created_at,sample_size) VALUES(?,'auto',?,'active',?,?)", (r['test_id'],f"flaky verdict; {r['sample_size']} samples",now,r['sample_size']))
        for r in release:
            conn.execute("UPDATE quarantine SET status='released',released_at=? WHERE id=? AND source='auto' AND status='active'", (now,r['id']))
        conn.commit()
    return {'added':len(add),'released':len(release),'dry_run':dry_run}

def add_manual(conn, test_id, reason='manual'):
    conn.execute("INSERT INTO quarantine(test_id,source,reason,status,created_at,sample_size) VALUES(?,'manual',?,'active',?,0)", (test_id,reason,_now())); conn.commit()

def release(conn, test_id):
    conn.execute("UPDATE quarantine SET status='released',released_at=? WHERE test_id=? AND status='active'", (_now(),test_id)); conn.commit()

def export(conn, out):
    rows=conn.execute("SELECT t.file_path,t.test_name,q.source,q.reason FROM quarantine q JOIN tests t ON t.id=q.test_id WHERE q.status='active' ORDER BY t.file_path,t.test_name").fetchall()
    payload={'generated_at':_now(),'quarantined_tests':[{'node_id':f"{r['file_path']}::{r['test_name']}" if r['file_path'] else r['test_name'],'source':r['source'],'reason':r['reason']} for r in rows]}
    Path(out).write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8'); return payload

def main():
    p=argparse.ArgumentParser(); p.add_argument('--db',default=None); sub=p.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('sync'); s.add_argument('--min-samples',type=int,default=5); s.add_argument('--max-quarantine',type=int,default=200); s.add_argument('--min-new-samples',type=int,default=5); s.add_argument('--dry-run',action='store_true')
    e=sub.add_parser('export'); e.add_argument('--out',default='quarantine.json')
    sub.add_parser('list')
    r=sub.add_parser('release'); r.add_argument('test_id',type=int)
    a=p.parse_args(); c=get_connection(a.db)
    try:
        if a.cmd=='sync': print(sync(c,a.min_samples,a.max_quarantine,a.min_new_samples,a.dry_run))
        elif a.cmd=='export': export(c,a.out)
        elif a.cmd=='list':
            for row in c.execute("SELECT q.test_id,q.source,q.reason,q.created_at,t.file_path,t.test_name FROM quarantine q JOIN tests t ON t.id=q.test_id WHERE q.status='active' ORDER BY q.created_at"): print(dict(row))
        else: release(c,a.test_id)
    finally: c.close()
if __name__=='__main__': main()
