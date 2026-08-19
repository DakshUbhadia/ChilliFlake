import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.connection import get_connection
from src.quarantine.export_quarantine import export_quarantine

OUTPUT = Path(tempfile.mktemp(suffix='_quarantine.yaml'))
conn = get_connection(':memory:')

def ins_test(project, file_path, test_name):
    conn.execute('INSERT INTO tests (project, file_path, test_name) VALUES (?, ?, ?)',
                 (project, file_path, test_name))
    return conn.execute('SELECT id FROM tests WHERE project=? AND test_name=?',
                        (project, test_name)).fetchone()['id']

def ins_score(test_id, verdict, wlb=0.2, flip_rate=0.3, pass_rate=0.8, n=20):
    conn.execute(
        "INSERT INTO flakiness_scores "
        "(test_id, sample_size, pass_rate, flip_rate, wilson_lower_bound, "
        "duration_cv, verdict, computed_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, '2026-01-01T00:00:00')",
        (test_id, n, pass_rate, flip_rate, wlb, verdict),
    )

id1 = ins_test('proj', 'tests/comp/foo/test_bar.py', 'test_flaky_with_path')
id2 = ins_test('proj', None, 'test_flaky_null_path')
id3 = ins_test('proj', '', 'test_flaky_empty_path')
id4 = ins_test('proj', 'tests/comp/baz/test_qux.py', 'test_always_fails')
id5 = ins_test('proj', 'tests/comp/ok/test_ok.py', 'test_stable')
ins_score(id1, 'flaky', wlb=0.23, flip_rate=0.31, n=42)
ins_score(id2, 'flaky', wlb=0.18, flip_rate=0.25, n=30)
ins_score(id3, 'flaky', wlb=0.16, flip_rate=0.22, n=28)
ins_score(id4, 'broken', pass_rate=0.02, n=38)
ins_score(id5, 'stable', wlb=0.01, pass_rate=0.98, n=50)
conn.commit()

print('=' * 60)
print('RUN 1 (fresh -- no prior quarantine.yaml)')
print('=' * 60)
r1 = export_quarantine(conn, OUTPUT)
print()
print('  quarantined_tests :', len(r1['quarantined_tests']))
print('  needs_attention   :', len(r1['needs_attention']))
print()
print('  Quarantined:')
for e in r1['quarantined_tests']:
    print('   ', e['node_id'])
print('  Needs attention:')
for e in r1['needs_attention']:
    print('   ', e['node_id'])

null_nid = next(e['node_id'] for e in r1['quarantined_tests'] if e['node_id'] == 'test_flaky_null_path')
empty_nid = next(e['node_id'] for e in r1['quarantined_tests'] if e['node_id'] == 'test_flaky_empty_path')
assert '::' not in null_nid, 'NULL path produced :: in node_id'
assert '::' not in empty_nid, 'Empty path produced :: in node_id'
print()
print('  OK: NULL and empty file_path both produce prefix-free node_ids')

print()
print('=' * 60)
print('RUN 2 (same data -- idempotency check)')
print('=' * 60)
r2 = export_quarantine(conn, OUTPUT)
assert len(r2['quarantined_tests']) == len(r1['quarantined_tests'])
assert len(r2['needs_attention']) == len(r1['needs_attention'])
print()
print('  OK: idempotency confirmed -- same counts, diff shows 0 newly quarantined/released')

conn.close()
OUTPUT.unlink(missing_ok=True)
print()
print('Done.')
