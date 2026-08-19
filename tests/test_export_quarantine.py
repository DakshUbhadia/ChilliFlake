from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.connection import get_connection
from src.quarantine.export_quarantine import export_quarantine, _node_id


def _make_conn() -> sqlite3.Connection:
    """In-memory DB bootstrapped with the real schema."""
    return get_connection(':memory:')


def _insert_test(conn, project: str, file_path, test_name: str) -> int:
    conn.execute(
        'INSERT INTO tests (project, file_path, test_name) VALUES (?, ?, ?)',
        (project, file_path, test_name),
    )
    row = conn.execute(
        'SELECT id FROM tests WHERE project=? AND test_name=?', (project, test_name)
    ).fetchone()
    return row['id']


def _insert_score(conn, test_id: int, verdict: str, wlb: float = 0.2,
                  flip_rate: float = 0.3, pass_rate: float = 0.8,
                  sample_size: int = 20) -> None:
    conn.execute(
        '''INSERT INTO flakiness_scores
           (test_id, sample_size, pass_rate, flip_rate, wilson_lower_bound,
            duration_cv, verdict, computed_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?, '2026-01-01T00:00:00')''',
        (test_id, sample_size, pass_rate, flip_rate, wlb, verdict),
    )
    conn.commit()


class TestVerdictRouting:
    def test_flaky_in_quarantined_not_in_attention(self, tmp_path):
        conn = _make_conn()
        tid = _insert_test(conn, 'proj', 'tests/foo.py', 'test_flaky')
        _insert_score(conn, tid, 'flaky', wlb=0.20)

        out = tmp_path / 'q.yaml'
        result = export_quarantine(conn, out)

        ids_q = [e['node_id'] for e in result['quarantined_tests']]
        ids_a = [e['node_id'] for e in result['needs_attention']]
        assert 'tests/foo.py::test_flaky' in ids_q
        assert 'tests/foo.py::test_flaky' not in ids_a

    def test_broken_in_attention_not_in_quarantined(self, tmp_path):
        conn = _make_conn()
        tid = _insert_test(conn, 'proj', 'tests/bar.py', 'test_broken')
        _insert_score(conn, tid, 'broken', pass_rate=0.02)

        out = tmp_path / 'q.yaml'
        result = export_quarantine(conn, out)

        ids_q = [e['node_id'] for e in result['quarantined_tests']]
        ids_a = [e['node_id'] for e in result['needs_attention']]
        assert 'tests/bar.py::test_broken' not in ids_q
        assert 'tests/bar.py::test_broken' in ids_a

    def test_stable_and_insufficient_in_neither(self, tmp_path):
        conn = _make_conn()
        s_id = _insert_test(conn, 'proj', 'tests/s.py', 'test_stable')
        i_id = _insert_test(conn, 'proj', 'tests/i.py', 'test_insuff')
        _insert_score(conn, s_id, 'stable')
        _insert_score(conn, i_id, 'insufficient_data')

        out = tmp_path / 'q.yaml'
        result = export_quarantine(conn, out)

        all_ids = (
            [e['node_id'] for e in result['quarantined_tests']]
            + [e['node_id'] for e in result['needs_attention']]
        )
        assert 'tests/s.py::test_stable' not in all_ids
        assert 'tests/i.py::test_insuff' not in all_ids


