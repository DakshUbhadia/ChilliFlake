"""
ChilliFlake Dashboard
---------------------
Streamlit application entry point.

Run with:
    streamlit run dashboard/app.py
"""
import streamlit as st

st.set_page_config(
    page_title="ChilliFlake — Flaky Test Monitor",
    page_icon="🌶️",
    layout="wide",
)

st.title("🌶️ ChilliFlake")
st.subheader("Statistical Flaky Test Detector & Pipeline Quarantine System")
st.info("Dashboard coming soon. Run the ingestion and analyzer modules first.")
