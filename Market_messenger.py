import datetime
import os
import sqlite3
import warnings
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# Suppress pandas future warnings and configure text table formatting
warnings.simplefilter(action="ignore", category=FutureWarning)
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 120)
pd.set_option('expand_frame_repr', False)

# ==========================================
# CONFIGURATION & CREDENTIALS
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "stock_market_data.db")

# ==========================================
# TICKER FIXES & MAPPING
# ==========================================
SYMBOL_FIXES = {
    "TEXMACO.NS": "TEXRAIL.NS",
    "PEL.NS": "PIRAMALFIN.NS",
    "PURE.NS": None,
    "DABUR1.NS": "DABUR.NS",
    "BORGER.NS": "BHARATFORG.NS",
    "SMLISUZU.NS": "SMLMAH.NS",
    "ICICIBC.NS": "ICICIBANK.NS",
    "CANFINHOMES.NS": "CANFINHOME.NS",
    "SPICEJET.NS": "SPICEJET.BO",
    "MCDOWELL-N.NS": "UNITDSPR.NS",
    "HAPPENST.NS": "HAPPSTMNDS.NS",
    "LTIM.NS": "LTM.NS",
    "CHOLAFHL.NS": "CHOLAHLDNG.NS",
    "PICCADILY.NS": "PICCADIL.NS",
    "GATI.NS": "ALLCARGO.NS",
    "ACLGATI.NS": "ALLCARGO.NS",
    "CEAT.NS": "CEATLTD.NS",
    "L&TFH.NS": "LTF.NS",
}

TATA_MOTORS_SYMBOLS = [
    "TMPV.NS",
    "TMCV.NS",
]

def clean_yfinance_symbols(original_symbols):
    cleaned = []
    for symbol in original_symbols:
        symbol = symbol.strip()
        if symbol == "TATAMOTORS.NS":
            cleaned.extend(TATA_MOTORS_SYMBOLS)
            continue
        corrected = SYMBOL_FIXES.get(symbol, symbol)
        if corrected is not None:
            cleaned.append(corrected)
    return list(dict.fromkeys(cleaned))


# ==========================================
# 1. DATABASE & LOT SIZE MANAGEMENT
# ==========================================
def initialize_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_ohlc_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            sma_8 REAL,
            sma_20 REAL,
            sma_50 REAL,
            sma_60 REAL,
            sma_200 REAL,
            volume_sma REAL,
            volume_sma_5 REAL,
            rvol REAL,
            turnover_cr REAL,
            rsi_20 REAL,
            rsi_sma_20 REAL,
            rsi_14 REAL,
            rsi_sma_14 REAL,
            atr_14 REAL,
            rs_nifty REAL,
            lot_size INTEGER,
            is_active INTEGER,
            UNIQUE(symbol, date)
        )
    """
    )

    cursor.execute("PRAGMA table_info(stock_ohlc_indicators);")
    existing_columns = [col[1] for col in cursor.fetchall()]
    
    new_columns_to_check = {
        "rsi_14": "REAL",
        "rsi_sma_14": "REAL",
        "atr_14": "REAL",
        "rs_nifty": "REAL"
    }
    
    for col_name, col_type in new_columns_to_check.items():
        if col_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE stock_ohlc_indicators ADD COLUMN {col_name} {col_type};")
                print(f"Database upgraded: Added missing column '{col_name}'.")
            except Exception as e:
                print(f"Could not add column {col_name}: {e}")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            signal_date TEXT,
            strategy_type TEXT,
            bias TEXT,
            close REAL,
            lot_size INTEGER,
            UNIQUE(symbol, signal_date, strategy_type, bias)
        )
    """
    )

    conn.commit()
    conn.close()


def load_lot_sizes(csv_filename="Stock futures.csv"):
    for filename in [csv_filename, "futures stocks.csv"]:
        path = os.path.join(SCRIPT_DIR, filename)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                df.columns = [str(c).strip().lower() for c in df.columns]
                if "symbol" in df.columns and "lot size" in df.columns:
                    return dict(
                        zip(df["symbol"].str.upper().str.strip(), df["lot size"])
                    )
            except Exception:
                pass
    return {}