class TestNodeId:
    def test_with_file_path(self):
        assert _node_id('tests/foo.py', 'test_bar') == 'tests/foo.py::test_bar'

    def test_with_null_file_path(self, tmp_path):
        conn = _make_conn()
        conn.execute(
            'INSERT INTO tests (project, file_path, test_name) VALUES (?, NULL, ?)',
            ('proj', 'test_no_file'),
        )
        tid = conn.execute(
            'SELECT id FROM tests WHERE test_name=?', ('test_no_file',)
        ).fetchone()['id']
        _insert_score(conn, tid, 'flaky', wlb=0.20)

        out = tmp_path / 'q.yaml'
        result = export_quarantine(conn, out)
        node_ids = [e['node_id'] for e in result['quarantined_tests']]
        assert 'test_no_file' in node_ids
        assert any('::' not in nid for nid in node_ids if nid == 'test_no_file')

    def test_with_empty_string_file_path(self, tmp_path):
        conn = _make_conn()
        conn.execute(
            'INSERT INTO tests (project, file_path, test_name) VALUES (?, ?, ?)',
            ('proj', '', 'test_no_file_empty'),
        )
        tid = conn.execute(
            'SELECT id FROM tests WHERE test_name=?', ('test_no_file_empty',)
        ).fetchone()['id']
        _insert_score(conn, tid, 'flaky', wlb=0.20)

        out = tmp_path / 'q.yaml'
        result = export_quarantine(conn, out)
        node_ids = [e['node_id'] for e in result['quarantined_tests']]
        assert 'test_no_file_empty' in node_ids
        assert all('::' not in nid for nid in node_ids if nid == 'test_no_file_empty')


class TestNeedsAttentionOrdering:
    def test_broken_ordered_by_pass_rate_ascending(self, tmp_path):
        conn = _make_conn()
        for name, pr in [('test_bad', 0.01), ('test_worst', 0.00), ('test_medium', 0.04)]:
            tid = _insert_test(conn, 'proj', f'tests/{name}.py', name)
            _insert_score(conn, tid, 'broken', pass_rate=pr)

        out = tmp_path / 'q.yaml'
        result = export_quarantine(conn, out)

        rates = [e['pass_rate'] for e in result['needs_attention']]
        assert rates == sorted(rates), f'Expected ascending pass_rate, got: {rates}'


class TestDiffLogic:
    def test_newly_quarantined_and_released_stable(self, tmp_path, caplog):
        import logging

        conn = _make_conn()
        tid_a = _insert_test(conn, 'proj', 'tests/a.py', 'test_a')
        _insert_score(conn, tid_a, 'stable')
        tid_b = _insert_test(conn, 'proj', 'tests/b.py', 'test_b')
        _insert_score(conn, tid_b, 'flaky', wlb=0.20)

        out = tmp_path / 'q.yaml'
        prev = {
            'generated_at': '2026-01-01T00:00:00Z',
            'quarantined_tests': [{'node_id': 'tests/a.py::test_a', 'wilson_lower_bound': 0.2, 'flip_rate': 0.3, 'sample_size': 20}],
            'needs_attention': [],
        }
        out.write_text(yaml.dump(prev), encoding='utf-8')

        with caplog.at_level(logging.INFO, logger='chilliflake.quarantine'):
            result = export_quarantine(conn, out)

        log_text = caplog.text
        assert 'tests/a.py::test_a' in log_text
        assert 'tests/b.py::test_b' in log_text
        assert 'Newly quarantined  : 1' in log_text
        assert 'Released -> stable : 1' in log_text
        assert 'Released -> broken : 0' in log_text

    def test_released_to_broken(self, tmp_path, caplog):
        import logging

        conn = _make_conn()
        tid_a = _insert_test(conn, 'proj', 'tests/a.py', 'test_a')
        _insert_score(conn, tid_a, 'broken', pass_rate=0.01)

        out = tmp_path / 'q.yaml'
        prev = {
            'generated_at': '2026-01-01T00:00:00Z',
            'quarantined_tests': [{'node_id': 'tests/a.py::test_a', 'wilson_lower_bound': 0.2, 'flip_rate': 0.3, 'sample_size': 20}],
            'needs_attention': [],
        }
        out.write_text(yaml.dump(prev), encoding='utf-8')

        with caplog.at_level(logging.INFO, logger='chilliflake.quarantine'):
            export_quarantine(conn, out)

        log_text = caplog.text
        assert 'Released -> broken : 1' in log_text
        assert 'Released -> stable : 0' in log_text
