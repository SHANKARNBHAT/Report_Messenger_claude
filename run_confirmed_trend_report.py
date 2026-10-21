import os
import sqlite3
import pandas as pd
from datetime import datetime

# ==========================================
# CONFIGURATION
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "stock_market_data.db")
ACTIVE_REPORT_PATH = os.path.join(SCRIPT_DIR, "active_stocks_report.txt")

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 150)
pd.set_option('expand_frame_repr', False)

def parse_active_stocks_symbols():
    """Parses the stock symbols from the existing active_stocks_report.txt file."""
    if not os.path.exists(ACTIVE_REPORT_PATH):
        print(f"Warning: {ACTIVE_REPORT_PATH} not found. Cannot cross-reference active stocks.")
        return set()

    active_symbols = set()
    with open(ACTIVE_REPORT_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    # Read line-by-line looking for stock symbols in the report layout
    parsing_started = False
    for line in lines:
        if "---" in line or "===" in line:
            if parsing_started:
                # If we hit another separator after starting, we might be done or headers finished
                pass
            continue
        
        parts = line.strip().split()
        if not parts:
            continue
            
        # Skip header rows
        if parts[0].lower() in ["symbol", "market", "active", "report"]:
            parsing_started = True
            continue
            
        if parsing_started or len(parts) > 1:
            # The first column is typically the stock symbol
            symbol = parts[0]
            # Validate it looks like a ticker name (alphanumeric)
            if symbol.isalnum() and symbol.upper() != "SYMBOL":
                active_symbols.add(symbol.upper())

    return active_symbols

def generate_unified_and_relevant_reports():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}.")
        return

    # 1. Parse active stock symbols from active_stocks_report.txt
    active_stock_symbols = parse_active_stocks_symbols()
    print(f"Loaded {len(active_stock_symbols)} active stocks from {ACTIVE_REPORT_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT symbol FROM stock_ohlc_indicators WHERE symbol != '^NSEI';")
    symbols = [row[0] for row in cursor.fetchall()]

    confirmed_records = []
    active_matched_rows = []

    print(f"Analyzing {len(symbols)} stocks for trend signals...")

    for symbol in symbols:
        query = """
            SELECT date, open, high, low, close, volume, sma_20, sma_50, sma_200, rsi_20, rsi_sma_20
            FROM stock_ohlc_indicators 
            WHERE symbol = ? 
            ORDER BY date ASC;
        """
        df = pd.read_sql_query(query, conn, params=(symbol,))
        
        if len(df) < 200:
            continue

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        close = latest['close']
        open_p = latest['open']
        high = latest['high']
        low = latest['low']
        volume = latest['volume']
        sma20 = latest['sma_20']
        sma50 = latest['sma_50']
        sma200 = latest['sma_200']
        prev_close = prev['close']
        prev_sma20 = prev['sma_20']
        prev_sma50 = prev['sma_50']

        # 3-Month Return (~63 trading days)
        idx_3m = -min(len(df), 63)
        close_3m_ago = df.iloc[idx_3m]['close']
        return_3m = ((close - close_3m_ago) / close_3m_ago) * 100

        # Filter strictly for positive 3-month return stocks (> 0%)
        if return_3m <= 0:
            continue

        # SMA Trend Classification (Priority)
        sma_trend = "Undetermined"
        if pd.notna(sma20) and pd.notna(sma50) and pd.notna(sma200):
            if (sma20 > sma50) and (close > sma200):
                sma_trend = "Bullish"
            elif (sma20 < sma50) and (close < sma200):
                sma_trend = "Bearish"

        # Strategy Signal Bias Evaluation
        is_bullish_trend = (sma20 > sma50) and (close > sma200)
        is_bearish_trend = (sma20 < sma50) and (close < sma200)
        cross_above_20_50 = (prev_close <= prev_sma20 and close > sma20) or (prev_close <= prev_sma50 and close > sma50)
        cross_below_20_50 = (prev_close >= prev_sma20 and close < sma20) or (prev_close >= prev_sma50 and close < sma50)

        signal_status = "Neutral / Flat"
        if is_bullish_trend:
            if cross_above_20_50:
                signal_status = "Bullish Entry Trigger"
            else:
                signal_status = "Bullish Trend (In Trade / Holding)"
        elif is_bearish_trend:
            if cross_below_20_50:
                signal_status = "Bearish Entry Trigger"
            else:
                signal_status = "Bearish Trend (In Trade / Holding)"

        signal_bias = "Undetermined"
        if "Bullish" in signal_status:
            signal_bias = "Bullish"
        elif "Bearish" in signal_status:
            signal_bias = "Bearish"

        # Conflict resolution: SMA trend takes priority
        if signal_bias in ["Bullish", "Bearish"] and signal_bias == sma_trend:
            resolved_trend = f"Strong {sma_trend}"
        else:
            resolved_trend = sma_trend

        # Keep ONLY strictly Bullish or Bearish confirmed trends (Drop Undetermined)
        if sma_trend in ["Bullish", "Bearish"]:
            confirmed_records.append({
                'symbol': symbol,
                'return_3m': return_3m,
                'confirmed_trend': resolved_trend
            })

            # Check if this strong trend stock is also present in active stocks
            if symbol.upper() in active_stock_symbols:
                active_matched_rows.append({
                    'symbol': symbol,
                    'close': close,
                    'return_3m': return_3m,
                    'trend': resolved_trend,
                    'volume': volume
                })

    conn.close()

    # --- 2. Write confirmed_bullish_bearish_report.txt ---
    if confirmed_records:
        df_res = pd.DataFrame(confirmed_records)
        df_sorted = df_res.sort_values(by='return_3m', ascending=False).copy()
        df_final = df_sorted[['symbol', 'confirmed_trend']]

        report_file_1 = os.path.join(SCRIPT_DIR, "confirmed_bullish_bearish_report.txt")
        with open(report_file_1, "w", encoding="utf-8") as f:
            f.write("=========================================\n")
            f.write(" STRONG BULLISH & BEARISH STOCKS (3M+)\n")
            f.write(f" Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=========================================\n\n")
            f.write(df_final.to_string(index=False))
            f.write("\n=========================================\n")
        print(f"Saved: {report_file_1}")

    # --- 3. Write relevant_active_stocks_report.txt (Matching Active Stocks Template) ---
    report_file_2 = os.path.join(SCRIPT_DIR, "relevant_active_stocks_report.txt")
    with open(report_file_2, "w", encoding="utf-8") as f:
        f.write("=========================================================================================\n")
        f.write(" 🎯 RELEVANT ACTIVE STOCKS (INTERSECTION OF ACTIVE & STRONG TRENDS)\n")
        f.write(f" Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=========================================================================================\n\n")
        
        if active_matched_rows:
            df_active_matched = pd.DataFrame(active_matched_rows)
            df_active_matched = df_active_matched.sort_values(by='return_3m', ascending=False)
            
            # Formatting to mimic typical Active Stocks columns template
            df_active_matched['return_3m'] = df_active_matched['return_3m'].map('{:+.2f}%'.format)
            df_active_matched['close'] = df_active_matched['close'].map('{:,.2f}'.format)
            
            cols_to_print = ['symbol', 'close', 'return_3m', 'trend', 'volume']
            f.write(df_active_matched[cols_to_print].to_string(index=False))
        else:
            f.write("No overlapping stocks found between Active Stocks and Strong Trend filters.\n")
            
        f.write("\n=========================================================================================\n")
        
    print(f"Saved: {report_file_2}")

if __name__ == "__main__":
    generate_unified_and_relevant_reports()
