from pathlib import Path

from streamlit.testing.v1 import AppTest

from db.connection import get_connection


def test_dashboard_starts_with_an_empty_current_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    get_connection(str(db_path)).close()
    monkeypatch.setenv("DB_PATH", str(db_path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"))
    app.run(timeout=10)
    assert not app.exception
    assert any("No projects" in message.value for message in app.info)
