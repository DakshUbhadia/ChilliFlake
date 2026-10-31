"""Upgrade an existing ChilliFlake DB for project isolation and analysis history."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml


def _project_aliases() -> dict[str, tuple[str, str]]:
    config = Path(__file__).resolve().parents[1] / 'config' / 'projects.yaml'
    projects = yaml.safe_load(config.read_text(encoding='utf-8')) or {}
    aliases: dict[str, tuple[str, str]] = {}
    for name, settings in projects.items():
        repo = settings.get('repo')
        if not repo:
            continue
        owner, repository = repo.split('/', 1)
        for alias in {name, f'{owner}-{repository}', owner if owner.casefold() == repository.casefold() else ''}:
            if alias:
                aliases[alias] = (name, repo)
    return aliases


def migrate(db_path: str) -> None:
    path = Path(db_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            aliases = _project_aliases()
            existing_projects = {row[0] for row in conn.execute('SELECT DISTINCT project FROM tests')}
            for old_project in sorted(existing_projects):
                mapped = aliases.get(old_project)
                if not mapped or mapped[0] == old_project:
                    continue
                canonical, _ = mapped
                conflict = conn.execute('''SELECT 1 FROM tests old JOIN tests new
                    ON new.project=? AND COALESCE(new.file_path,'')=COALESCE(old.file_path,'')
                    AND new.test_name=old.test_name WHERE old.project=? LIMIT 1''',
                                        (canonical, old_project)).fetchone()
                if not conflict:
                    conn.execute('UPDATE tests SET project=? WHERE project=?', (canonical, old_project))

            build_columns = {row[1] for row in conn.execute('PRAGMA table_info(builds)')}
            if 'repo' not in build_columns:
                conn.execute('ALTER TABLE builds ADD COLUMN repo VARCHAR(255)')
            for row in conn.execute('''SELECT b.id,t.project FROM builds b
                JOIN test_runs tr ON tr.build_id=b.id JOIN tests t ON t.id=tr.test_id
                WHERE b.repo IS NULL GROUP BY b.id HAVING COUNT(DISTINCT t.project)=1''').fetchall():
                mapped = aliases.get(row[1])
                if mapped:
                    conn.execute('UPDATE builds SET repo=? WHERE id=?', (mapped[1], row[0]))
            projects = [row[0] for row in conn.execute('SELECT DISTINCT project FROM tests')]
            if len(projects) == 1 and (mapped := aliases.get(projects[0])):
                conn.execute('UPDATE builds SET repo=? WHERE repo IS NULL', (mapped[1],))

            score_columns = {row[1] for row in conn.execute('PRAGMA table_info(flakiness_scores)')}
            pk = [row[1] for row in sorted(conn.execute('PRAGMA table_info(flakiness_scores)'), key=lambda row: row[5]) if row[5]]
            if score_columns and pk != ['test_id', 'analysis_branch']:
                conn.execute('ALTER TABLE flakiness_scores RENAME TO flakiness_scores_v2')
                conn.execute('''CREATE TABLE flakiness_scores (
                    test_id INTEGER NOT NULL, analysis_branch TEXT NOT NULL DEFAULT 'dev',
                    sample_size INTEGER NOT NULL, pass_rate REAL NOT NULL, flip_rate REAL NOT NULL,
                    wilson_lower_bound REAL NOT NULL, duration_cv REAL, isolated_failures INTEGER NOT NULL DEFAULT 0,
                    commit_conflicts INTEGER NOT NULL DEFAULT 0,
                    verdict VARCHAR(20) NOT NULL CHECK(verdict IN ('stable','flaky','broken','insufficient_data')),
                    computed_at TIMESTAMP NOT NULL, FOREIGN KEY(test_id) REFERENCES tests(id),
                    PRIMARY KEY(test_id,analysis_branch))''')
                old_columns = {row[1] for row in conn.execute('PRAGMA table_info(flakiness_scores_v2)')}
                branch_expr = 'analysis_branch' if 'analysis_branch' in old_columns else "'dev'"
                isolated_expr = 'isolated_failures' if 'isolated_failures' in old_columns else '0'
                conflicts_expr = 'commit_conflicts' if 'commit_conflicts' in old_columns else '0'
                conn.execute(f'''INSERT INTO flakiness_scores
                    SELECT test_id,{branch_expr},sample_size,pass_rate,flip_rate,wilson_lower_bound,duration_cv,
                    {isolated_expr},{conflicts_expr},verdict,computed_at FROM flakiness_scores_v2''')
                conn.execute('DROP TABLE flakiness_scores_v2')
            schema = Path(__file__).resolve().parents[1] / 'db' / 'schema.sql'
            conn.executescript(schema.read_text(encoding='utf-8'))
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='db/chilliflake.db')
    migrate(parser.parse_args().db)
