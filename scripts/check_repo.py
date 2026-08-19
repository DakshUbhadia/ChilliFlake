import os, requests, zipfile, io, sys

sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get('GITHUB_TOKEN')
if not TOKEN:
    print("Error: GITHUB_TOKEN is not set. Add it to your .env file.")
    sys.exit(1)
headers = {
    'Authorization': f"Bearer {TOKEN}",
    'Accept': 'application/vnd.github+json'
}

candidates = [sys.argv[1]] if len(sys.argv) > 1 else [
    'apache/airflow',
    'grafana/grafana',
    'ansible/ansible',
    'ray-project/ray',
    'celery/celery',
    'microsoft/playwright-python',
    'scrapy/scrapy',
    'pytest-dev/pytest'
]

print("Searching for a repository with XML test artifacts...")

def check_repo(repo):
    print(f"\n[{repo}] Checking workflows...")
    r = requests.get(f'https://api.github.com/repos/{repo}/actions/runs?per_page=10', headers=headers)
    if r.status_code != 200:
        return False
    
    runs = r.json().get('workflow_runs', [])
    if not runs:
        return False
        
    for run in runs:
        ar = requests.get(run['artifacts_url'], headers=headers).json()
        artifacts = ar.get('artifacts', [])
        
        for a in artifacts:
            name = a['name'].lower()
            if 'test' in name or 'result' in name or 'report' in name or 'coverage' in name:
                print(f"  -> Found promising artifact: '{a['name']}' in workflow '{run['name']}'")
                
                download_url = a['archive_download_url']
                dl = requests.get(download_url, headers=headers)
                if dl.status_code == 200:
                    try:
                        with zipfile.ZipFile(io.BytesIO(dl.content)) as z:
                            xml_files = [f for f in z.namelist() if f.endswith('.xml')]
                            if xml_files:
                                print(f"  ✅ SUCCESS! Found {len(xml_files)} XML files in artifact '{a['name']}'!")
                                print(f"  👉 RECOMMENDATION: Use repo '{repo}', workflow '{run['name']}', artifact-prefix '{a['name'][:5]}'")
                                return True
                            else:
                                print(f"  ❌ Downloaded, but no XML files inside (found {len(z.namelist())} other files).")
                    except Exception as e:
                        print(f"  ❌ Error reading zip: {e}")
                else:
                    print(f"  ❌ Failed to download artifact: HTTP {dl.status_code}")
    return False

for repo in candidates:
    if check_repo(repo):
        print("\n🎉 WE FOUND A MATCH! You can stop here.")
        break
