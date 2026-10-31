import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db')
    parser.add_argument('--project', default='home-assistant-core')
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--branch')
    args = parser.parse_args()
    common = ['--db', args.db] if args.db else []
    scope = ['--project', args.project]
    branch = ['--branch', args.branch] if args.branch is not None else []
    db_path = Path(args.db or 'db/chilliflake.db')
    if db_path.exists():
        subprocess.run([sys.executable, 'scripts/migrate_v2.py', '--db', str(db_path)], check=True)
        subprocess.run([sys.executable, 'scripts/migrate_v3.py', '--db', str(db_path)], check=True)
    steps = [
        [sys.executable, '-m', 'src.ingestion.run_ingestion', '--runs', str(args.runs), *scope, *branch, *common],
        [sys.executable, '-m', 'src.analyzer.run_analysis', *scope, *branch, *common],
        [sys.executable, '-m', 'src.analyzer.quarantine', *common, 'sync', *scope, *branch],
        [sys.executable, '-m', 'src.analyzer.quarantine', *common, 'export', *scope, *branch,
         '--out', 'quarantine.yaml', '--format', 'yaml'],
    ]
    for step in steps:
        subprocess.run(step, check=True)


if __name__ == '__main__':
    main()
