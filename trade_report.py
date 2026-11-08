"""
The Auto trader's closed trades as an Excel workbook (the Trade report download of
the Auto trader page): a summary by strategy, side and exit style, the strategies per
stock, and every closed trade. The totals are formulas over the Closed trades sheet,
so they follow edits to it; Excel computes them when the file opens.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as col

FONT = "Arial"
RUPEES = '"₹"#,##0;[Red]-"₹"#,##0;-'
HEADER = Font(name=FONT, bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
BODY, BOLD = Font(name=FONT), Font(name=FONT, bold=True)
TITLE, NOTE = Font(name=FONT, bold=True, size=13), Font(name=FONT, italic=True, color="555555")
GAIN, LOSS = PatternFill("solid", fgColor="E2EFDA"), PatternFill("solid", fgColor="FCE4E4")
SHORT_TF = {"2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily", "WEEKLY": "Weekly"}
# Closed trades sheet: the columns the formulas read.
SYMBOL, STRATEGY, STYLE, SIDE, NET = "A", "B", "C", "D", "N"


def ist(ms: pd.Series) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)


def header(ws, row: int, names: list[str], widths: list[int]) -> None:
    for i, (name, width) in enumerate(zip(names, widths), 1):
        cell = ws.cell(row, i, name)
        cell.font, cell.fill = HEADER, HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[col(i)].width = width


def gain_loss(ws, cells: str) -> None:
    ws.conditional_formatting.add(cells, CellIsRule(operator="greaterThan", formula=["0"], fill=GAIN))
    ws.conditional_formatting.add(cells, CellIsRule(operator="lessThan", formula=["0"], fill=LOSS))


def workbook(history: pd.DataFrame, styles: dict[str, str]) -> bytes:
    """The report of the closed trades (rows as auto_trader_page.prepare gives them);
    `styles` names the exit styles (futures_simulator.BOT_STYLES)."""
    t = history.copy()
    t["system"] = t["system"].fillna("RSI")
    t["style"] = t["style"].fillna("eod")
    t["Entered"], t["Exited"] = ist(t["entry_time"]), ist(t["exit_time"])
    t["Timeframe"] = [" + ".join(SHORT_TF.get(tf, tf) for tf in tfs) if isinstance(tfs, list) and tfs
                      else str(source).removeprefix("Auto ").removeprefix("HA ")
                      for tfs, source in zip(t["timeframes"], t["source"])]
    t = t.sort_values(["symbol", "Entered", "system"])
    last = len(t) + 1
    span = (f"{t['Entered'].min():%d %b %Y} – {t['Exited'].max():%d %b %Y}" if len(t) else "no trades yet")

    def rows(letter: str) -> str:
        return f"'Closed trades'!${letter}$2:${letter}${max(last, 2)}"

    wb = Workbook()

    # Summary ------------------------------------------------------------------
    sm = wb.active
    sm.title = "Summary"
    sm["A1"], sm["A1"].font = f"Auto trader: strategies compared ({span})", TITLE
    sm["A2"] = ("Closed trades only. Each signal is traded once per exit style, so one signal can count as 2–3 trades.")
    sm["A2"].font = NOTE

    def block(row: int, title: str, groups: list[tuple[str, str]], by: str | None) -> int:
        sm.cell(row, 1, title).font = Font(name=FONT, bold=True, size=11)
        header(sm, row + 1, ["Strategy", "Group", "Trades", "Wins", "Win %", "Net ₹", "Avg per trade ₹"],
               [12, 26, 9, 8, 9, 14, 15])
        r = row + 2
        for system, group in groups:
            sm.cell(r, 1, system)
            sm.cell(r, 2, group)
            match = f"{rows(STRATEGY)},$A{r}" + (f",{rows(by)},$B{r}" if by else "")
            sm.cell(r, 3, f"=COUNTIFS({match})")
            sm.cell(r, 4, f'=COUNTIFS({match},{rows(NET)},">0")')
            sm.cell(r, 5, f'=IF(C{r}=0,"",D{r}/C{r})')
            sm.cell(r, 6, f"=SUMIFS({rows(NET)},{match})")
            sm.cell(r, 7, f'=IF(C{r}=0,"",F{r}/C{r})')
            for c in range(1, 8):
                sm.cell(r, c).font = BODY
            sm.cell(r, 5).number_format = "0%"
            sm.cell(r, 6).number_format = sm.cell(r, 7).number_format = RUPEES
            r += 1
        gain_loss(sm, f"F{row + 2}:G{r - 1}")
        return r + 1

    systems = sorted(t["system"].unique()) or ["HA", "RSI"]
    row = block(4, "By strategy", [(s, "All") for s in systems], None)
    row = block(row, "By strategy and side", [(s, d) for s in systems for d in ("LONG", "SHORT")], SIDE)
    pairs = t[["system", "style"]].drop_duplicates().sort_values(["system", "style"])
    block(row, "By strategy and exit style",
          [(s, styles.get(k, k)) for s, k in pairs.itertuples(index=False)], STYLE)

    # Per stock ----------------------------------------------------------------
    ps = wb.create_sheet("Per stock")
    names = " vs ".join(systems)
    ps["A1"], ps["A1"].font = f"{names} per stock ({span})", TITLE
    ps["A2"] = ("Totals from the Closed trades sheet, best stock first. Best: the strategy with the highest net "
                "among those that traded the stock.")
    ps["A2"].font = NOTE
    heads, widths = ["Symbol"], [14]
    for system in systems:
        heads += [f"{system} trades", f"{system} wins", f"{system} win %", f"{system} net ₹"]
        widths += [9, 8, 9, 13]
    tot_c, best_c, by_c = 2 + 4 * len(systems), 3 + 4 * len(systems), 4 + 4 * len(systems)
    heads += ["Total net ₹", "Best", "Strategies"]
    widths += [13, 11, 11]
    header(ps, 4, heads, widths)
    ps.freeze_panes = "B5"
    firsts = {system: 2 + 4 * i for i, system in enumerate(systems)}
    order = t.groupby("symbol")["net"].sum().sort_values(ascending=False).index
    for r, symbol in enumerate(order, 5):
        ps.cell(r, 1, symbol)
        for system, first in firsts.items():
            match = f'{rows(SYMBOL)},$A{r},{rows(STRATEGY)},"{system}"'
            ps.cell(r, first, f"=COUNTIFS({match})")
            ps.cell(r, first + 1, f'=COUNTIFS({match},{rows(NET)},">0")')
            ps.cell(r, first + 2, f'=IF({col(first)}{r}=0,"",{col(first + 1)}{r}/{col(first)}{r})')
            ps.cell(r, first + 3, f"=SUMIFS({rows(NET)},{match})")
        ps.cell(r, tot_c, "=" + "+".join(f"{col(f + 3)}{r}" for f in firsts.values()))
        best = '""'
        for system, first in reversed(list(firsts.items())):
            beats = [f"OR({col(o)}{r}=0,{col(first + 3)}{r}>={col(o + 3)}{r})" for s2, o in firsts.items() if s2 != system]
            test = f"AND({col(first)}{r}>0{''.join(',' + b for b in beats)})"
            best = f'IF({test},"{system}",{best})'
        ps.cell(r, best_c, "=" + best)
        ps.cell(r, by_c, "=" + "+".join(f"({col(f)}{r}>0)" for f in firsts.values()))
    end = 4 + len(order)
    total = end + 1
    ps.cell(total, 1, "Total")
    for first in firsts.values():
        for c in (first, first + 1, first + 3):
            ps.cell(total, c, f"=SUM({col(c)}5:{col(c)}{max(end, 5)})")
        ps.cell(total, first + 2, f'=IF({col(first)}{total}=0,"",{col(first + 1)}{total}/{col(first)}{total})')
    ps.cell(total, tot_c, f"=SUM({col(tot_c)}5:{col(tot_c)}{max(end, 5)})")
    money = {f + 3 for f in firsts.values()} | {tot_c}
    for r in range(5, total + 1):
        for c in range(1, by_c + 1):
            cell = ps.cell(r, c)
            cell.font = BOLD if r == total else BODY
            if c in money:
                cell.number_format = RUPEES
            elif c in {f + 2 for f in firsts.values()}:
                cell.number_format = "0%"
            elif c in (best_c, by_c):
                cell.alignment = Alignment(horizontal="center")
    for c in money:
        gain_loss(ps, f"{col(c)}5:{col(c)}{total}")
    if len(order):
        ps.auto_filter.ref = f"A4:{col(by_c)}{end}"
    for i, system in enumerate(systems):
        ps.cell(total + 2 + i, 1, f"Stocks traded by 2+ strategies where {system} did best").font = BODY
        ps.cell(total + 2 + i, tot_c, f'=COUNTIFS({col(best_c)}5:{col(best_c)}{max(end, 5)},"{system}",'
                                      f'{col(by_c)}5:{col(by_c)}{max(end, 5)},">1")')

    # Closed trades --------------------------------------------------------------
    ct = wb.create_sheet("Closed trades")
    header(ct, 1, ["Symbol", "Strategy", "Style", "Side", "Timeframe", "Qty", "Entry", "Exit", "Entered", "Exited",
                   "Reason", "Gross ₹", "Charges ₹", "Net ₹", "Result", "SL", "Target", "High", "Low"],
           [14, 9, 24, 8, 10, 8, 10, 10, 16, 16, 26, 12, 11, 12, 8, 10, 10, 10, 10])
    ct.freeze_panes = "B2"

    def number(value):
        return None if value is None or pd.isna(value) else float(value)

    for r, x in enumerate(t.itertuples(index=False), 2):
        values = [x.symbol, x.system, styles.get(x.style, x.style), "LONG" if x.side == "BUY" else "SHORT",
                  x.Timeframe, number(x.qty), number(x.entry), number(x.exit), x.Entered.to_pydatetime(),
                  x.Exited.to_pydatetime(), x.reason, number(x.gross), number(x.charges)]
        for c, value in enumerate(values, 1):
            ct.cell(r, c, value)
        ct.cell(r, 14, f"=L{r}-M{r}")
        ct.cell(r, 15, f'=IF(N{r}>0,"Win","Loss")')
        for c, value in zip(range(16, 20), (x.sl, x.tp, x.high, x.low)):
            ct.cell(r, c, number(value))
        for c in range(1, 20):
            cell = ct.cell(r, c)
            cell.font = BODY
            if c in (9, 10):
                cell.number_format = "dd mmm hh:mm"
            elif c in (12, 13, 14):
                cell.number_format = RUPEES
    if len(t):
        ct.auto_filter.ref = f"A1:S{last}"
        gain_loss(ct, f"N2:N{last}")

    wb.calculation.fullCalcOnLoad = True
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def open_workbook(positions: list[dict], as_of: str) -> bytes:
    """The report of the open positions: one row each (Symbol, Side, Strategy, Style,
    Signal, Qty, Entry, LTP, SL / TSL, Target, At target, Entered, Agrees) as the page
    shows them, with the unrealized P&L as a formula, and a summary by strategy and side."""
    wb = Workbook()
    sm = wb.active
    sm.title = "Summary"
    sm["A1"], sm["A1"].font = f"Auto trader: open positions ({as_of})", TITLE
    sm["A2"] = "Unrealized P&L at the last traded price (LTP) when the file was made; it excludes charges."
    sm["A2"].font = NOTE

    op = wb.create_sheet("Open positions")
    names = ["Symbol", "Side", "Strategy", "Style", "Signal", "Qty", "Entry", "LTP", "SL / TSL", "Target",
             "At target", "Unrealized ₹", "Unrealized %", "Entered", "Agrees"]
    header(op, 1, names, [14, 8, 9, 24, 13, 8, 10, 10, 10, 10, 16, 13, 12, 16, 18])
    op.freeze_panes = "B2"
    last = len(positions) + 1
    for r, p in enumerate(positions, 2):
        for c, name in enumerate(names, 1):
            if name in ("Unrealized ₹", "Unrealized %"):
                continue
            value = p.get(name)
            op.cell(r, c, None if value is None or (isinstance(value, float) and pd.isna(value)) else value)
        sign = f'IF(B{r}="LONG",1,-1)'
        op.cell(r, 12, f'=IF(H{r}="","",(H{r}-G{r})*F{r}*{sign})')
        op.cell(r, 13, f'=IF(H{r}="","",(H{r}-G{r})/G{r}*{sign})')
        for c in range(1, len(names) + 1):
            cell = op.cell(r, c)
            cell.font = BODY
            if c == 12:
                cell.number_format = RUPEES
            elif c == 13:
                cell.number_format = "0.00%"
            elif c == 14:
                cell.number_format = "dd mmm hh:mm"
    if positions:
        op.auto_filter.ref = f"A1:O{last}"
        gain_loss(op, f"L2:M{last}")

    def rows(letter: str) -> str:
        return f"'Open positions'!${letter}$2:${letter}${max(last, 2)}"

    header(sm, 4, ["Strategy", "Side", "Positions", "In profit", "Unrealized ₹"], [12, 10, 11, 10, 15])
    groups = [(s, d) for s in sorted({p.get("Strategy") or "RSI" for p in positions} or {"HA", "RSI"})
              for d in ("LONG", "SHORT")]
    r = 5
    for system, side in groups + [("All", "All")]:
        sm.cell(r, 1, system)
        sm.cell(r, 2, side)
        match = "" if system == "All" else f"{rows('C')},$A{r},{rows('B')},$B{r}"
        if match:
            sm.cell(r, 3, f"=COUNTIFS({match})")
            sm.cell(r, 4, f'=COUNTIFS({match},{rows("L")},">0")')
            sm.cell(r, 5, f"=SUMIFS({rows('L')},{match})")
        else:
            sm.cell(r, 3, f"=SUM(C5:C{r - 1})")
            sm.cell(r, 4, f"=SUM(D5:D{r - 1})")
            sm.cell(r, 5, f"=SUM(E5:E{r - 1})")
        for c in range(1, 6):
            sm.cell(r, c).font = BOLD if system == "All" else BODY
        sm.cell(r, 5).number_format = RUPEES
        r += 1
    gain_loss(sm, f"E5:E{r - 1}")

    wb.calculation.fullCalcOnLoad = True
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
