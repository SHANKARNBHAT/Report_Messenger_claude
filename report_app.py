"""
Entry point of the dashboard. A menu at the top switches between:
  * Market reports      reports_page.py: Futures and Cash reports from the pipeline
  * Chartink dashboard  chartink_dashboard.py: live results of a Chartink dashboard
"""

import streamlit as st

from page_settings import restore

# Choices made on one page, kept while another page is open (see page_settings.py).
restore(["segment", "refresh", "charts", "snapshot"])

pages = [
    st.Page("reports_page.py", title="Market reports", icon="📰", url_path="reports", default=True),
    st.Page("chartink_dashboard.py", title="Chartink dashboard", icon="📊", url_path="chartink"),
]
st.navigation(pages, position="top").run()
