import json, os
from pathlib import Path

def pytest_collection_modifyitems(config, items):
    filename=os.environ.get('CHILLIFLAKE_QUARANTINE_FILE','quarantine.json')
    path=Path(filename)
    if not path.exists(): return
    data=json.loads(path.read_text(encoding='utf-8'))
    ids={entry['node_id'] for entry in data.get('quarantined_tests',[])}
    mode=os.environ.get('CHILLIFLAKE_QUARANTINE_MODE','xfail').lower()
    import pytest
    marker=pytest.mark.skip(reason='ChilliFlake quarantine') if mode=='skip' else pytest.mark.xfail(reason='ChilliFlake quarantine',strict=False,run=True)
    for item in items:
        if item.nodeid.replace('\\','/') in ids: item.add_marker(marker)
