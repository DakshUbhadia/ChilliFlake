from __future__ import annotations

import argparse
import datetime
import logging
import sys
from itertools import groupby
from operator import itemgetter
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from db.connection import get_connection
from src.analyzer.flakiness import classify, duration_coefficient_of_variation, flip_count_and_rate, isolated_failure_count, wilson_lower_bound

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(name)s -- %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
logger = logging.getLogger('chilliflake.analyzer')

_FETCH_ALL_SQL = """SELECT tr.test_id, tr.status, tr.duration_ms, b.commit_sha
FROM test_runs tr JOIN builds b ON b.id = tr.build_id
WHERE tr.status != 'skipped' {branch_clause}
ORDER BY tr.test_id, b.created_at, b.id, tr.id"""
_CONFLICTS_SQL = """SELECT test_id, COUNT(*) AS commit_conflicts FROM (
SELECT tr.test_id, b.commit_sha FROM test_runs tr JOIN builds b ON b.id=tr.build_id
WHERE tr.status != 'skipped' {branch_clause}
GROUP BY tr.test_id, b.commit_sha HAVING COUNT(DISTINCT tr.status)>1
) GROUP BY test_id"""
_FETCH_BRANCH_SQL = """SELECT tr.test_id,tr.status,tr.duration_ms,b.commit_sha
FROM builds b CROSS JOIN test_runs tr INDEXED BY idx_test_runs_build_test
WHERE tr.build_id=b.id AND b.branch=? AND tr.status!='skipped'
ORDER BY tr.test_id,b.created_at,b.id,tr.id"""
_CONFLICTS_BRANCH_SQL = """SELECT test_id,COUNT(*) AS commit_conflicts FROM (
SELECT tr.test_id,b.commit_sha FROM builds b CROSS JOIN test_runs tr INDEXED BY idx_test_runs_build_test
WHERE tr.build_id=b.id AND b.branch=? AND tr.status!='skipped'
GROUP BY tr.test_id,b.commit_sha HAVING COUNT(DISTINCT tr.status)>1
) GROUP BY test_id"""

def _score_runs(test_id: int, statuses: list[str], durations: list[int], commit_conflicts: int = 0,
                min_samples: int = 5, flaky_threshold: float = .15, broken_threshold: float = .05,
                min_isolated: int = 2) -> dict:
    n = len(statuses)
    passes = sum(s == 'passed' for s in statuses)
    pass_rate = passes / n if n else 0.0
    flips, rate = flip_count_and_rate(statuses)
    wlb = wilson_lower_bound(flips, max(n - 1, 0))
    isolated = isolated_failure_count(statuses)
    verdict = classify(n, pass_rate, wlb, min_samples, flaky_threshold, broken_threshold,
                       isolated_failures=isolated, commit_conflicts=commit_conflicts, min_isolated=min_isolated)
    return {'test_id': test_id, 'sample_size': n, 'pass_rate': pass_rate, 'flip_rate': rate,
            'wilson_lower_bound': wlb, 'duration_cv': duration_coefficient_of_variation(durations),
            'isolated_failures': isolated, 'commit_conflicts': commit_conflicts, 'verdict': verdict}

def _score_test(conn, test_id: int, min_samples: int, flaky_threshold: float, broken_threshold: float) -> dict:
    rows = conn.execute("SELECT tr.status,tr.duration_ms FROM test_runs tr JOIN builds b ON b.id=tr.build_id WHERE tr.test_id=? AND tr.status!='skipped' ORDER BY b.created_at,b.id,tr.id", (test_id,)).fetchall()
    return _score_runs(test_id, [r['status'] for r in rows], [r['duration_ms'] for r in rows],
                       min_samples=min_samples, flaky_threshold=flaky_threshold, broken_threshold=broken_threshold)

def run_analysis(conn, min_samples: int = 5, flaky_threshold: float = .15, broken_threshold: float = .05,
                 branch: str | None = 'dev', min_isolated: int = 2) -> list[dict]:
    branch = branch or None
    computed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if branch is not None:
        fetch_sql, conflicts_sql, params = _FETCH_BRANCH_SQL, _CONFLICTS_BRANCH_SQL, (branch,)
    else:
        fetch_sql, conflicts_sql, params = _FETCH_ALL_SQL.format(branch_clause=''), _CONFLICTS_SQL.format(branch_clause=''), ()
    conflicts = {r['test_id']: r['commit_conflicts'] for r in conn.execute(conflicts_sql, params)}
    rows = conn.execute(fetch_sql, params)
    scores = []
    for test_id, grouped in groupby(rows, key=itemgetter('test_id')):
        group = list(grouped)
        score = _score_runs(test_id, [r['status'] for r in group], [r['duration_ms'] for r in group],
                            conflicts.get(test_id, 0), min_samples, flaky_threshold, broken_threshold, min_isolated)
        score['computed_at'] = computed_at
        score['analysis_branch'] = branch if branch is not None else '*'
        conn.execute("""INSERT OR REPLACE INTO flakiness_scores
        (test_id,analysis_branch,sample_size,pass_rate,flip_rate,wilson_lower_bound,duration_cv,isolated_failures,commit_conflicts,verdict,computed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (score['test_id'],score['analysis_branch'],score['sample_size'],score['pass_rate'],score['flip_rate'],score['wilson_lower_bound'],score['duration_cv'],score['isolated_failures'],score['commit_conflicts'],score['verdict'],computed_at))
        scores.append(score)
    conn.execute('DELETE FROM flakiness_scores WHERE computed_at != ?', (computed_at,))
    conn.commit()
    return scores

def _print_top_n(conn, scores: list[dict], top_n: int) -> None:
    ranked = sorted(scores, key=lambda s: (s['wilson_lower_bound'], s['sample_size']), reverse=True)[:top_n]
    labels = {r['id']: r for r in conn.execute('SELECT id,file_path,test_name FROM tests')}
    for rank, score in enumerate(ranked, 1):
        label = labels.get(score['test_id'])
        logger.info('#%d [%s] wlb=%.4f fr=%.4f n=%d %s :: %s', rank, score['verdict'].upper(), score['wilson_lower_bound'], score['flip_rate'], score['sample_size'], label['file_path'] or '(no path)' if label else 'unknown', label['test_name'] if label else 'unknown')

def _log_summary(scores: list[dict]) -> None:
    counts = {v: sum(s['verdict'] == v for s in scores) for v in ('stable','flaky','broken','insufficient_data')}
    logger.info('ANALYSIS SUMMARY tests=%d %s', len(scores), counts)

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Compute flakiness scores from ingested test runs.')
    parser.add_argument('--db', default=None)
    parser.add_argument('--min-samples', type=int, default=5)
    parser.add_argument('--top', type=int, default=10)
    parser.add_argument('--flaky-threshold', type=float, default=.15)
    parser.add_argument('--broken-threshold', type=float, default=.05)
    parser.add_argument('--branch', default='dev', help="Branch to score; empty string scores all branches.")
    parser.add_argument('--min-isolated', type=int, default=2)
    return parser.parse_args()

def main() -> None:
    args = _parse_args()
    branch = args.branch or None
    logger.info('Config branch=%s min_samples=%d min_isolated=%d', branch or '(all)', args.min_samples, args.min_isolated)
    conn = get_connection(args.db)
    try:
        scores = run_analysis(conn, args.min_samples, args.flaky_threshold, args.broken_threshold, branch, args.min_isolated)
        _log_summary(scores)
        _print_top_n(conn, scores, args.top)
    finally:
        conn.close()

if __name__ == '__main__':
    main()
