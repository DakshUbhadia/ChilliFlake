"""Pytest consumer for ChilliFlake YAML and legacy JSON quarantine files."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger('chilliflake.pytest_plugin')


def _load_ids(path: Path) -> set[str]:
    if path.suffix.lower() in ('.yaml', '.yml'):
        import yaml
        doc = yaml.safe_load(path.read_text(encoding='utf-8'))
        if (not isinstance(doc, dict) or doc.get('schema_version') != 1
                or not isinstance(doc.get('project'), str) or not isinstance(doc.get('generated_at'), str)
                or not isinstance(doc.get('source'), dict) or not isinstance(doc.get('thresholds'), dict)
                or not isinstance(doc.get('summary'), dict) or not isinstance(doc.get('quarantine'), list)):
            raise ValueError('invalid ChilliFlake YAML schema')
        summary, source = doc['summary'], doc['source']
        if (not isinstance(source.get('builds_analyzed'), int) or not isinstance(source.get('window'), dict)
                or any(not isinstance(summary.get(key), int) for key in
                       ('quarantined', 'newly_quarantined', 'released', 'regressed'))):
            raise ValueError('invalid ChilliFlake YAML source or summary')
        entries = doc['quarantine']
        ids = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
                    or entry.get('source') not in ('auto', 'manual')
                    or not isinstance(entry.get('verdict'), str)
                    or not isinstance(entry.get('sample_size'), int)
                    or not isinstance(entry.get('since'), str)):
                raise ValueError('invalid quarantine entry')
            if entry['id'] in ids:
                raise ValueError(f"duplicate quarantine id: {entry['id']}")
            ids.add(entry['id'])
        changes = doc.get('changes')
        if not isinstance(changes, dict) or any(not isinstance(changes.get(key), list)
                                                 for key in ('newly_quarantined', 'released', 'regressed')):
            raise ValueError('invalid ChilliFlake YAML changes')
        return ids
    if path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('quarantined_tests', []), list):
            raise ValueError('invalid legacy JSON quarantine schema')
        return {entry['node_id'] for entry in data.get('quarantined_tests', [])
                if isinstance(entry, dict) and isinstance(entry.get('node_id'), str)}
    raise ValueError(f'unsupported quarantine file extension: {path.suffix}')


def pytest_collection_modifyitems(config, items):
    filename = os.environ.get('CHILLIFLAKE_QUARANTINE_FILE', 'quarantine.yaml')
    path = Path(filename)
    if not path.exists():
        log.warning('Quarantine file %s is missing; no tests will be quarantined.', path)
        return
    try:
        ids = _load_ids(path)
    except Exception as exc:
        log.warning('Quarantine file %s is invalid (%s); no tests will be quarantined.', path, exc)
        return
    mode = os.environ.get('CHILLIFLAKE_QUARANTINE_MODE', 'xfail').lower()
    try:
        import pytest
        marker = (pytest.mark.skip(reason='ChilliFlake quarantine') if mode == 'skip'
                  else pytest.mark.xfail(reason='ChilliFlake quarantine', strict=False, run=True))
        for item in items:
            if item.nodeid.replace('\\', '/') in ids:
                item.add_marker(marker)
    except Exception as exc:
        log.warning('Could not apply ChilliFlake quarantine markers: %s', exc)
