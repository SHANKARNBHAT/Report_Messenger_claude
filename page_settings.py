"""
Keep the choices made on one page while another page of the app is open.

Streamlit drops a widget's value as soon as its page is left. Each widget to keep
passes on_change=remember, args=(key,); report_app.py calls restore() every run.
"""

import streamlit as st


def remember(key):
    st.session_state[f"_kept_{key}"] = st.session_state[key]


def restore(keys):
    for key in keys:
        kept = f"_kept_{key}"
        if kept in st.session_state:
            # Written back every run, even when unchanged: only a value set this
            # run is sent to the browser, which forgot it when the page was left.
            st.session_state[key] = st.session_state[kept]
