from pathlib import Path
import os, sqlite3, sys
import pandas as pd
import streamlit as st

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
st.set_page_config(page_title='ChilliFlake - Flaky Test Monitor',page_icon='🌶️',layout='wide')
st.title('🌶️ ChilliFlake')
db_path=Path(os.environ.get('DB_PATH','db/chilliflake.db'))
if not db_path.exists(): st.error(f'Database not found: {db_path}'); st.stop()
@st.cache_data(ttl=30)
def load(sql, params=()):
    uri=f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri,uri=True) as conn: return pd.read_sql_query(sql,conn,params=params)
try:
    columns=set(load('PRAGMA table_info(flakiness_scores)')['name'])
    needed={'isolated_failures','commit_conflicts','analysis_branch'}
    if not needed <= columns: st.error('Database schema is old; run python scripts/migrate_v2.py'); st.stop()
    branches=load('SELECT DISTINCT branch FROM builds ORDER BY branch')['branch'].dropna().tolist()
    options=['(all)']+branches
    scored_scope=load('SELECT DISTINCT analysis_branch FROM flakiness_scores')['analysis_branch'].dropna().tolist()
    scored_scope='(all)' if scored_scope==['*'] else (scored_scope[0] if scored_scope else '(all)')
    branch=st.selectbox('Branch', options, index=options.index(scored_scope) if scored_scope in options else 0)
    if branch != scored_scope:
        st.warning(f'Scores currently cover {scored_scope}. Run the analyzer with --branch {"" if branch=="(all)" else branch} to refresh this scope.')
        st.stop()
    where='' if branch=='(all)' else 'WHERE b.branch=?'
    scores=load(f'''SELECT fs.*,t.file_path,t.test_name FROM flakiness_scores fs JOIN tests t ON t.id=fs.test_id
      WHERE fs.test_id IN (SELECT DISTINCT tr.test_id FROM test_runs tr JOIN builds b ON b.id=tr.build_id {where})''', () if branch=='(all)' else (branch,))
    if scores.empty: st.info('No scores yet. Run the analyzer after ingesting test results.'); st.stop()
    updated=scores.computed_at.max(); st.caption(f'Scores last updated: {updated}')
    active=load("SELECT COUNT(*) n FROM quarantine WHERE status='active'").iloc[0,0]
    a,b,c,d=st.columns(4)
    for col,label,value in zip((a,b,c,d),('Tests scored','Flaky','Broken','Quarantined'),(len(scores),(scores.verdict=='flaky').sum(),(scores.verdict=='broken').sum(),active)): col.metric(label,int(value))
    left,right=st.columns(2); left.bar_chart(scores.verdict.value_counts()); right.scatter_chart(scores,x='flip_rate',y='wilson_lower_bound',color='verdict')
    search=st.text_input('Search tests'); verdicts=st.multiselect('Verdict',sorted(scores.verdict.unique()),default=sorted(scores.verdict.unique())); min_n=st.number_input('Minimum samples',min_value=0,value=0)
    view=scores[scores.verdict.isin(verdicts)&(scores.sample_size>=min_n)]
    if search: view=view[view.file_path.fillna('').str.contains(search,case=False)|view.test_name.str.contains(search,case=False)]
    st.dataframe(view[['file_path','test_name','verdict','sample_size','flip_rate','wilson_lower_bound','isolated_failures','commit_conflicts','duration_cv']],use_container_width=True)
    labels=(scores.file_path.fillna('')+'::'+scores.test_name).tolist()[:200]
    selected=st.selectbox('Test details (first 200 scored tests)',labels)
    row=scores[(scores.file_path.fillna('')+'::'+scores.test_name)==selected].iloc[0]
    history=load('''SELECT b.created_at,tr.status,tr.duration_ms FROM test_runs tr JOIN builds b ON b.id=tr.build_id WHERE tr.test_id=? ORDER BY b.created_at DESC LIMIT 100''',(int(row.test_id),))
    st.subheader(selected); st.dataframe(history,use_container_width=True); st.line_chart(history.sort_values('created_at').set_index('created_at')['duration_ms'])
    st.subheader('Quarantine')
    q=load("SELECT q.id,q.test_id,q.source,q.reason,q.status,q.created_at,q.released_at,t.file_path,t.test_name FROM quarantine q JOIN tests t ON t.id=q.test_id ORDER BY q.status,q.created_at DESC")
    active_q=q[q.status=='active']; st.dataframe(active_q,use_container_width=True)
    with st.expander('Recently released'): st.dataframe(q[q.status=='released'].sort_values('released_at',ascending=False).head(100),use_container_width=True)
    test_ids=dict(zip(scores.file_path.fillna('')+'::'+scores.test_name,scores.test_id))
    from src.analyzer.quarantine import add_manual,release
    c1,c2=st.columns(2)
    active_match=active_q[active_q.test_id==int(row.test_id)]
    if active_match.empty and c1.button('Quarantine selected test'):
        conn=sqlite3.connect(db_path); conn.execute('PRAGMA foreign_keys=ON'); add_manual(conn,int(test_ids[selected]),'Added from dashboard'); conn.close(); st.cache_data.clear(); st.rerun()
    if not active_match.empty and c2.button('Release selected test'):
        conn=sqlite3.connect(db_path); release(conn,int(row.test_id)); conn.close(); st.cache_data.clear(); st.rerun()
    if not active_q.empty:
        q_labels=dict(zip(active_q.file_path.fillna('')+'::'+active_q.test_name,active_q.test_id))
        q_selected=st.selectbox('Release an active quarantine',list(q_labels))
        if st.button('Release this quarantine'):
            conn=sqlite3.connect(db_path); release(conn,int(q_labels[q_selected])); conn.close(); st.cache_data.clear(); st.rerun()
except sqlite3.OperationalError as exc:
    st.error(f'Database schema is unavailable or outdated ({exc}); run python scripts/migrate_v2.py')
