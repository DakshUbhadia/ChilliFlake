"""Explore repositories to find workflows that publish downloadable JUnit XML."""
import io
import os
import sys
import zipfile
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.environ.get('GITHUB_TOKEN')
if not token:
    raise SystemExit('GITHUB_TOKEN is not set. Add it to .env.')
headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
candidates = [sys.argv[1]] if len(sys.argv) > 1 else [
    'dask/dask','apache/airflow','grafana/grafana','ansible/ansible','ray-project/ray',
    'celery/celery','microsoft/playwright-python','scrapy/scrapy','pytest-dev/pytest']

def check_repo(repo):
    response=requests.get(f'https://api.github.com/repos/{repo}/actions/runs?per_page=10',headers=headers,timeout=20)
    request_headers=headers
    if response.status_code == 401:
        # Public repositories can be inspected without auth; a stale local PAT
        # should not prevent read-only discovery from completing.
        request_headers={'Accept':'application/vnd.github+json'}
        response=requests.get(f'https://api.github.com/repos/{repo}/actions/runs?per_page=10',headers=request_headers,timeout=20)
    if response.status_code != 200:
        print(f'{repo}: workflow lookup failed ({response.status_code}): {response.text[:300]}')
        return False
    runs=response.json().get('workflow_runs',[])
    print(f'{repo}: inspected {len(runs)} recent workflow run(s)')
    seen=[]
    for run in runs:
        artifact_response=requests.get(run['artifacts_url'],headers=request_headers,timeout=20)
        if artifact_response.status_code != 200:
            print(f"{repo}: artifact lookup failed for run {run['id']} ({artifact_response.status_code})")
            continue
        artifacts=artifact_response.json().get('artifacts',[])
        for artifact in artifacts:
            name=artifact.get('name','').lower()
            seen.append(artifact.get('name',''))
            if not any(word in name for word in ('test','result','report','coverage')): continue
            download=requests.get(artifact['archive_download_url'],headers=request_headers,timeout=30)
            if download.status_code != 200: continue
            try:
                with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                    xml=[f for f in archive.namelist() if f.endswith('.xml')]
                if xml:
                    print(f'{repo}: workflow={run.get("name")} artifact={artifact["name"]} XML files={len(xml)}')
                    return True
            except zipfile.BadZipFile:
                continue
    print(f'{repo}: no downloadable JUnit XML artifact found; observed artifact names={sorted(set(seen))}')
    return False

for candidate in candidates:
    if check_repo(candidate): break
