"""
Entry point of the dashboard. A menu at the top switches between:
  * Chartink dashboard  chartink_dashboard.py: live results of a Chartink dashboard (opens first)
  * Market reports      reports_page.py: Futures and Cash reports from the pipeline
  * Fundamentals        fundamentals_page.py: stocks institutions are buying (long term)
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

    /* Phones: the page menu moves into the sidebar, so make its button easy to see, and
       wrap tabs onto more lines on every page so none hide off the right edge. */
    @media (max-width: 700px) {
      [data-testid="stExpandSidebarButton"] {
        width: auto !important; height: auto !important; padding: .35rem .8rem;
        border: 1px solid #126e72; border-radius: 10px; background: #e6f3f2; color: #126e72;
      }
      [data-testid="stExpandSidebarButton"] > * { display: none; }
      [data-testid="stExpandSidebarButton"]::after { content: "☰ Menu"; font-weight: 600; }
      [data-testid="stTabs"] [role="tablist"] { flex-wrap: wrap; gap: .3rem; overflow: visible !important; }
      [data-testid="stTabs"] [data-baseweb="tab-highlight"],
      [data-testid="stTabs"] [data-baseweb="tab-border"] { display: none; }
      [data-testid="stTabs"] [role="tab"] {
        min-height: 2.4rem; padding: 0 .7rem; border: 1px solid #dce6f3; border-radius: 10px;
      }
      [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
        background: #e6f3f2; border-color: #126e72;
      }
    }
    </style>
    """
)

pages = [
    st.Page("chartink_dashboard.py", title="Chartink dashboard", icon="📊", url_path="chartink", default=True),
    st.Page("reports_page.py", title="Market reports", icon="📰", url_path="reports"),
    st.Page("fundamentals_page.py", title="Fundamentals", icon="🏛️", url_path="fundamentals"),
    st.Page("futures_simulator.py", title="Futures simulator", icon="🧪", url_path="simulator"),
    st.Page("auto_trader_page.py", title="Auto trader", icon="🤖", url_path="auto"),
]
st.navigation(pages, position="top").run()