def prune_old_database_records(conn, max_days=400):
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(date) FROM stock_ohlc_indicators;")
    max_date_row = cursor.fetchone()

    if max_date_row and max_date_row[0]:
        max_date = pd.to_datetime(max_date_row[0])
        cutoff_date = (max_date - pd.Timedelta(days=max_days)).strftime(
            "%Y-%m-%d"
        )

        cursor.execute(
            "DELETE FROM stock_ohlc_indicators WHERE date < ?;", (cutoff_date,)
        )
        cursor.execute(
            "DELETE FROM strategy_signal_history WHERE signal_date < ?;",
            (cutoff_date,),
        )
        conn.commit()
        cursor.execute("VACUUM;")
        print(f"Database pruned: Removed records older than {cutoff_date} (keeping ~400 days)")


def run_market_update(csv_filename="futures stocks.csv"):
    initialize_database()
    lot_map = load_lot_sizes()

    csv_path = os.path.join(SCRIPT_DIR, csv_filename)
    if not os.path.exists(csv_path):
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop", csv_filename)
        if os.path.exists(desktop_path):
            csv_path = desktop_path
        else:
            print(f"Error: Watchlist CSV '{csv_filename}' not found.")
            return

    df_watchlist = pd.read_csv(csv_path)
    df_watchlist.columns = [str(c).strip().lower() for c in df_watchlist.columns]
    raw_symbols = (
        df_watchlist["symbol"].dropna().tolist()
        if "symbol" in df_watchlist.columns
        else df_watchlist.iloc[:, 2].dropna().tolist()
    )
    
    formatted_raw_symbols = [
        str(s).strip() + ".NS"
        if not str(s).endswith(".NS")
        else str(s).strip()
        for s in raw_symbols
    ]
    
    symbols = clean_yfinance_symbols(formatted_raw_symbols)

    print(f"Fetching Nifty 50 (^NSEI) benchmark data...")
    nifty_map = {}
    try:
        nifty_df = yf.download("^NSEI", period="2y", progress=False)
        if not nifty_df.empty:
            if isinstance(nifty_df.columns, pd.MultiIndex):
                nifty_df.columns = nifty_df.columns.get_level_values(0)
            nifty_df = nifty_df.reset_index()
            nifty_df.columns = [str(c).lower() for c in nifty_df.columns]
            nifty_df["nifty_return_50"] = nifty_df["close"].pct_change(periods=50, fill_method=None)
            nifty_map = nifty_df.set_index(nifty_df.iloc[:, 0].astype(str).str[:10])["nifty_return_50"].to_dict()
            
            conn_temp = sqlite3.connect(DB_PATH)
            nifty_df["sma_20"] = nifty_df["close"].rolling(20).mean()
            nifty_df["sma_50"] = nifty_df["close"].rolling(50).mean()
            nifty_df["sma_200"] = nifty_df["close"].rolling(200).mean()
            for _, r in nifty_df.tail(30).iterrows():
                d_str = str(r.iloc[0])[:10]
                cur_c = r.get("close")
                if pd.notna(cur_c):
                    cur_cursor = conn_temp.cursor()
                    cur_cursor.execute(
                        """
                        INSERT OR REPLACE INTO stock_ohlc_indicators 
                        (symbol, date, close, sma_20, sma_50, sma_200)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (
                            "^NSEI",
                            d_str,
                            float(cur_c),
                            float(r.get("sma_20")) if pd.notna(r.get("sma_20")) else None,
                            float(r.get("sma_50")) if pd.notna(r.get("sma_50")) else None,
                            float(r.get("sma_200")) if pd.notna(r.get("sma_200")) else None,
                        ),
                    )
            conn_temp.commit()
            conn_temp.close()
    except Exception as e:
        print(f"Warning: Could not process Nifty benchmark data: {e}")

    print(f"Fetching market data for {len(symbols)} stocks via yfinance...")
    data = yf.download(symbols, period="2y", group_by="ticker", progress=False)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    updated_records = 0

    for symbol in symbols:
        clean_symbol = symbol.replace(".NS", "").replace(".BO", "").upper()
        lot_size = lot_map.get(clean_symbol, 1)
        try:
            hist = data[symbol] if len(symbols) > 1 else data
            if hist.empty or len(hist) < 200:
                continue
            hist = hist.reset_index()
            hist.columns = [str(c).lower() for c in hist.columns]

            # Technical Indicators
            hist["sma_8"] = hist["close"].rolling(8).mean()
            hist["sma_20"] = hist["close"].rolling(20).mean()
            hist["sma_50"] = hist["close"].rolling(50).mean()
            hist["sma_60"] = hist["close"].rolling(60).mean()
            hist["sma_200"] = hist["close"].rolling(200).mean()
            hist["volume_sma"] = hist["volume"].rolling(20).mean()
            hist["volume_sma_5"] = hist["volume"].rolling(5).mean()

            # ATR 14
            high_low = hist["high"] - hist["low"]
            high_close = (hist["high"] - hist["close"].shift()).abs()
            low_close = (hist["low"] - hist["close"].shift()).abs()
            true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            hist["atr_14"] = true_range.rolling(14).mean()

            # RSI 20 & SMA of RSI 20
            delta = hist["close"].diff()
            gain = delta.clip(lower=0)
            loss = -1 * delta.clip(upper=0)
            avg_gain_20 = gain.ewm(com=19, min_periods=20).mean()
            avg_loss_20 = loss.ewm(com=19, min_periods=20).mean()
            rs_20 = avg_gain_20 / avg_loss_20.replace(0, np.nan)
            hist["rsi_20"] = 100 - (100 / (1 + rs_20))
            hist["rsi_sma_20"] = hist["rsi_20"].rolling(20).mean()

            # RSI 14 & SMA of RSI 14
            avg_gain_14 = gain.ewm(com=13, min_periods=14).mean()
            avg_loss_14 = loss.ewm(com=13, min_periods=14).mean()
            rs_14 = avg_gain_14 / avg_loss_14.replace(0, np.nan)
            hist["rsi_14"] = 100 - (100 / (1 + rs_14))
            hist["rsi_sma_14"] = hist["rsi_14"].rolling(14).mean()

            # Relative Strength vs Nifty
            hist["stock_return_50"] = hist["close"].pct_change(periods=50, fill_method=None)
            
            hist["rvol"] = hist["volume"] / hist["volume_sma"]
            hist["turnover_cr"] = (hist["close"] * hist["volume"]) / 10_000_000
            hist["is_active"] = (
                (hist["rvol"] >= 1.5) & (hist["turnover_cr"] >= 10.0)
            ).astype(int)

            for _, row in hist.tail(30).iterrows():
                date_str = str(row.iloc[0])[:10]
                close_p = row.get("close")
                if pd.isna(close_p):
                    continue  # Skip if close price is missing

                stock_ret = row.get("stock_return_50")
                nifty_ret = nifty_map.get(date_str, np.nan)
                rs_val = (stock_ret - nifty_ret) if (pd.notna(stock_ret) and pd.notna(nifty_ret)) else None

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO stock_ohlc_indicators 
                    (symbol, date, open, high, low, close, volume, sma_8, sma_20, sma_50, sma_60, sma_200, 
                     volume_sma, volume_sma_5, rvol, turnover_cr, rsi_20, rsi_sma_20, rsi_14, rsi_sma_14, 
                     atr_14, rs_nifty, lot_size, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        clean_symbol,
                        date_str,
                        float(row.get("open")) if pd.notna(row.get("open")) else None,
                        float(row.get("high")) if pd.notna(row.get("high")) else None,
                        float(row.get("low")) if pd.notna(row.get("low")) else None,
                        float(close_p),
                        int(row.get("volume", 0)),
                        float(row.get("sma_8")) if pd.notna(row.get("sma_8")) else None,
                        float(row.get("sma_20")) if pd.notna(row.get("sma_20")) else None,
                        float(row.get("sma_50")) if pd.notna(row.get("sma_50")) else None,
                        float(row.get("sma_60")) if pd.notna(row.get("sma_60")) else None,
                        float(row.get("sma_200")) if pd.notna(row.get("sma_200")) else None,
                        float(row.get("volume_sma")) if pd.notna(row.get("volume_sma")) else None,
                        float(row.get("volume_sma_5")) if pd.notna(row.get("volume_sma_5")) else None,
                        float(row.get("rvol")) if pd.notna(row.get("rvol")) else None,
                        float(row.get("turnover_cr")) if pd.notna(row.get("turnover_cr")) else None,
                        float(row.get("rsi_20")) if pd.notna(row.get("rsi_20")) else None,
                        float(row.get("rsi_sma_20")) if pd.notna(row.get("rsi_sma_20")) else None,
                        float(row.get("rsi_14")) if pd.notna(row.get("rsi_14")) else None,
                        float(row.get("rsi_sma_14")) if pd.notna(row.get("rsi_sma_14")) else None,
                        float(row.get("atr_14")) if pd.notna(row.get("atr_14")) else None,
                        float(rs_val) if rs_val is not None else None,
                        int(lot_size),
                        int(row.get("is_active", 0)),
                    ),
                )
                updated_records += 1
            conn.commit()
        except Exception:
            continue

    prune_old_database_records(conn, max_days=400)
    conn.close()
    print(f"Database update complete. Synced {updated_records} records.")


# ==========================================
# 2. STRATEGY SCANNING & REPORTS GENERATION
# ==========================================
def run_strategy_scans_and_reports():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT symbol FROM stock_ohlc_indicators WHERE symbol != '^NSEI';")
    symbols = [row[0] for row in cursor.fetchall()]

    for symbol in symbols:
        query = """
            SELECT date, close, high, low, sma_20, sma_50, sma_200, rsi_20, rsi_sma_20, lot_size, is_active
            FROM stock_ohlc_indicators WHERE symbol = ? ORDER BY date ASC;
        """
        df = pd.read_sql_query(query, conn, params=(symbol,))
        if len(df) < 50:
            continue

        prev = df.iloc[-2]
        latest = df.iloc[-1]
        current_date = str(latest["date"])[:10]
        lot_size = int(latest["lot_size"]) if pd.notna(latest["lot_size"]) else 1

        # Strategy 1: Price Cross of SMA 20 / SMA 50 with 200 SMA Filter
        s20_l, s50_l, s200_l, close_l = latest["sma_20"], latest["sma_50"], latest["sma_200"], latest["close"]
        s20_p, s50_p, close_p = prev["sma_20"], prev["sma_50"], prev["close"]

        if pd.notna(s20_l) and pd.notna(s50_l) and pd.notna(s200_l) and pd.notna(close_l) and pd.notna(close_p):
            crossed_above_sma20 = (close_p <= s20_p) and (close_l > s20_l)
            crossed_above_sma50 = (close_p <= s50_p) and (close_l > s50_l)
            crossed_below_sma20 = (close_p >= s20_p) and (close_l < s20_l)
            crossed_below_sma50 = (close_p >= s50_p) and (close_l < s50_l)

            if (s20_l >= s50_l) and (crossed_above_sma20 or crossed_above_sma50) and (close_l > s200_l):
                cursor.execute(
                    "INSERT OR IGNORE INTO strategy_signal_history (symbol, signal_date, strategy_type, bias, close, lot_size) VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol, current_date, "SMA_Crossover", "BULLISH", latest["close"], lot_size),
                )
            elif (s20_l <= s50_l) and (crossed_below_sma20 or crossed_below_sma50) and (close_l < s200_l):
                cursor.execute(
                    "INSERT OR IGNORE INTO strategy_signal_history (symbol, signal_date, strategy_type, bias, close, lot_size) VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol, current_date, "SMA_Crossover", "BEARISH", latest["close"], lot_size),
                )

        # Strategy 2: RSI Strategy
        rsi_l, rsi_p = latest["rsi_20"], prev["rsi_20"]
        rsisma_l = latest["rsi_sma_20"]

        if pd.notna(rsi_l) and pd.notna(rsi_p) and pd.notna(rsisma_l):
            if (rsisma_l > 45) and (rsi_p <= 60) and (rsi_l > 60):
                cursor.execute(
                    "INSERT OR IGNORE INTO strategy_signal_history (symbol, signal_date, strategy_type, bias, close, lot_size) VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol, current_date, "RSI_Strategy", "BULLISH", latest["close"], lot_size),
                )
            elif (rsisma_l > 50) and (rsi_p >= 40) and (rsi_l < 40):
                cursor.execute(
                    "INSERT OR IGNORE INTO strategy_signal_history (symbol, signal_date, strategy_type, bias, close, lot_size) VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol, current_date, "RSI_Strategy", "BEARISH", latest["close"], lot_size),
                )

    conn.commit()

    # Determine Nifty Market Baseline Status
    nifty_trend_status = "Undetermined"
    try:
        nifty_df = pd.read_sql_query(
            """
            SELECT sma_20, sma_50, sma_200, close 
            FROM stock_ohlc_indicators 
            WHERE symbol = '^NSEI' AND date = (SELECT MAX(date) FROM stock_ohlc_indicators WHERE symbol = '^NSEI')
        """,
            conn,
        )
        if not nifty_df.empty:
            ns20, ns50, ns200, nclose = nifty_df.iloc[0]["sma_20"], nifty_df.iloc[0]["sma_50"], nifty_df.iloc[0]["sma_200"], nifty_df.iloc[0]["close"]
            if pd.notna(ns20) and pd.notna(ns50) and pd.notna(ns200) and pd.notna(nclose):
                if (ns20 > ns50) and (nclose > ns200):
                    nifty_trend_status = "Bullish"
                elif (ns20 < ns50) and (nclose < ns200):
                    nifty_trend_status = "Bearish"
    except Exception:
        pass

    market_baseline_sentence = f"• Market Baseline: Nifty 50 is currently {nifty_trend_status} (Close vs 200 SMA & SMA 20 vs SMA 50)."

    seven_days_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    fourteen_days_ago = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")

    active_df = pd.read_sql_query(
        """
        SELECT symbol, sma_20, sma_50, sma_200, rvol, turnover_cr, close, volume, is_active, atr_14, high, low 
        FROM stock_ohlc_indicators 
        WHERE date = (SELECT MAX(date) FROM stock_ohlc_indicators WHERE symbol != '^NSEI') AND symbol != '^NSEI'
        ORDER BY rvol DESC
    """,
        conn,
    )

    def compute_trend(row):
        s20, s50, s200, close = row['sma_20'], row['sma_50'], row['sma_200'], row['close']
        if pd.notna(s20) and pd.notna(s50) and pd.notna(s200) and pd.notna(close):
            if (s20 > s50) and (close > s200):
                return 'Bullish'
            elif (s20 < s50) and (close < s200):
                return 'Bearish'
        return 'Undetermined'

    active_df['daily_range'] = active_df['high'] - active_df['low']

    def compute_action(row):
        trend = row['trend']
        daily_range = row['daily_range']
        atr_14 = row['atr_14']
        
        if pd.isna(daily_range) or pd.isna(atr_14):
            return '4.Wait'
            
        is_expanding = daily_range > atr_14
        
        if trend == 'Bullish':
            return '1.Long' if is_expanding else '4.Wait'
        elif trend == 'Bearish':
            return '2.Short' if is_expanding else '4.Wait'
        elif trend == 'Undetermined':
            return '3.Watch' if is_expanding else '4.Wait'
            
        return '4.Wait'

    active_df['trend'] = active_df.apply(compute_trend, axis=1)
    active_df['action'] = active_df.apply(compute_action, axis=1)

    history_df = pd.read_sql_query(
        f"""
        SELECT symbol, signal_date, strategy_type, bias, close, lot_size 
        FROM strategy_signal_history 
        WHERE signal_date >= '{seven_days_ago}'
    """,
        conn,
    )

    fourteen_day_history_df = pd.read_sql_query(
        f"""
        SELECT symbol, signal_date, strategy_type, bias, close, lot_size 
        FROM strategy_signal_history 
        WHERE signal_date >= '{fourteen_days_ago}'
    """,
        conn,
    )
    conn.close()

    bullish_records = history_df[history_df["bias"] == "BULLISH"] if not history_df.empty else pd.DataFrame()
    bearish_records = history_df[history_df["bias"] == "BEARISH"] if not history_df.empty else pd.DataFrame()

    with open(os.path.join(SCRIPT_DIR, "bullish_report.txt"), "w", encoding="utf-8") as f:
        f.write("=== BULLISH REPORT (Last 7 Days) ===\n")
        f.write(f"{market_baseline_sentence}\n\n")
        f.write(bullish_records.to_string(index=False) if not bullish_records.empty else "No bullish records in past 7 days.")

    with open(os.path.join(SCRIPT_DIR, "bearish_report.txt"), "w", encoding="utf-8") as f:
        f.write("=== BEARISH REPORT (Last 7 Days) ===\n")
        f.write(f"{market_baseline_sentence}\n\n")
        f.write(bearish_records.to_string(index=False) if not bearish_records.empty else "No bearish records in past 7 days.")

    active_rvol_all = active_df[active_df['rvol'] > 1.0][['symbol', 'rvol', 'close', 'daily_range', 'atr_14', 'is_active', 'trend', 'action']].copy()
    
    # Format columns neatly to prevent line wrapping inside Telegram code blocks
    active_rvol_all['rvol'] = active_rvol_all['rvol'].map(lambda x: f"{x:.2f}" if pd.notna(x) else "0.00")
    active_rvol_all['close'] = active_rvol_all['close'].map(lambda x: f"{x:.2f}" if pd.notna(x) else "0.00")
    active_rvol_all['daily_range'] = active_rvol_all['daily_range'].map(lambda x: f"{x:.2f}" if pd.notna(x) else "0.00")
    active_rvol_all['atr_14'] = active_rvol_all['atr_14'].map(lambda x: f"{x:.2f}" if pd.notna(x) else "0.00")

    # Sort strictly by action (1.Long -> 2.Short -> 3.Watch -> 4.Wait), then by RVOL descending
    active_rvol_all = active_rvol_all.sort_values(by=['action', 'rvol'], ascending=[True, False])

    with open(os.path.join(SCRIPT_DIR, "active_stocks_report.txt"), "w", encoding="utf-8") as f:
        f.write("==================================================\n")
        f.write(" ⚡ ALL ACTIVE STOCKS REPORT (RVOL > 1.0)\n")
        f.write(f" Generated on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("==================================================\n")
        f.write("REPORT CRITERIA & DESCRIPTION:\n")
        f.write(f"{market_baseline_sentence}\n")
        f.write("• Tracks all NSE futures stocks where Relative Volume (RVOL) > 1.0.\n")
        f.write("• Action Logic:\n")
        f.write("    - 1.Long  : Bullish Trend AND (Daily Range > ATR 14)\n")
        f.write("    - 2.Short : Bearish Trend AND (Daily Range > ATR 14)\n")
        f.write("    - 3.Watch : Undetermined Trend AND (Daily Range > ATR 14)\n")
        f.write("    - 4.Wait  : Otherwise (Daily Range <= ATR 14 / Compression)\n")
        f.write("• Grouped & Ordered by Action (1.Long -> 2.Short -> 3.Watch -> 4.Wait):\n")
        f.write("    - Bullish     : SMA 20 > SMA 50 AND Close > SMA 200\n")
        f.write("    - Bearish     : SMA 20 < SMA 50 AND Close < SMA 200\n")
        f.write("    - Undetermined: Any other moving average alignment\n")
        f.write("--------------------------------------------------\n\n")
        f.write(active_rvol_all.to_string(index=False) if not active_rvol_all.empty else "No stocks found with RVOL > 1.0.")

    bullish_14d_records = fourteen_day_history_df[fourteen_day_history_df["bias"] == "BULLISH"] if not fourteen_day_history_df.empty else pd.DataFrame()
    bearish_14d_records = fourteen_day_history_df[fourteen_day_history_df["bias"] == "BEARISH"] if not fourteen_day_history_df.empty else pd.DataFrame()

    bullish_overlap = set(bullish_14d_records[bullish_14d_records["strategy_type"] == "SMA_Crossover"]["symbol"]).intersection(set(bullish_14d_records[bullish_14d_records["strategy_type"] == "RSI_Strategy"]["symbol"])) if not bullish_14d_records.empty else set()
    bearish_overlap = set(bearish_14d_records[bearish_14d_records["strategy_type"] == "SMA_Crossover"]["symbol"]).intersection(set(bearish_14d_records[bearish_14d_records["strategy_type"] == "RSI_Strategy"]["symbol"])) if not bearish_14d_records.empty else set()
    active_symbols = set(active_df[active_df["is_active"] == 1]["symbol"].str.upper())

    consolidated_path = os.path.join(SCRIPT_DIR, "consolidated_strategy_report.txt")
    with open(consolidated_path, "w", encoding="utf-8") as f:
        f.write("==================================================\n")
        f.write(" 🎯 CONSOLIDATED MARKET INTELLIGENCE REPORT (2-WEEK WINDOW)\n")
        f.write(f" Generated on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("==================================================\n")
        f.write(f"{market_baseline_sentence}\n\n")

        f.write("【 🟢 HIGH-CONVICTION BULLISH OVERLAPS (SMA Crossover + RSI in Last 14D) 】\n")
        if bullish_overlap:
            for sym in sorted(bullish_overlap):
                sample_row = bullish_14d_records[bullish_14d_records["symbol"] == sym].iloc[0]
                is_act = "⚡ ACTIVE" if sym in active_symbols else ""
                f.write(f"• {sym:<12} | Lot Size: {sample_row['lot_size']:<5} | Last Close: {sample_row['close']:<8.2f} {is_act}\n")
        else:
            f.write("No multi-strategy overlapping bullish stocks found in the past 2 weeks.\n")

        f.write("\n【 🔴 HIGH-CONVICTION BEARISH OVERLAPS (SMA Crossover + RSI in Last 14D) 】\n")
        if bearish_overlap:
            for sym in sorted(bearish_overlap):
                sample_row = bearish_14d_records[bearish_14d_records["symbol"] == sym].iloc[0]
                is_act = "⚡ ACTIVE" if sym in active_symbols else ""
                f.write(f"• {sym:<12} | Lot Size: {sample_row['lot_size']:<5} | Last Close: {sample_row['close']:<8.2f} {is_act}\n")
        else:
            f.write("No multi-strategy overlapping bearish stocks found in the past 2 weeks.\n")

        f.write("\n--------------------------------------------------\n")
        f.write("📋 FULL 14-DAY SIGNAL LOG:\n")
        if not fourteen_day_history_df.empty:
            f.write(fourteen_day_history_df[["symbol", "signal_date", "strategy_type", "bias", "lot_size"]].to_string(index=False))
        else:
            f.write("No entries logged yet.")

    active_rvol_df = active_df[active_df["rvol"] > 1.0]
    active_rvol_symbols = set(active_rvol_symbol.upper() for active_rvol_symbol in active_rvol_df["symbol"].tolist())
    watchlist_signal_symbols = set(fourteen_day_history_df["symbol"].str.upper().tolist()) if not fourteen_day_history_df.empty else set()
    common_rvol_symbols = watchlist_signal_symbols.intersection(active_rvol_symbols)

    rvol_report_lines = [
        "==================================================",
        " 👁️ STOCKS TO WATCH",
        f" Generated on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "==================================================",
        f"{market_baseline_sentence}",
        "",
        "WHAT IS RVOL (RELATIVE VOLUME)?",
        "• Relative Volume (RVOL) compares today's trading volume against its historical average.",
        "• High RVOL (> 1.5) highlights institutional participation or breakouts.",
        "",
        "REPORT CRITERIA:",
        "• Cross-references past 14-day technical signals with stocks exhibiting RVOL > 1.0 today.",
        "--------------------------------------------------\n",
    ]

    if common_rvol_symbols and not fourteen_day_history_df.empty:
        rvol_report_lines.append(f"Found {len(common_rvol_symbols)} stock(s) from 14-day signal log active with RVOL > 1 today:\n")
        rvol_report_lines.append(f"{'Symbol':<12} {'Strategy':<16} {'Bias':<10} {'Lot Size':<10} {'RVOL':<8} {'ATR14':<8} {'Daily Range':<12} {'Close':<10}")
        rvol_report_lines.append("-" * 92)

        for sym in sorted(common_rvol_symbols):
            sym_signals = fourteen_day_history_df[fourteen_day_history_df["symbol"].str.upper() == sym].sort_values(by="signal_date", ascending=False)
            latest_sig = sym_signals.iloc[0]
            active_match = active_rvol_df[active_rvol_df["symbol"].str.upper() == sym].iloc[0]

            rvol_report_lines.append(
                f"{sym:<12} "
                f"{latest_sig['strategy_type']:<16} "
                f"{latest_sig['bias']:<10} "
                f"{int(latest_sig['lot_size']):<10} "
                f"{active_match['rvol']:<8.2f} "
                f"{(active_match['atr_14'] if pd.notna(active_match['atr_14']) else 0):<8.2f} "
                f"{(active_match['daily_range'] if pd.notna(active_match['daily_range']) else 0):<12.2f} "
                f"{active_match['close']:<10.2f}"
            )
    else:
        rvol_report_lines.append("ℹ️ No stocks from 14-day signal log match the RVOL > 1 active list filter today.")

    rvol_report_path = os.path.join(SCRIPT_DIR, "rvol_active_signals_report.txt")
    with open(rvol_report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rvol_report_lines))

    print("All 5 reports successfully generated and updated!")


# ==========================================
# 3. TELEGRAM DISPATCHER
# ==========================================
def dispatch_to_telegram():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured."
        )

    reports = [
        ("bullish_report.txt", "📈 Bullish Strategy Report"),
        ("bearish_report.txt", "📉 Bearish Strategy Report"),
        ("active_stocks_report.txt", "⚡ Active Stocks Report"),
        ("consolidated_strategy_report.txt", "🎯 Consolidated Strategy & Activity Report"),
        ("rvol_active_signals_report.txt", "👁️ Stocks to Watch"),
    ]

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for filename, title in reports:
        path = os.path.join(SCRIPT_DIR, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            content = content.replace(">", " GT ").replace("<", " LT ")

            max_len = 4000
            chunks = [content[i : i + max_len] for i in range(0, len(content), max_len)]

            for idx, chunk in enumerate(chunks):
                chunk_title = f"{title} (Part {idx+1})" if len(chunks) > 1 else title
                payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": f"<b>{chunk_title}</b>\n<pre>{chunk}</pre>",
                    "parse_mode": "HTML",
                }
                try:
                    response = requests.post(url, json=payload, timeout=10)
                    if response.status_code != 200:
                        print(f"Telegram error for {chunk_title}: {response.text}")
                    else:
                        print(f"Dispatched {chunk_title} to Telegram.")
                except Exception as e:
                    print(f"Failed to dispatch {chunk_title}: {e}")


if __name__ == "__main__":
    print("=== Starting Unified Master Market Pipeline ===")
    run_market_update("futures stocks.csv")
    run_strategy_scans_and_reports()
    print("Telegram delivery is paused.")
    print("Master pipeline execution completed successfully!")
