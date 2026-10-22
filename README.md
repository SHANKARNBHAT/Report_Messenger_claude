# Report Messenger

Market reports for NSE futures stocks, generated from Yahoo Finance data and
published to a public GitHub repository. The Streamlit dashboard reads the
generated reports from this repository.

## Run the pipeline

Install dependencies and run the report pipeline:

```powershell
python -m pip install -r requirements.txt
python Market_messenger.py
python run_confirmed_trend_report.py
```

Telegram report delivery is paused. The automated workflow generates and
publishes reports without sending Telegram messages. Never commit credentials
or `.streamlit/secrets.toml`.

## Run the dashboard

Install the dependencies and start the app:

```powershell
streamlit run report_app.py
```

The dashboard reads reports directly from the public repository without a
GitHub token. To start the report workflow from the dashboard, configure
`GITHUB_TOKEN` as a Streamlit secret with Actions write access to this
repository.
