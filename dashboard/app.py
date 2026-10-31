"""Interactive dashboard for scored tests and quarantine management."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from src.analyzer.quarantine import add_manual, release


def configured_db_path() -> Path:
    configured = Path(os.environ.get("DB_PATH", "db/chilliflake.db"))
    return configured if configured.is_absolute() else ROOT / configured


DB_PATH = configured_db_path()


@st.cache_data(ttl=30)
def load(sql: str, params: tuple = ()) -> pd.DataFrame:
    uri = f"file:{DB_PATH.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def refresh() -> None:
    st.cache_data.clear()
    st.rerun()


st.set_page_config(page_title="ChilliFlake - Flaky Test Monitor", page_icon="🌶️", layout="wide")
st.title("🌶️ ChilliFlake")
st.caption("CI test history, flaky-test verdicts, and quarantine controls")

if not DB_PATH.exists():
    st.error(f"Database not found: {DB_PATH}")
    st.code("python scripts/run_pipeline.py --runs 50 --project home-assistant-core --branch dev")
    st.stop()

try:
    columns = set(load("PRAGMA table_info(flakiness_scores)")["name"])
    needed = {"isolated_failures", "commit_conflicts", "analysis_branch"}
    if not needed <= columns:
        st.error("Database schema is old; run scripts/migrate_v2.py and scripts/migrate_v3.py.")
        st.stop()

    projects = load("SELECT DISTINCT project FROM tests ORDER BY project")["project"].dropna().tolist()
    if not projects:
        st.info("No projects have been ingested yet.")
        st.stop()

    filter_project, filter_branch = st.columns(2)
    project = filter_project.selectbox("Project", projects)
    scopes = load(
        """SELECT DISTINCT analysis_branch FROM flakiness_scores fs
           JOIN tests t ON t.id=fs.test_id WHERE t.project=? ORDER BY analysis_branch""",
        (project,),
    )["analysis_branch"].dropna().tolist()
    if not scopes:
        st.info(f"No scores for {project}. Run the analyzer after ingesting test results.")
        st.stop()

    display_scope = {"*": "All branches"}
    branch_label = filter_branch.selectbox("Scored branch scope", [display_scope.get(s, s) for s in scopes])
    branch = next(scope for scope in scopes if display_scope.get(scope, scope) == branch_label)
    scores = load(
        """SELECT fs.*,t.file_path,t.test_name FROM flakiness_scores fs
           JOIN tests t ON t.id=fs.test_id WHERE t.project=? AND fs.analysis_branch=?""",
        (project, branch),
    )
    if scores.empty:
        st.info("No scores are available for this scope.")
        st.stop()

    st.caption(f"Scores last updated: {scores.computed_at.max()}")
    active_count = load(
        """SELECT COUNT(*) n FROM quarantine q JOIN tests t ON t.id=q.test_id
           WHERE q.status='active' AND t.project=?""",
        (project,),
    ).iloc[0, 0]
    metrics = st.columns(4)
    values = (len(scores), (scores.verdict == "flaky").sum(),
              (scores.verdict == "broken").sum(), active_count)
    for column, label, value in zip(metrics, ("Tests scored", "Flaky", "Broken", "Quarantined"), values):
        column.metric(label, int(value))

    chart_left, chart_right = st.columns(2)
    chart_left.subheader("Verdicts")
    chart_left.bar_chart(scores.verdict.value_counts())
    chart_right.subheader("Flakiness signal")
    chart_right.scatter_chart(scores, x="flip_rate", y="wilson_lower_bound", color="verdict")

    st.subheader("Scored tests")
    search_col, verdict_col, samples_col = st.columns([2, 2, 1])
    search = search_col.text_input("Search tests")
    verdicts = verdict_col.multiselect(
        "Verdict", sorted(scores.verdict.unique()), default=sorted(scores.verdict.unique())
    )
    min_samples = samples_col.number_input("Minimum samples", min_value=0, value=0)
    view = scores[scores.verdict.isin(verdicts) & (scores.sample_size >= min_samples)]
    if search:
        view = view[
            view.file_path.fillna("").str.contains(search, case=False, regex=False)
            | view.test_name.str.contains(search, case=False, regex=False)
        ]
    st.dataframe(
        view[["file_path", "test_name", "verdict", "sample_size", "pass_rate", "flip_rate",
              "wilson_lower_bound", "isolated_failures", "commit_conflicts", "duration_cv"]],
        use_container_width=True,
        hide_index=True,
    )

    if view.empty:
        st.info("No tests match the current filters.")
        st.stop()

    labels = (view.file_path.fillna("") + "::" + view.test_name).tolist()
    selected = st.selectbox("Test details", labels)
    row = view[(view.file_path.fillna("") + "::" + view.test_name) == selected].iloc[0]
    history = load(
        """SELECT b.created_at,b.commit_sha,tr.status,tr.duration_ms FROM test_runs tr
           JOIN builds b ON b.id=tr.build_id WHERE tr.test_id=?
           AND (?='*' OR b.branch=?) ORDER BY b.created_at DESC,b.id DESC,tr.id DESC LIMIT 100""",
        (int(row.test_id), branch, branch),
    )
    st.subheader(selected)
    detail_table, duration_chart = st.columns([2, 1])
    detail_table.dataframe(history, use_container_width=True, hide_index=True)
    duration_chart.line_chart(history.sort_values("created_at").set_index("created_at")["duration_ms"])

    st.subheader("Quarantine")
    quarantine = load(
        """SELECT q.id,q.test_id,q.source,q.reason,q.status,q.created_at,q.released_at,
                  t.file_path,t.test_name FROM quarantine q JOIN tests t ON t.id=q.test_id
           WHERE t.project=? ORDER BY q.status,q.created_at DESC""",
        (project,),
    )
    active = quarantine[quarantine.status == "active"]
    st.dataframe(active, use_container_width=True, hide_index=True)
    with st.expander("Recently released"):
        st.dataframe(
            quarantine[quarantine.status == "released"].sort_values("released_at", ascending=False).head(100),
            use_container_width=True,
            hide_index=True,
        )

    selected_id = int(row.test_id)
    active_match = active[active.test_id == selected_id]
    action_left, action_right = st.columns(2)
    if active_match.empty and action_left.button("Quarantine selected test", type="primary"):
        conn = get_connection(str(DB_PATH))
        try:
            add_manual(conn, selected_id, "Added from dashboard")
        finally:
            conn.close()
        refresh()
    if not active_match.empty and action_right.button("Release selected test"):
        conn = get_connection(str(DB_PATH))
        try:
            release(conn, selected_id)
        finally:
            conn.close()
        refresh()
except sqlite3.OperationalError as exc:
    st.error(f"Database schema is unavailable or outdated ({exc}). Run both migration scripts.")
