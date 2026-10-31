"""Project settings shared by ingestion and analysis commands."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "projects.yaml"


def load_projects(path: str | Path = CONFIG_PATH) -> dict[str, dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or any(not isinstance(value, dict) for value in data.values()):
        raise ValueError(f"Invalid project configuration in {path}")
    return data


def project_slug(repo: str) -> str:
    owner, name = repo.split("/", 1)
    slug = owner if owner.casefold() == name.casefold() else f"{owner}-{name}"
    return slug.replace("_", "-")


def get_project(project: str | None = None, repo: str | None = None) -> tuple[str, dict]:
    projects = load_projects()
    if project:
        name = project
    elif repo:
        configured = [name for name, settings in projects.items() if settings.get("repo") == repo]
        name = configured[0] if len(configured) == 1 else project_slug(repo)
    else:
        name = "home-assistant-core"
    if name not in projects:
        if repo:
            return name, {"repo": repo, "workflow": "", "artifact_prefix": "", "branch": "main", "min_samples": 10}
        raise ValueError(f"Unknown project {name!r}; configured projects: {', '.join(sorted(projects))}")
    return name, projects[name]
