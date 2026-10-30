"""
Entry point of the dashboard. A menu at the top switches between:
  * Market reports      reports_page.py: Futures and Cash reports from the pipeline
  * Chartink dashboard  chartink_dashboard.py: live results of a Chartink dashboard
  * Futures simulator   futures_simulator.py: paper trading of NSE futures and 2H signals
  * Auto trader         auto_trader_page.py: the account auto_trader.py trades on a schedule
"""

import streamlit as st

from page_settings import restore

# Choices made on one page, kept while another page is open (see page_settings.py).
restore(["segment", "refresh", "charts", "snapshot"])

# Menu at the top: the page links share the width equally and are larger.
st.html(
    """
    <style>
    [data-testid="stToolbar"] .rc-overflow { flex: 1 1 auto; }
    [data-testid="stToolbar"] .rc-overflow-item:has([data-testid="stTopNavLink"]) { flex: 1 1 0; }
    [data-testid="stToolbar"] .rc-overflow-item:has([data-testid="stTopNavLink"]) > div,
    [data-testid="stTopNavLinkContainer"] { width: 100%; }
    [data-testid="stTopNavLink"] {
      width: calc(100% - .5rem); margin: 0 .25rem; min-height: 3rem; justify-content: center; gap: .5rem;
      border: 1px solid #dce6f3; border-radius: 12px;
    }
    [data-testid="stTopNavLink"] * { font-size: 1.15rem !important; font-weight: 600; }
    </style>
    """
)

pages = [
    st.Page("reports_page.py", title="Market reports", icon="📰", url_path="reports", default=True),
    st.Page("chartink_dashboard.py", title="Chartink dashboard", icon="📊", url_path="chartink"),
    st.Page("futures_simulator.py", title="Futures simulator", icon="🧪", url_path="simulator"),
    st.Page("auto_trader_page.py", title="Auto trader", icon="🤖", url_path="auto"),
]
st.navigation(pages, position="top").run()
