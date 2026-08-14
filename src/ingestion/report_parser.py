import logging
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
logger = logging.getLogger(__name__)
VALID_STATUSES = frozenset({'passed', 'failed', 'timedOut', 'skipped', 'interrupted'})
PROJECT = 'home-assistant-core'

def extract_xml_files_from_zip(zip_path: str | Path) -> tuple[Path, list[Path]]:
    zip_path = Path(zip_path)
    temp_dir = Path(tempfile.mkdtemp(prefix='chilliflake_xml_'))
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(temp_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    xml_paths = sorted(temp_dir.rglob('*.xml'))
    if not xml_paths:
        logger.warning('ZIP %s contained no .xml files.', zip_path.name)
    return (temp_dir, xml_paths)

def _get_or_insert_build(conn: Any, github_run_id: int, commit_sha: str, branch: str | None, created_at: str, build_id_cache: dict[int, int]) -> int:
    if github_run_id in build_id_cache:
        return build_id_cache[github_run_id]
    conn.execute('INSERT OR IGNORE INTO builds (github_run_id, commit_sha, branch, created_at) VALUES (?, ?, ?, ?)', (github_run_id, commit_sha, branch, created_at))
    row = conn.execute('SELECT id FROM builds WHERE github_run_id = ?', (github_run_id,)).fetchone()
    build_id_cache[github_run_id] = row['id']
    return row['id']

def _get_or_insert_test(conn: Any, project: str, file_path: str | None, test_name: str, test_id_cache: dict[tuple[str, str | None, str], int]) -> int:
    cache_key = (project, file_path, test_name)
    if cache_key in test_id_cache:
        return test_id_cache[cache_key]
    conn.execute('INSERT OR IGNORE INTO tests (project, file_path, test_name) VALUES (?, ?, ?)', (project, file_path, test_name))
    if file_path is None:
        row = conn.execute('SELECT id FROM tests WHERE project = ? AND file_path IS NULL AND test_name = ?', (project, test_name)).fetchone()
    else:
        row = conn.execute('SELECT id FROM tests WHERE project = ? AND file_path = ? AND test_name = ?', (project, file_path, test_name)).fetchone()
    test_id_cache[cache_key] = row['id']
    return row['id']

def _resolve_file_path(testcase: ET.Element, classname: str) -> str | None:
    file_attr = testcase.get('file')
    if file_attr:
        return file_attr
    classname = classname.strip()
    return classname.replace('.', '/') + '.py' if classname else None

def _determine_status(testcase: ET.Element) -> str:
    if testcase.find('failure') is not None or testcase.find('error') is not None:
        return 'failed'
    if testcase.find('skipped') is not None:
        return 'skipped'
    return 'passed'

def ingest_run(conn: Any, run_meta: dict[str, Any], xml_paths: list[Path], test_id_cache: dict[tuple[str, str | None, str], int], build_id_cache: dict[int, int]) -> dict[str, int]:
    github_run_id = run_meta['github_run_id']
    counts = {'inserted': 0, 'skipped_bad_status': 0, 'files_parsed': 0, 'files_skipped': 0, 'already_ingested': 0}
    if not xml_paths:
        logger.warning('Run %d: no XML files provided.', github_run_id)
        return counts
    try:
        build_id = _get_or_insert_build(conn, github_run_id, run_meta.get('commit_sha', 'unknown'), run_meta.get('branch'), run_meta.get('created_at', ''), build_id_cache)
        if conn.execute('SELECT 1 FROM test_runs WHERE build_id = ? LIMIT 1', (build_id,)).fetchone():
            logger.info('Run %d: already ingested (build_id=%d). Skipping.', github_run_id, build_id)
            counts['already_ingested'] = 1
            return counts
        for xml_path in xml_paths:
            try:
                root = ET.parse(xml_path).getroot()
            except (ET.ParseError, OSError) as exc:
                logger.warning('Run %d: skipping unreadable %s (%s).', github_run_id, xml_path.name, exc)
                counts['files_skipped'] += 1
                continue
            if root.tag == 'testsuites':
                testsuites = root.findall('testsuite')
            elif root.tag == 'testsuite':
                testsuites = [root]
            else:
                testsuites = root.findall('.//testsuite')
                if not testsuites:
                    logger.warning('Run %d: %s has no <testsuite> data. Skipping.', github_run_id, xml_path.name)
                    counts['files_skipped'] += 1
                    continue
            for testsuite in testsuites:
                for testcase in testsuite.findall('testcase'):
                    classname = testcase.get('classname', '')
                    name = testcase.get('name', '').strip() or classname or 'unknown'
                    try:
                        duration_ms = int(round(float(testcase.get('time', '0')) * 1000))
                    except (ValueError, TypeError):
                        duration_ms = 0
                    status = _determine_status(testcase)
                    if status not in VALID_STATUSES:
                        logger.error('Run %d | %s::%s: invalid status %r — bug, skipping.', github_run_id, classname, name, status)
                        counts['skipped_bad_status'] += 1
                        continue
                    file_path = _resolve_file_path(testcase, classname)
                    test_id = _get_or_insert_test(conn, PROJECT, file_path, name, test_id_cache)
                    conn.execute('INSERT INTO test_runs (build_id, test_id, status, reported_flaky, duration_ms, retries) VALUES (?, ?, ?, 0, ?, 0)', (build_id, test_id, status, duration_ms))
                    counts['inserted'] += 1
            counts['files_parsed'] += 1
        conn.commit()
        logger.info('Run %d: %d inserted, %d files parsed, %d skipped, %d bad-status.', github_run_id, counts['inserted'], counts['files_parsed'], counts['files_skipped'], counts['skipped_bad_status'])
    except Exception:
        conn.rollback()
        logger.exception('Run %d failed — rolled back.', github_run_id)
        raise
    return counts