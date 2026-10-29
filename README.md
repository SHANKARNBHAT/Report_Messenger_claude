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

The app has three pages, switched from the menu at the top:

- **Market reports** (`reports_page.py`): the Futures and Cash reports. The Cash
  reports are read from the `Report_Messenger_cash` repository.
- **Chartink dashboard** (`chartink_dashboard.py`): the live results of a public
  Chartink dashboard, one tab per timeframe, with bullish and bearish tables.
  The first tab, COMMON, lists the stocks that are also on the Active Watchlist
  of the Market reports (Futures or Cash), and the stocks found in two or more
  timeframes.
  Widgets are sorted by name: `2H`, `4h`, `DAILY` or `WEEKLY` picks the tab, and
  `SHORT`, `BEAR` or `SELL` marks a bearish widget. Widgets named
  `Futures stocks ...` list prices, not signals: they are left out of the tabs,
  read in full, and give the Futures simulator its latest prices. The dashboard and chart
  snapshot links are set at the top of the file; the snapshot link can also be
  changed in the sidebar.
- **Futures simulator** (`futures_simulator.py`, with its `.html`, `.css` and `.js`
  files): paper trading of NSE futures. Orders are entered at the latest price
  from the Chartink price-list widgets, else Yahoo Finance (or a price typed in), with lots from `futures stocks.csv`, a stop
  loss, target and trailing stop loss. The 2H Chartink signals can be executed
  automatically with a 1.5% stop loss and 3% target. Open positions are checked
  against the 5-minute bars since entry when the page opens, on Refresh and on
  Run EOD settlement: a trade that touched its SL, trailing SL or target closes
  there, the rest at 3:15 PM IST. Settled trades are charged estimated brokerage,
  STT, exchange fees, stamp duty and GST. The account is saved in the browser
  (localStorage), so each browser has its own. Margin, auto SL/target and charge
  rates are set at the top of `futures_simulator.py`.
