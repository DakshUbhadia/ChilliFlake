"""Orchestration: reads test_runs, computes flakiness scores per test, writes
results to flakiness_scores.
Usage: python -m src.analyzer.run_analysis [--db PATH] [--min-samples N] [--top N]
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
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
from src.analyzer.flakiness import classify, duration_coefficient_of_variation, flip_rate, wilson_lower_bound

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(name)s -- %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
logger = logging.getLogger('chilliflake.analyzer')

_FETCH_RUNS_SQL = """
SELECT tr.status, tr.duration_ms FROM test_runs tr JOIN builds b ON b.id = tr.build_id
WHERE tr.test_id = ? AND tr.status != 'skipped' ORDER BY b.created_at ASC
"""
_ALL_TEST_IDS_SQL = "SELECT DISTINCT test_id FROM test_runs"
_UPSERT_SCORE_SQL = """
INSERT OR REPLACE INTO flakiness_scores
    (test_id, sample_size, pass_rate, flip_rate, wilson_lower_bound, duration_cv, verdict, computed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""
_TEST_LABEL_SQL = "SELECT file_path, test_name FROM tests WHERE id = ?"


def _score_test(conn, test_id: int, min_samples: int, flaky_threshold: float, broken_threshold: float) -> dict:
    rows = conn.execute(_FETCH_RUNS_SQL, (test_id,)).fetchall()
    statuses = [r['status'] for r in rows]
    durations = [r['duration_ms'] for r in rows]
    sample_size = len(statuses)

    passes = sum(1 for s in statuses if s == 'passed')
    pass_rate = passes / sample_size if sample_size > 0 else 0.0

    fr = flip_rate(statuses)
    n_pairs = max(sample_size - 1, 0)
    flips = sum(1 for a, b in zip(statuses, statuses[1:]) if a != b)
    wlb = wilson_lower_bound(flips, n_pairs)

    cv = duration_coefficient_of_variation(durations)
    verdict = classify(sample_size, pass_rate, wlb, min_samples, flaky_threshold, broken_threshold)

    return {
        'test_id': test_id, 'sample_size': sample_size, 'pass_rate': pass_rate,
        'flip_rate': fr, 'wilson_lower_bound': wlb, 'duration_cv': cv, 'verdict': verdict,
    }


def run_analysis(conn, min_samples: int = 5, flaky_threshold: float = 0.15, broken_threshold: float = 0.05) -> list[dict]:
    test_ids = [r['test_id'] for r in conn.execute(_ALL_TEST_IDS_SQL)]
    logger.info('Found %d distinct test IDs to score.', len(test_ids))

    computed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    scores = []
    for test_id in test_ids:
        score = _score_test(conn, test_id, min_samples, flaky_threshold, broken_threshold)
        score['computed_at'] = computed_at
        conn.execute(_UPSERT_SCORE_SQL, (
            score['test_id'], score['sample_size'], score['pass_rate'], score['flip_rate'],
            score['wilson_lower_bound'], score['duration_cv'], score['verdict'], score['computed_at'],
        ))
        scores.append(score)

    conn.commit()
    return scores


def _print_top_n(conn, scores: list[dict], top_n: int) -> None:
    ranked = sorted(scores, key=lambda s: (s['wilson_lower_bound'], s['sample_size']), reverse=True)[:top_n]
    if not ranked:
        logger.info('No scores to display.')
        return

    logger.info('=' * 60)
    logger.info('TOP %d TESTS BY WILSON LOWER BOUND', top_n)
    logger.info('=' * 60)
    for rank, score in enumerate(ranked, 1):
        label = conn.execute(_TEST_LABEL_SQL, (score['test_id'],)).fetchone()
        file_path = label['file_path'] if label else 'unknown'
        test_name = label['test_name'] if label else 'unknown'
        logger.info('#%d  [%s] wlb=%.4f fr=%.4f n=%d  %s :: %s', rank, score['verdict'].upper(),
                    score['wilson_lower_bound'], score['flip_rate'], score['sample_size'],
                    file_path or '(no path)', test_name)
    logger.info('=' * 60)


def _log_summary(scores: list[dict]) -> None:
    verdicts = ['stable', 'flaky', 'broken', 'insufficient_data']
    counts = {v: 0 for v in verdicts}
    for s in scores:
        counts[s['verdict']] += 1
    logger.info('ANALYSIS SUMMARY')
    logger.info('  Tests analyzed : %d', len(scores))
    for verdict in verdicts:
        logger.info('  %-20s: %d', verdict, counts[verdict])


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='ChilliFlake analyzer: compute flakiness scores from ingested test_runs.')
    parser.add_argument('--db', default=None, help='SQLite DB path (overrides DB_PATH env var).')
    parser.add_argument('--min-samples', type=int, default=5, metavar='N', help='Minimum non-skipped runs before a verdict (default: 5).')
    parser.add_argument('--top', type=int, default=10, metavar='N', help='Print the N highest-wilson_lower_bound tests (default: 10).')
    parser.add_argument('--flaky-threshold', type=float, default=0.15, help='Wilson lower bound above which a test is "flaky" (default: 0.15).')
    parser.add_argument('--broken-threshold', type=float, default=0.05, help='pass_rate below which a test is "broken" (default: 0.05).')
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    conn = get_connection(args.db)
    try:
        scores = run_analysis(conn, min_samples=args.min_samples, flaky_threshold=args.flaky_threshold, broken_threshold=args.broken_threshold)
        _log_summary(scores)
        _print_top_n(conn, scores, top_n=args.top)
    finally:
        conn.close()


if __name__ == '__main__':
    main()