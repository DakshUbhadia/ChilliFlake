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
    'apache/airflow','grafana/grafana','ansible/ansible','ray-project/ray',
    'celery/celery','microsoft/playwright-python','scrapy/scrapy','pytest-dev/pytest']

def check_repo(repo):
    response=requests.get(f'https://api.github.com/repos/{repo}/actions/runs?per_page=10',headers=headers)
    if response.status_code != 200: return False
    for run in response.json().get('workflow_runs',[]):
        artifacts=requests.get(run['artifacts_url'],headers=headers).json().get('artifacts',[])
        for artifact in artifacts:
            name=artifact.get('name','').lower()
            if not any(word in name for word in ('test','result','report','coverage')): continue
            download=requests.get(artifact['archive_download_url'],headers=headers)
            if download.status_code != 200: continue
            try:
                with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                    xml=[f for f in archive.namelist() if f.endswith('.xml')]
                if xml:
                    print(f'{repo}: workflow={run.get("name")} artifact={artifact["name"]} XML files={len(xml)}')
                    return True
            except zipfile.BadZipFile:
                continue
    return False

for candidate in candidates:
    if check_repo(candidate): break
