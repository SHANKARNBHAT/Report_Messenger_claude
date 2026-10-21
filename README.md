# Report Messenger

Market reports for NSE futures stocks, generated from Yahoo Finance data and published to this private GitHub repository. The Streamlit dashboard reads the generated reports from this repository.

## Run the pipeline

Install dependencies and configure the Telegram credentials in the environment:

```powershell
python -m pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN = "your-rotated-bot-token"
$env:TELEGRAM_CHAT_ID = "your-chat-id"
python Market_messenger.py
python run_confirmed_trend_report.py
```

The automated workflow reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from GitHub Actions repository secrets. Never commit credentials or `.streamlit/secrets.toml`.

## Run the dashboard

Install the dependencies, configure `GITHUB_TOKEN` as a Streamlit secret with permission to dispatch the workflow, and start the app:

```powershell
streamlit run report_app.py
```

The dashboard's workflow trigger targets `SHANKARNBHAT/Report_Messenger_Claude`.
