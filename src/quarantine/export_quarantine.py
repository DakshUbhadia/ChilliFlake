from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import yaml
from db.connection import get_connection

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s -- %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
logger = logging.getLogger('chilliflake.quarantine')

_FLAKY_SQL = """
SELECT fs.wilson_lower_bound, fs.flip_rate, fs.sample_size,
       t.file_path, t.test_name
FROM flakiness_scores fs
JOIN tests t ON t.id = fs.test_id
WHERE fs.verdict = 'flaky'
ORDER BY fs.wilson_lower_bound DESC
"""

_BROKEN_SQL = """
SELECT fs.pass_rate, fs.sample_size,
       t.file_path, t.test_name
FROM flakiness_scores fs
JOIN tests t ON t.id = fs.test_id
WHERE fs.verdict = 'broken'
ORDER BY fs.pass_rate ASC
"""


def _node_id(file_path: str | None, test_name: str) -> str:
    return f"{file_path}::{test_name}" if file_path else test_name


def _read_previous(output_path: Path) -> tuple[set[str], set[str]]:
    """Return (old_quarantined_node_ids, old_attention_node_ids) from existing YAML, or empty sets."""
    if not output_path.exists():
        return set(), set()
    try:
        data = yaml.safe_load(output_path.read_text(encoding='utf-8')) or {}
        quarantined = {e['node_id'] for e in data.get('quarantined_tests', [])}
        attention = {e['node_id'] for e in data.get('needs_attention', [])}
        return quarantined, attention
    except Exception as exc:
        logger.warning('Could not read previous quarantine file %s: %s', output_path, exc)
        return set(), set()


def _log_diff(old_quarantined: set[str], old_attention: set[str], new_quarantined: set[str], new_attention: set[str]) -> None:
    newly = new_quarantined - old_quarantined
    released = old_quarantined - new_quarantined
    still = old_quarantined & new_quarantined

    released_to_broken = released & new_attention
    released_to_stable = released - new_attention

    logger.info('Quarantine diff:')
    logger.info('  Newly quarantined  : %d', len(newly))
    logger.info('  Released -> stable : %d', len(released_to_stable))
    logger.info('  Released -> broken : %d  (check needs_attention — these got worse, not better)', len(released_to_broken))
    logger.info('  Still quarantined  : %d', len(still))

    for nid in sorted(newly):
        logger.info('    [NEW]     %s', nid)
    for nid in sorted(released_to_stable):
        logger.info('    [STABLE]  %s', nid)
    for nid in sorted(released_to_broken):
        logger.info('    [BROKEN]  %s', nid)


def export_quarantine(conn: Any, output_path: Path) -> dict:
    old_quarantined, old_attention = _read_previous(output_path)

    flaky_rows = conn.execute(_FLAKY_SQL).fetchall()
    broken_rows = conn.execute(_BROKEN_SQL).fetchall()

    quarantined_tests = [
        {
            'node_id': _node_id(row['file_path'], row['test_name']),
            'wilson_lower_bound': round(row['wilson_lower_bound'], 6),
            'flip_rate': round(row['flip_rate'], 6),
            'sample_size': row['sample_size'],
        }
        for row in flaky_rows
    ]

    needs_attention = [
        {
            'node_id': _node_id(row['file_path'], row['test_name']),
            'pass_rate': round(row['pass_rate'], 6),
            'sample_size': row['sample_size'],
        }
        for row in broken_rows
    ]

    new_quarantined = {e['node_id'] for e in quarantined_tests}
    new_attention = {e['node_id'] for e in needs_attention}

    _log_diff(old_quarantined, old_attention, new_quarantined, new_attention)

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    payload = {
        'generated_at': generated_at,
        'quarantined_tests': quarantined_tests,
        'needs_attention': needs_attention,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.dump(payload, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding='utf-8',
    )
    logger.info('Wrote %d quarantined test(s) and %d needing attention to %s',
                len(quarantined_tests), len(needs_attention), output_path)
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='ChilliFlake quarantine exporter: writes quarantine.yaml from flakiness_scores.')
    parser.add_argument('--db', default=None, help='SQLite DB path (overrides DB_PATH env var).')
    parser.add_argument('--output', default='quarantine.yaml', help='Output YAML path (default: quarantine.yaml).')
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    conn = get_connection(args.db)
    try:
        export_quarantine(conn, Path(args.output))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
