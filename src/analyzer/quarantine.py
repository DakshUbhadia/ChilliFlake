from __future__ import annotations

import argparse
import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from db.connection import get_connection
from src.project_config import get_project

log = logging.getLogger('chilliflake.quarantine')


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _timestamp(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=datetime.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(datetime.timezone.utc)


@dataclass(frozen=True)
class Changes:
    newly_quarantined: list[int]
    released: list[int]
    regressed: list[int]

    def __getitem__(self, key):
        # Compatibility with the original count-based sync result.
        values = {'added': len(self.newly_quarantined) + len(self.regressed),
                  'released': len(self.released), 'newly_quarantined': self.newly_quarantined,
                  'regressed': self.regressed}
        return values[key]


def plan_changes(flaky_now: set[int], stable_now: set[int], active: set[int],
                 auto_active: set[int], released_before: set[int]) -> Changes:
    candidates = flaky_now - active
    return Changes(sorted(candidates - released_before),
                    sorted(auto_active & stable_now),
                    sorted(candidates & released_before))


def sync(conn, min_samples=5, max_quarantine=200, min_new_samples=5, dry_run=False,
         project: str | None = None, branch: str | None = 'dev') -> Changes:
    branch_key = branch or '*'
    scope = ['fs.analysis_branch=?']
    args: list = [branch_key]
    if project is not None:
        scope.append('t.project=?'); args.append(project)
    where = ' AND '.join(scope)
    latest_score = conn.execute(f'''SELECT MAX(fs.computed_at) FROM flakiness_scores fs
        JOIN tests t ON t.id=fs.test_id WHERE {where}''', args).fetchone()[0]
    build_filters = []
    build_args = []
    if project is not None:
        build_filters.append('t.project=?'); build_args.append(project)
    if branch is not None:
        build_filters.append('b.branch=?'); build_args.append(branch)
    build_where = (' AND '.join(build_filters) if build_filters else '1=1')
    latest_build = conn.execute(f'''SELECT MAX(b.created_at) FROM builds b
        JOIN test_runs tr ON tr.build_id=b.id JOIN tests t ON t.id=tr.test_id WHERE {build_where}''', build_args).fetchone()[0]
    if latest_build and (not latest_score or _timestamp(latest_score) < _timestamp(latest_build)):
        raise RuntimeError('Scores are stale relative to ingested builds; run analysis before sync.')

    scores = conn.execute(f'''SELECT fs.test_id,fs.verdict,fs.sample_size,fs.computed_at,fs.wilson_lower_bound
        FROM flakiness_scores fs JOIN tests t ON t.id=fs.test_id WHERE {where}''', args).fetchall()
    active_rows = conn.execute('''SELECT q.id,q.test_id,q.source,q.sample_size FROM quarantine q
        JOIN tests t ON t.id=q.test_id WHERE q.status='active' AND (? IS NULL OR t.project=?)''',
                               (project, project)).fetchall()
    flaky = {r['test_id'] for r in scores if r['verdict'] == 'flaky' and r['sample_size'] >= min_samples}
    stable = {r['test_id'] for r in scores if r['verdict'] == 'stable'}
    active = {r['test_id'] for r in active_rows}
    auto_rows = [r for r in active_rows if r['source'] == 'auto']
    auto_active = {r['test_id'] for r in auto_rows}
    released_before = {r['test_id'] for r in conn.execute('''SELECT DISTINCT q.test_id FROM quarantine q
        JOIN tests t ON t.id=q.test_id WHERE q.status='released' AND q.source='auto'
        AND (? IS NULL OR t.project=?)''', (project, project))}
    planned = plan_changes(flaky, stable, active, auto_active, released_before)
    score_by_id = {r['test_id']: r for r in scores}
    auto_by_id = {r['test_id']: r for r in auto_rows}
    eligible_releases = [tid for tid in planned.released
                         if score_by_id[tid]['sample_size'] >= auto_by_id[tid]['sample_size'] + min_new_samples]
    capacity = max(0, max_quarantine - (len(active) - len(eligible_releases)))
    rank = lambda tid: (-score_by_id[tid]['wilson_lower_bound'], -score_by_id[tid]['sample_size'], tid)
    new_candidates = sorted(planned.newly_quarantined, key=rank)
    regressed_candidates = sorted(planned.regressed, key=rank)
    # A known recurring flake is more urgent than a first-time candidate.
    regressed_ids = regressed_candidates[:capacity]
    new_ids = new_candidates[:max(0, capacity - len(regressed_ids))]
    changes = Changes(new_ids, eligible_releases, regressed_ids)
    if dry_run:
        return changes

    now = _now()
    analysis_row = conn.execute('''SELECT id FROM analysis_runs WHERE project=? AND branch IS ?
        ORDER BY id DESC LIMIT 1''', (project or '*', branch)).fetchone()
    analysis_id = analysis_row['id'] if analysis_row else None
    try:
        for tid in new_ids + regressed_ids:
            reason = f"flaky: wlb={score_by_id[tid]['wilson_lower_bound']:.3f} n={score_by_id[tid]['sample_size']}"
            conn.execute("INSERT INTO quarantine(test_id,source,reason,status,created_at,sample_size) VALUES(?,'auto',?,'active',?,?)",
                         (tid, reason, now, score_by_id[tid]['sample_size']))
            event = 'regressed' if tid in regressed_ids else 'quarantined'
            conn.execute('INSERT INTO quarantine_events(analysis_run_id,test_id,event,details,created_at) VALUES(?,?,?,?,?)',
                         (analysis_id, tid, event, reason, now))
        for tid in eligible_releases:
            row = auto_by_id[tid]
            conn.execute("UPDATE quarantine SET status='released',released_at=? WHERE id=? AND source='auto' AND status='active'",
                         (now, row['id']))
            conn.execute('INSERT INTO quarantine_events(analysis_run_id,test_id,event,details,created_at) VALUES(?,?,?,?,?)',
                         (analysis_id, tid, 'released', 'stable with sufficient new samples', now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return changes


def add_manual(conn, test_id, reason='manual'):
    conn.execute("INSERT INTO quarantine(test_id,source,reason,status,created_at,sample_size) VALUES(?,'manual',?,'active',?,0)",
                 (test_id, reason, _now()))
    conn.commit()


def release(conn, test_id):
    conn.execute("UPDATE quarantine SET status='released',released_at=? WHERE test_id=? AND status='active'",
                 (_now(), test_id))
    conn.commit()


def validate_doc(doc) -> dict:
    if not isinstance(doc, dict) or doc.get('schema_version') != 1:
        raise ValueError('quarantine document must be a mapping with schema_version: 1')
    if (not isinstance(doc.get('project'), str) or not isinstance(doc.get('generated_at'), str)
            or not isinstance(doc.get('source'), dict) or not isinstance(doc.get('thresholds'), dict)
            or not isinstance(doc.get('summary'), dict) or not isinstance(doc.get('quarantine'), list)):
        raise ValueError('quarantine document requires project, generated_at, source, thresholds, summary and quarantine')
    source, summary = doc['source'], doc['summary']
    if (not isinstance(source.get('builds_analyzed'), int) or not isinstance(source.get('window'), dict)
            or any(not isinstance(summary.get(key), int) for key in
                   ('quarantined', 'newly_quarantined', 'released', 'regressed'))):
        raise ValueError('source and summary fields have an invalid shape')
    ids = set()
    for entry in doc['quarantine']:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not entry['id']:
            raise ValueError('each quarantine entry requires a non-empty string id')
        if entry['id'] in ids:
            raise ValueError(f"duplicate quarantine id: {entry['id']}")
        if (entry.get('source') not in ('auto', 'manual')
                or not isinstance(entry.get('verdict'), str)
                or not isinstance(entry.get('sample_size'), int)
                or not isinstance(entry.get('since'), str)):
            raise ValueError(f"invalid quarantine source for {entry['id']}")
        ids.add(entry['id'])
    changes = doc.get('changes')
    if not isinstance(changes, dict) or any(not isinstance(changes.get(k, []), list)
                                             for k in ('newly_quarantined', 'released', 'regressed')):
        raise ValueError('changes must contain lists')
    return doc


def export_yaml(conn, out_path, project, changes=None, thresholds=None, source_meta=None, branch='dev'):
    rows = conn.execute('''SELECT t.file_path,t.test_name,t.project,q.source,q.reason,q.created_at,
        fs.verdict,fs.sample_size,fs.wilson_lower_bound,fs.flip_rate
        FROM quarantine q JOIN tests t ON t.id=q.test_id
        LEFT JOIN flakiness_scores fs ON fs.test_id=q.test_id AND fs.analysis_branch=?
        WHERE q.status='active' AND t.project=? AND t.file_path IS NOT NULL
        ORDER BY t.file_path,t.test_name''', (branch or '*', project)).fetchall()
    items = []
    for row in rows:
        test_id = f"{row['file_path']}::{row['test_name']}"
        items.append({'id': test_id, 'source': row['source'], 'verdict': row['verdict'] or 'flaky',
                      'since': row['created_at'], 'sample_size': row['sample_size'] or 0,
                      'wilson_lower_bound': row['wilson_lower_bound'], 'flip_rate': row['flip_rate'],
                      'reason': row['reason']})
    generated = _now()
    source_meta = source_meta or {}
    if changes is None:
        latest = conn.execute('''SELECT id FROM analysis_runs WHERE project=? AND branch IS ?
            ORDER BY id DESC LIMIT 1''', (project, branch)).fetchone()
        event_rows = conn.execute('''SELECT e.event,e.test_id,t.file_path,t.test_name FROM quarantine_events e
            JOIN tests t ON t.id=e.test_id WHERE e.analysis_run_id=? AND t.project=? AND t.file_path IS NOT NULL''',
                                  (latest['id'] if latest else -1, project)).fetchall()
        grouped = {'quarantined': [], 'released': [], 'regressed': []}
        for row in event_rows:
            if row['event'] in grouped:
                grouped[row['event']].append(f"{row['file_path']}::{row['test_name']}")
        changes = {'newly_quarantined': sorted(grouped['quarantined']),
                   'released': sorted(grouped['released']), 'regressed': sorted(grouped['regressed'])}
    else:
        def node_ids(ids):
            if not ids:
                return []
            marks = ','.join('?' for _ in ids)
            return [f"{r['file_path']}::{r['test_name']}" for r in conn.execute(
                f'SELECT file_path,test_name FROM tests WHERE project=? AND id IN ({marks}) ORDER BY file_path,test_name',
                [project, *ids])]
        changes = {'newly_quarantined': node_ids(changes.newly_quarantined),
                   'released': node_ids(changes.released), 'regressed': node_ids(changes.regressed)}
    doc = {'schema_version': 1, 'project': project, 'generated_at': generated,
           'source': {'repo': source_meta.get('repo'), 'branch': source_meta.get('branch'),
                      'builds_analyzed': source_meta.get('builds_analyzed', 0),
                      'window': source_meta.get('window', {'from': None, 'to': None})},
           'thresholds': thresholds or {},
           'summary': {'quarantined': len(items), 'newly_quarantined': len(changes['newly_quarantined']),
                       'released': len(changes['released']), 'regressed': len(changes['regressed'])},
           'quarantine': items,
           'changes': changes}
    validate_doc(doc)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    try:
        temp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return doc


def export_json(conn, out, project=None):
    rows = conn.execute('''SELECT t.file_path,t.test_name,q.source,q.reason FROM quarantine q
        JOIN tests t ON t.id=q.test_id WHERE q.status='active' AND (? IS NULL OR t.project=?) ORDER BY t.file_path,t.test_name''',
                        (project, project)).fetchall()
    payload = {'generated_at': _now(), 'quarantined_tests': [
        {'node_id': f"{r['file_path']}::{r['test_name']}" if r['file_path'] else r['test_name'],
         'source': r['source'], 'reason': r['reason']} for r in rows]}
    Path(out).write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    return payload


def export(conn, out):
    """Backward-compatible JSON export for existing callers."""
    return export_json(conn, out)


def diff_files(old_path, new_path, fail_on_regressed=False):
    old = validate_doc(yaml.safe_load(Path(old_path).read_text(encoding='utf-8')))
    new = validate_doc(yaml.safe_load(Path(new_path).read_text(encoding='utf-8')))
    if old['project'] != new['project']:
        raise ValueError('cannot diff quarantine exports for different projects')
    old_ids = {item['id'] for item in old['quarantine']}
    new_ids = {item['id'] for item in new['quarantine']}
    result = {'added': sorted(new_ids - old_ids), 'removed': sorted(old_ids - new_ids),
              'regressed': sorted(new.get('changes', {}).get('regressed', []))}
    print(json.dumps(result, indent=2))
    return 1 if fail_on_regressed and result['regressed'] else 0


def main():
    parser = argparse.ArgumentParser(description='Maintain project-scoped flaky test quarantine.')
    parser.add_argument('--db', default=None)
    sub = parser.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('sync'); s.add_argument('--project', default='home-assistant-core'); s.add_argument('--branch', default=None)
    s.add_argument('--min-samples', type=int, default=None); s.add_argument('--max-quarantine', type=int, default=200)
    s.add_argument('--min-new-samples', type=int, default=5); s.add_argument('--dry-run', action='store_true')
    e = sub.add_parser('export'); e.add_argument('--project', default='home-assistant-core'); e.add_argument('--branch', default=None)
    e.add_argument('--out', default='quarantine.yaml'); e.add_argument('--format', choices=('yaml', 'json'), default='yaml')
    d = sub.add_parser('diff'); d.add_argument('old'); d.add_argument('new'); d.add_argument('--fail-on-regressed', action='store_true')
    sub.add_parser('list'); r = sub.add_parser('release'); r.add_argument('test_id', type=int)
    a = parser.parse_args()
    if a.cmd == 'diff':
        raise SystemExit(diff_files(a.old, a.new, a.fail_on_regressed))
    conn = get_connection(a.db)
    try:
        if a.cmd == 'sync':
            name, settings = get_project(a.project)
            branch = settings.get('branch') if a.branch is None else a.branch
            branch = branch or None
            min_samples = settings.get('min_samples', 5) if a.min_samples is None else a.min_samples
            changes = sync(conn, min_samples, a.max_quarantine, a.min_new_samples, a.dry_run, name, branch)
            print({'newly_quarantined': changes.newly_quarantined, 'released': changes.released,
                   'regressed': changes.regressed, 'dry_run': a.dry_run})
        elif a.cmd == 'export':
            name, settings = get_project(a.project)
            branch = settings.get('branch') if a.branch is None else a.branch
            branch = branch or None
            if a.format == 'json': export_json(conn, a.out, name)
            else:
                build_count = conn.execute('''SELECT COUNT(DISTINCT b.id) FROM builds b
                    JOIN test_runs tr ON tr.build_id=b.id JOIN tests t ON t.id=tr.test_id
                    WHERE t.project=? AND (? IS NULL OR b.branch=?)''', (name, branch, branch)).fetchone()[0]
                analysis = conn.execute('''SELECT config_json FROM analysis_runs WHERE project=? AND branch IS ?
                    ORDER BY id DESC LIMIT 1''', (name, branch)).fetchone()
                thresholds = json.loads(analysis['config_json']) if analysis else {'min_samples': settings.get('min_samples', 5)}
                window = conn.execute('''SELECT MIN(b.created_at) AS start,MAX(b.created_at) AS end FROM builds b
                    JOIN test_runs tr ON tr.build_id=b.id JOIN tests t ON t.id=tr.test_id
                    WHERE t.project=? AND (? IS NULL OR b.branch=?)''', (name, branch, branch)).fetchone()
                export_yaml(conn, a.out, name, source_meta={'repo': settings.get('repo'), 'branch': branch,
                             'builds_analyzed': build_count,
                             'window': {'from': window['start'], 'to': window['end']}},
                            thresholds=thresholds, branch=branch)
        elif a.cmd == 'list':
            for row in conn.execute("SELECT q.test_id,q.source,q.reason,q.created_at,t.project,t.file_path,t.test_name FROM quarantine q JOIN tests t ON t.id=q.test_id WHERE q.status='active' ORDER BY q.created_at"):
                print(dict(row))
        else: release(conn, a.test_id)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
