// Futures Trade Simulator: the browser side of futures_simulator.py.
// The account (capital, open positions, trade log) lives in localStorage. Python
// sends instruments, 2H signals and prices in `data`; the simulator sends back the
// symbols it needs prices for (state "symbols") and checks of its open positions
// (trigger "check"), answered in data.result.

const STORE = "futures_simulator_v1"
const PREFS = "futures_simulator_prefs"  // signal timeframe, entry mode, SL/target on or off, chart site
const SIGNALS = "futures_simulator_signals"  // session date -> the signals last seen in that session
const SESSION_CLOSE = "15:30"  // carried-over signals are shown until the next session closes
const instances = new WeakMap()

// ---------------------------------------------------------------------------
// Account
// ---------------------------------------------------------------------------
function freshAccount(capital) {
  return { capital, positions: [], history: [], applied: [], seq: 0 }
}

function loadAccount(capital) {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE))
    if (saved && Array.isArray(saved.positions)) return { ...freshAccount(capital), ...saved }
  } catch (e) { /* storage blocked or corrupt: start fresh */ }
  return freshAccount(capital)
}

function saveAccount(account) {
  try { localStorage.setItem(STORE, JSON.stringify(account)) } catch (e) { /* storage blocked */ }
}

function storageWorks() {
  try {
    localStorage.setItem(`${STORE}_test`, "1")
    localStorage.removeItem(`${STORE}_test`)
    return true
  } catch (e) { return false }
}

// The account as last saved, possibly by another tab of the page, before changing it.
function latest(ctx) {
  if (ctx.storageOk) ctx.account = loadAccount(ctx.data.settings.start_capital)
}

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS)) || {} } catch (e) { return {} }
}

function savePrefs(ctx) {
  const prefs = { timeframe: ctx.timeframe, entry: ctx.ref("entry-mode").value, risk: ctx.ref("auto-risk").checked,
    chart: ctx.ref("chart-site").value }
  try { localStorage.setItem(PREFS, JSON.stringify(prefs)) } catch (e) { /* storage blocked */ }
}

// Today's signals with the previous session's added, until the current session closes.
// Each page load saves the signals under the session they belong to, so the list
// carried over is the last one seen in that session. Carried signals are marked with
// their session date (`from`).
function withCarriedSignals(data) {
  const live = data.signals, today = data.session_date
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(SIGNALS)) || {} } catch (e) { /* storage blocked */ }
  saved[today] = live.map(({ symbol, timeframe, strategy, bias, agrees, fno, open }) =>
    ({ symbol, timeframe, strategy, bias, agrees, fno, open }))
  const dates = Object.keys(saved).sort().slice(-5)
  try { localStorage.setItem(SIGNALS, JSON.stringify(Object.fromEntries(dates.map((d) => [d, saved[d]])))) } catch (e) { /* storage blocked */ }

  const previous = dates.filter((d) => d < today).pop()
  const now = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Kolkata" })  // "YYYY-MM-DD HH:MM:SS"
  if (!previous || now >= `${today} ${SESSION_CLOSE}`) return live
  const current = new Set(live.map((s) => `${s.symbol}|${s.timeframe}`))
  return [...live, ...saved[previous]
    .filter((s) => !current.has(`${s.symbol}|${s.timeframe}`))
    .map((s) => ({ ...s, from: previous }))]
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const tick = (x) => Math.round(x * 20) / 20  // NSE futures tick size ₹0.05
const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : null }
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]))
const inr = (x, digits = 2) => x == null ? "—" :
  "₹" + Number(x).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })
const signed = (x) => x == null ? "—" : `<span class="${x >= 0 ? "pos" : "neg"}">${x >= 0 ? "+" : "−"}${inr(Math.abs(x))}</span>`
const sessionDay = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "UTC" })
const when = (ms) => ms ? new Date(ms).toLocaleString("en-IN", {
  timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
}) : "—"

function charges(buyValue, sellValue, rates) {
  const brokerage = [buyValue, sellValue]
    .reduce((sum, v) => sum + Math.min(rates.brokerage_per_order, v * rates.brokerage_rate), 0)
  const exchange = (buyValue + sellValue) * rates.exchange
  const sebi = (buyValue + sellValue) * rates.sebi
  const stt = sellValue * rates.stt_sell
  const stamp = buyValue * rates.stamp_buy
  const gst = (brokerage + exchange + sebi) * rates.gst
  return brokerage + exchange + sebi + stt + stamp + gst
}

function pnl(position, price) {
  return (position.side === "BUY" ? price - position.entry : position.entry - price) * position.qty
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export default function (component) {
  const { data, parentElement, setStateValue, setTriggerValue } = component
  let ctx = instances.get(parentElement)
  const first = !ctx
  if (first) {
    ctx = setup(parentElement, data)
    instances.set(parentElement, ctx)
  }
  ctx.data = data
  ctx.signals = withCarriedSignals(data)
  ctx.setStateValue = setStateValue
  ctx.setTriggerValue = setTriggerValue

  applyResult(ctx, data.result)
  fillPrice(ctx)
  render(ctx)
  syncSymbols(ctx)
  // Check open positions against the bars since entry each time the page opens.
  if (first && ctx.account.positions.length) setTimeout(() => sendCheck(ctx, "auto"), 0)
}

function setup(root, data) {
  const ref = (name) => root.querySelector(`[data-ref="${name}"]`)
  const ctx = {
    root, ref,
    account: loadAccount(data.settings.start_capital),
    side: "BUY",
    priceEdited: false,  // entry price typed in: stop filling it from the LTP
    pending: null,       // id of the check waiting for an answer
    sentSymbols: "",
    storageOk: storageWorks(),
  }
  ref("storage-warning").hidden = ctx.storageOk

  // Another tab of the page changed the account: show its version.
  const onStorage = (event) => {
    if (!root.isConnected) { window.removeEventListener("storage", onStorage); return }
    if (event.key !== STORE) return
    latest(ctx)
    render(ctx)
    syncSymbols(ctx)
  }
  window.addEventListener("storage", onStorage)

  ref("instruments").innerHTML = data.instruments
    .map((i) => `<option value="${esc(i.symbol)}">${esc(i.name)} · lot ${i.lot}</option>`).join("")
  ref("capital").value = ctx.account.capital
  const prefs = loadPrefs()
  ctx.timeframe = data.timeframes.includes(prefs.timeframe) ? prefs.timeframe : data.settings.default_timeframe
  if (prefs.entry) ref("entry-mode").value = prefs.entry
  if (prefs.risk === false) ref("auto-risk").checked = false
  const sites = Object.keys(data.charts)
  ref("chart-site").innerHTML = sites.map((site) => `<option>${esc(site)}</option>`).join("")
  ref("chart-site").value = [prefs.chart, data.default_chart].find((site) => sites.includes(site)) || sites[0]
  ref("timeframes").innerHTML = data.timeframes
    .map((tf) => `<button type="button" data-tf="${esc(tf)}">${esc(tf.replace(" HOUR", "H"))}</button>`).join("")

  root.addEventListener("change", (event) => {
    if (["entry-mode", "auto-risk", "chart-site"].includes(event.target.dataset.ref)) {
      savePrefs(ctx)
      render(ctx)
    }
  })

  root.addEventListener("click", (event) => {
    const button = event.target.closest("button")
    if (!button || !root.contains(button)) return
    if (button.dataset.side) {
      ctx.side = button.dataset.side
      render(ctx)
      return
    }
    if (button.dataset.tf) {
      ctx.timeframe = button.dataset.tf
      savePrefs(ctx)
      render(ctx)
      return
    }
    const act = button.dataset.act
    const actions = {
      refresh: () => sendCheck(ctx, "auto", true),
      eod: () => runEod(ctx),
      "auto-all": () => autoExecute(ctx, shownSignals(ctx)),
      "auto-one": () => autoExecute(ctx, [ctx.signals[Number(button.dataset.row)]]),
      "use-ltp": () => { ctx.priceEdited = false; fillPrice(ctx); render(ctx) },
      preset: () => presetRisk(ctx),
      "clear-risk": () => { ref("sl").value = ""; ref("tp").value = ""; render(ctx) },
      place: () => placeFromForm(ctx),
      exit: () => manualExit(ctx, button.dataset.id),
      export: () => exportCsv(ctx),
      reset: () => resetAccount(ctx),
    }
    if (actions[act]) actions[act]()
  })

  root.addEventListener("input", (event) => {
    const name = event.target.dataset.ref
    if (name === "price") ctx.priceEdited = event.target.value !== ""
    if (name === "symbol") {
      ctx.priceEdited = false
      ref("price").value = ""
      syncSymbols(ctx)
      fillPrice(ctx)
    }
    render(ctx)
  })
  return ctx
}

function instrument(ctx, symbol) {
  return ctx.data.instruments.find((i) => i.symbol === symbol)
}

function quote(ctx, symbol) {
  return ctx.data.quotes[symbol]?.price ?? null
}

// Symbols that need a price: open positions, carried-over signals and the instrument
// in the order form.
function syncSymbols(ctx) {
  const symbols = new Set(ctx.account.positions.map((p) => p.symbol))
  for (const s of ctx.signals) if (s.from && s.fno) symbols.add(s.symbol)
  const picked = ctx.ref("symbol").value.trim().toUpperCase()
  if (instrument(ctx, picked)) symbols.add(picked)
  const list = [...symbols].sort()
  const key = list.join(",")
  if (key !== ctx.sentSymbols) {
    ctx.sentSymbols = key
    ctx.setStateValue("symbols", list)
  }
}

function fillPrice(ctx) {
  const ltp = quote(ctx, ctx.ref("symbol").value.trim().toUpperCase())
  if (!ctx.priceEdited && ltp != null) ctx.ref("price").value = tick(ltp).toFixed(2)
}

function toast(ctx, message, bad = false) {
  const box = ctx.ref("toast")
  box.textContent = message
  box.className = "toast" + (bad ? " bad" : "")
  box.hidden = false
  clearTimeout(ctx.toastTimer)
  ctx.toastTimer = setTimeout(() => { box.hidden = true }, 7000)
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------
function balance(ctx) {
  return ctx.account.capital + ctx.account.history.reduce((sum, t) => sum + t.net, 0)
}

function usedMargin(ctx) {
  const rate = ctx.data.settings.margin_rate
  return ctx.account.positions.reduce((sum, p) => sum + p.entry * p.qty * rate, 0)
}

// The order the form describes, with an error message if it cannot be placed.
function formOrder(ctx) {
  const ref = ctx.ref
  const symbol = ref("symbol").value.trim().toUpperCase()
  const info = instrument(ctx, symbol)
  const lots = Math.floor(num(ref("lots").value) ?? 0)
  const order = {
    symbol, side: ctx.side, lots, entry: num(ref("price").value),
    qty: info ? lots * info.lot : 0, sl: num(ref("sl").value), tp: num(ref("tp").value),
    tsl: { on: ref("tsl-on").checked, trail: num(ref("tsl-trail").value) ?? 0, step: num(ref("tsl-step").value) ?? 0 },
  }
  const long = order.side === "BUY"
  if (!info) order.error = symbol ? `${symbol} is not in futures stocks.csv.` : "Pick an instrument."
  else if (!order.entry || order.entry <= 0) order.error = "Enter an entry price."
  else if (lots < 1) order.error = "Lots must be 1 or more."
  else if (order.sl != null && (long ? order.sl >= order.entry : order.sl <= order.entry))
    order.error = `Stop loss must be ${long ? "below" : "above"} the entry price for a ${long ? "long" : "short"}.`
  else if (order.tp != null && (long ? order.tp <= order.entry : order.tp >= order.entry))
    order.error = `Target must be ${long ? "above" : "below"} the entry price for a ${long ? "long" : "short"}.`
  else if (order.tsl.on && (order.tsl.trail <= 0 || order.tsl.step <= 0))
    order.error = "Trailing SL needs a trail and a jump step above ₹0."
  else if (order.entry * order.qty * ctx.data.settings.margin_rate > balance(ctx) - usedMargin(ctx))
    order.error = "Not enough margin available."
  return order
}

function placeOrder(ctx, order) {
  const account = ctx.account
  account.seq += 1
  account.positions.push({
    id: `${Date.now()}-${account.seq}`, symbol: order.symbol, side: order.side, lots: order.lots,
    qty: order.qty, entry: order.entry, sl: order.sl, tp: order.tp,
    tsl: order.tsl.on ? order.tsl : { on: false }, stop: order.sl, time: order.time || Date.now(),
    source: order.source || "Manual", status: "open",
  })
  saveAccount(account)
}

function placeFromForm(ctx) {
  latest(ctx)
  const order = formOrder(ctx)
  if (order.error) {
    ctx.ref("order-error").textContent = order.error
    return
  }
  placeOrder(ctx, order)
  toast(ctx, `${order.side === "BUY" ? "Bought" : "Sold"} ${order.lots} lot(s) of ${order.symbol} at ${inr(order.entry)}.`)
  render(ctx)
  syncSymbols(ctx)
}

function presetRisk(ctx) {
  const entry = num(ctx.ref("price").value)
  if (!entry) return
  const { auto_sl_pct: slPct, auto_tp_pct: tpPct } = ctx.data.settings
  const long = ctx.side === "BUY"
  ctx.ref("sl").value = tick(entry * (1 + (long ? -slPct : slPct) / 100)).toFixed(2)
  ctx.ref("tp").value = tick(entry * (1 + (long ? tpPct : -tpPct) / 100)).toFixed(2)
  render(ctx)
}

function shownSignals(ctx) {
  return ctx.signals.filter((s) => s.timeframe === ctx.timeframe)
}

const shortTf = (tf) => tf.replace(" HOUR", "H")

// Entry price and time of a signal: the latest price, or the open of the candle it is on.
function signalEntry(ctx, signal) {
  if (ctx.ref("entry-mode").value === "candle") return signal.open ? { price: signal.open.price, time: signal.open.time } : null
  const ltp = quote(ctx, signal.symbol)
  return ltp == null ? null : { price: tick(ltp), time: null }
}

// Market orders for signals, with the preset SL and target if ticked.
function autoExecute(ctx, signals) {
  latest(ctx)
  const { auto_sl_pct: slPct, auto_tp_pct: tpPct } = ctx.data.settings
  const lots = Math.max(1, Math.floor(num(ctx.ref("auto-lots").value) ?? 1))
  const risk = ctx.ref("auto-risk").checked
  const bySymbol = {}
  for (const s of signals) (bySymbol[s.symbol] ||= []).push(s)
  const placed = [], skipped = []
  for (const [symbol, found] of Object.entries(bySymbol)) {
    const info = instrument(ctx, symbol)
    const biases = new Set(found.map((s) => s.bias))
    if (biases.size > 1) { skipped.push(`${symbol} (mixed signals)`); continue }
    const side = biases.has("Bullish") ? "BUY" : "SELL"
    if (!info) { skipped.push(`${symbol} (not F&O)`); continue }
    const start = signalEntry(ctx, found[0])
    if (!start) { skipped.push(`${symbol} (no price)`); continue }
    if (ctx.account.positions.some((p) => p.symbol === symbol && p.side === side)) {
      skipped.push(`${symbol} (already open)`); continue
    }
    const entry = start.price, long = side === "BUY"
    const order = {
      symbol, side, lots, qty: lots * info.lot, entry, time: start.time,
      source: `Auto ${shortTf(found[0].timeframe)}`,
      sl: risk ? tick(entry * (1 + (long ? -slPct : slPct) / 100)) : null,
      tp: risk ? tick(entry * (1 + (long ? tpPct : -tpPct) / 100)) : null,
      tsl: { on: false },
    }
    if (entry * order.qty * ctx.data.settings.margin_rate > balance(ctx) - usedMargin(ctx)) {
      skipped.push(`${symbol} (margin)`); continue
    }
    placeOrder(ctx, order)
    placed.push(symbol)
  }
  const message = (placed.length ? `Placed ${placed.length}: ${placed.join(", ")}.` : "No orders placed.") +
    (skipped.length ? ` Skipped ${skipped.join(", ")}.` : "")
  toast(ctx, message, !placed.length)
  render(ctx)
  syncSymbols(ctx)
  // An entry in the past may have hit its SL or target already.
  if (placed.length && ctx.ref("entry-mode").value === "candle") sendCheck(ctx, "auto")
}

// ---------------------------------------------------------------------------
// Settlement
// ---------------------------------------------------------------------------
function sendCheck(ctx, mode, refresh = false) {
  latest(ctx)
  const positions = ctx.account.positions
  if (!positions.length && !refresh) return
  ctx.pending = `${Date.now()}-${mode}`
  ctx.setTriggerValue("check", {
    id: ctx.pending, mode, refresh,
    positions: positions.map(({ id, symbol, side, entry, sl, tp, tsl, time }) => ({ id, symbol, side, entry, sl, tp, tsl, time })),
  })
  render(ctx)
}

function runEod(ctx) {
  if (!ctx.account.positions.length) { toast(ctx, "No open positions to settle."); return }
  sendCheck(ctx, "eod")
}

function closePosition(ctx, position, price, time, reason) {
  const rates = ctx.data.settings.charges
  const value = (p) => p * position.qty
  const [buy, sell] = position.side === "BUY" ? [position.entry, price] : [price, position.entry]
  const gross = pnl(position, price)
  const cost = charges(value(buy), value(sell), rates)
  ctx.account.history.push({
    id: position.id, symbol: position.symbol, side: position.side, lots: position.lots, qty: position.qty,
    entry: position.entry, exit: price, entry_time: position.time, exit_time: time, reason,
    source: position.source, gross, charges: cost, net: gross - cost,
  })
  ctx.account.positions = ctx.account.positions.filter((p) => p.id !== position.id)
}

function applyResult(ctx, result) {
  if (result) latest(ctx)
  if (!result || ctx.account.applied.includes(result.id)) {
    if (result && result.id === ctx.pending) ctx.pending = null
    return
  }
  ctx.account.applied = [...ctx.account.applied.slice(-30), result.id]
  if (result.id === ctx.pending) ctx.pending = null
  const closed = [], errors = []
  for (const r of result.results) {
    const position = ctx.account.positions.find((p) => p.id === r.id)
    if (!position) continue
    if (r.status === "closed") {
      closePosition(ctx, position, r.exit, r.exit_time, r.reason)
      closed.push(`${position.symbol} ${r.reason}`)
    } else if (r.status === "error") {
      errors.push(`${position.symbol}: ${r.error}`)
    } else {
      position.stop = r.stop
      position.status = r.status
    }
  }
  saveAccount(ctx.account)
  if (closed.length || errors.length) {
    toast(ctx, [closed.length && `Closed ${closed.length}: ${closed.join(", ")}.`,
                errors.length && `No prices for ${errors.join("; ")}.`].filter(Boolean).join(" "), !!errors.length)
  } else if (result.mode === "eod") {
    toast(ctx, "Nothing to settle: no trading since these orders were placed.")
  }
}

function manualExit(ctx, id) {
  latest(ctx)
  const position = ctx.account.positions.find((p) => p.id === id)
  const ltp = position && quote(ctx, position.symbol)
  if (ltp == null) return
  closePosition(ctx, position, tick(ltp), Date.now(), "Manual exit")
  saveAccount(ctx.account)
  toast(ctx, `Exited ${position.symbol} at ${inr(tick(ltp))}.`)
  render(ctx)
  syncSymbols(ctx)
}

function resetAccount(ctx) {
  const capital = num(ctx.ref("capital").value) ?? ctx.data.settings.start_capital
  if (!confirm(`Reset the account to ${inr(capital, 0)}? Open positions and the trade log are deleted.`)) return
  ctx.account = freshAccount(capital)
  saveAccount(ctx.account)
  render(ctx)
  syncSymbols(ctx)
}

function exportCsv(ctx) {
  const columns = ["symbol", "side", "lots", "qty", "entry", "exit", "entry_time", "exit_time", "reason", "source", "gross", "charges", "net"]
  const cell = (t, c) => c.endsWith("_time") ? new Date(t[c]).toISOString()
    : typeof t[c] === "number" ? t[c].toFixed(2).replace(/\.00$/, "") : `"${String(t[c] ?? "").replace(/"/g, '""')}"`
  const lines = [columns.join(","), ...ctx.account.history.map((t) => columns.map((c) => cell(t, c)).join(","))]
  const link = document.createElement("a")
  link.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }))
  link.download = "futures_trades.csv"
  link.click()
  URL.revokeObjectURL(link.href)
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function render(ctx) {
  renderStats(ctx)
  renderSignals(ctx)
  renderOrder(ctx)
  renderPositions(ctx)
  renderLedger(ctx)
}

function renderStats(ctx) {
  const open = ctx.account.positions
    .reduce((sum, p) => { const ltp = quote(ctx, p.symbol); return ltp == null ? sum : sum + pnl(p, ltp) }, 0)
  const used = usedMargin(ctx)
  ctx.ref("stats").innerHTML = [
    ["Balance", inr(balance(ctx))],
    ["Used margin", inr(used)],
    ["Available", inr(balance(ctx) - used)],
    ["Open P&L", signed(open)],
    ["Positions", ctx.account.positions.length],
  ].map(([label, value]) => `<div class="stat"><span class="muted small">${label}</span><b>${value}</b></div>`).join("")
  ctx.root.querySelectorAll('[data-act="refresh"], [data-act="eod"]').forEach((b) => { b.disabled = !!ctx.pending })
  ctx.root.querySelector('[data-act="eod"]').textContent = ctx.pending ? "Checking…" : "⏱ Run EOD settlement"
}

// A stock's name, linked to its chart on the site picked under Charts.
function symbolLink(ctx, symbol) {
  const site = ctx.ref("chart-site").value
  const link = (ctx.data.charts[site] || Object.values(ctx.data.charts)[0]).replace("SYMBOL", encodeURIComponent(symbol))
  return `<a class="sym" href="${esc(link)}" target="_blank" rel="noopener" title="Open the chart on ${esc(site)}"><b>${esc(symbol)}</b></a>`
}

function renderSignals(ctx) {
  const { signals_note: note, settings } = ctx.data
  const signals = ctx.signals
  const tf = shortTf(ctx.timeframe)
  const candle = ctx.ref("entry-mode").value === "candle"
  const risk = ctx.ref("auto-risk").checked
  ctx.ref("timeframes").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.tf === ctx.timeframe))
  ctx.ref("signals-note").textContent = note
  ctx.ref("auto-risk-label").textContent = `SL ${settings.auto_sl_pct}% / target ${settings.auto_tp_pct}%`
  ctx.ref("auto-note").textContent = `Auto orders buy bullish and sell bearish ${tf} signals ` +
    (candle ? `at the open of the ${tf} candle the signal is on, timed at that candle's start` : "at the latest price") +
    (risk ? `, with SL ${settings.auto_sl_pct}% and target ${settings.auto_tp_pct}% away.` : ", with no SL or target.") +
    " Agrees: the Market reports Active Watchlist has the same bias." +
    " Signals with a date are from the last session, kept until 3:30 PM."
  const agree = { Yes: "yes", No: "no", "Not on watchlist": "none" }
  const shown = signals.map((s, row) => ({ ...s, row })).filter((s) => s.timeframe === ctx.timeframe)
  ctx.ref("signals").innerHTML = `<tr><th>Symbol</th><th>Strategy</th><th>Bias</th><th>Agrees</th>
    <th class="r">LTP</th><th class="r">${tf} candle open</th><th class="r">Lot</th><th></th></tr>` + (shown.length ? shown.map((s) => {
    const ltp = quote(ctx, s.symbol), info = instrument(ctx, s.symbol), start = signalEntry(ctx, s)
    const open = ctx.account.positions.some((p) => p.symbol === s.symbol && p.side === (s.bias === "Bullish" ? "BUY" : "SELL"))
    const label = !info ? "Not F&O" : open ? "Open" : !start ? "No price" : "⚡ Execute"
    const from = s.from ? ` <span class="tag" title="From the last session">${esc(sessionDay(s.from))}</span>` : ""
    return `<tr><td>${symbolLink(ctx, s.symbol)}${from}</td><td class="muted">${esc(s.strategy)}</td>
      <td><span class="pill ${s.bias === "Bullish" ? "long" : "short"}">${s.bias}</span></td>
      <td><span class="pill ${agree[s.agrees]}">${esc(s.agrees)}</span></td>
      <td class="r">${inr(ltp)}</td>
      <td class="r">${s.open ? `${inr(s.open.price)} <span class="muted small">${when(s.open.time)}</span>` : "—"}</td>
      <td class="r">${info ? info.lot : "—"}</td>
      <td class="r"><button type="button" class="btn xs ${s.bias === "Bullish" ? "buy" : "sell"}" data-act="auto-one"
        data-row="${s.row}" ${!info || open || !start ? "disabled" : ""}>${label}</button></td></tr>`
  }).join("") : `<tr><td class="empty" colspan="8">No stocks on the ${tf} Chartink widgets right now.</td></tr>`)
  ctx.root.querySelector('[data-act="auto-all"]').disabled = !shown.some((s) => s.fno)
  ctx.root.querySelector('[data-act="auto-all"]').textContent = `⚡ Auto-execute all ${tf} signals`
}

function renderOrder(ctx) {
  const ref = ctx.ref
  ref("side").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.side === ctx.side))
  const order = formOrder(ctx)
  const info = instrument(ctx, order.symbol)
  const ltp = quote(ctx, order.symbol)
  ref("ltp").innerHTML = !info ? "Pick an instrument to load its price"
    : ltp == null ? `${esc(info.name)} · loading price… (or type one)`
    : `${esc(info.name)} · LTP <b>${inr(ltp)}</b> · ${esc(ctx.data.quotes[order.symbol].source)} ${when(ctx.data.quotes[order.symbol].time)}`
  ref("qty").textContent = info ? `Qty ${order.qty.toLocaleString("en-IN")} = ${order.lots || 0} lot(s) × ${info.lot}` : "Qty —"

  const turnover = order.entry && order.qty ? order.entry * order.qty : null
  const risk = turnover && order.sl != null ? Math.abs(order.entry - order.sl) * order.qty : null
  const reward = turnover && order.tp != null ? Math.abs(order.tp - order.entry) * order.qty : null
  const rate = ctx.data.settings.margin_rate
  ref("margin").innerHTML = [
    ["Turnover", inr(turnover)],
    [`Required margin (${Math.round(rate * 100)}%)`, inr(turnover && turnover * rate)],
    ["Max risk (to SL)", risk == null ? "—" : `<span class="neg">${inr(risk)}</span>`],
    ["Max reward (to target)", reward == null ? "—" : `<span class="pos">${inr(reward)}</span>`],
    ["Available margin", inr(balance(ctx) - usedMargin(ctx))],
  ].map(([label, value]) => `<div><span>${label}</span><span>${value}</span></div>`).join("")

  const place = ctx.root.querySelector('[data-act="place"]')
  place.className = `btn place ${ctx.side === "BUY" ? "buy" : "sell"}`
  place.textContent = `${ctx.side} ${info ? `${order.symbol} · ${order.lots || 0} lot(s)` : ""}`.trim()
  ref("order-error").textContent = order.symbol && order.entry ? order.error || "" : ""
}

function renderPositions(ctx) {
  const positions = ctx.account.positions
  ctx.ref("positions-note").textContent = ctx.pending ? "Checking against the latest bars…"
    : "SL, trailing SL and target are checked on the 5-minute bars when the page opens, on Refresh and at EOD."
  ctx.ref("positions").innerHTML = `<tr><th>Symbol</th><th>Side</th><th class="r">Qty</th><th class="r">Avg entry</th>
    <th class="r">LTP</th><th class="r">SL / TSL</th><th class="r">Target</th><th class="r">Unrealized P&amp;L</th>
    <th>Entered</th><th></th></tr>` + (positions.length ? positions.map((p) => {
    const ltp = quote(ctx, p.symbol)
    const stop = p.stop == null ? "—" : inr(p.stop) + (p.tsl?.on ? (p.stop !== p.sl ? " ↗ TSL" : " · TSL") : "")
    const status = p.status === "waiting" ? '<span class="muted small"> · waits for market</span>' : ""
    return `<tr><td>${symbolLink(ctx, p.symbol)}<div class="muted small">${esc(p.source)}</div></td>
      <td><span class="pill ${p.side === "BUY" ? "long" : "short"}">${p.side === "BUY" ? "LONG" : "SHORT"}</span></td>
      <td class="r">${p.qty.toLocaleString("en-IN")}</td><td class="r">${inr(p.entry)}</td>
      <td class="r">${inr(ltp)}${ltp == null ? "" : `<div class="muted small">${esc(ctx.data.quotes[p.symbol].source)} ${when(ctx.data.quotes[p.symbol].time)}</div>`}</td>
      <td class="r">${stop}</td><td class="r">${inr(p.tp)}</td><td class="r">${ltp == null ? "—" : signed(pnl(p, ltp))}</td>
      <td>${when(p.time)}${status}</td>
      <td class="r"><button type="button" class="btn xs ghost" data-act="exit" data-id="${esc(p.id)}"
        ${ltp == null ? "disabled" : ""}>Exit at LTP</button></td></tr>`
  }).join("") : `<tr><td class="empty" colspan="10">No open positions. Place an order or auto-execute a signal.</td></tr>`)
}

function renderLedger(ctx) {
  const trades = ctx.account.history
  const sum = (key) => trades.reduce((s, t) => s + t[key], 0)
  const wins = trades.filter((t) => t.net > 0).length
  ctx.ref("tiles").innerHTML = [
    ["Trades settled", trades.length],
    ["Win rate (net)", trades.length ? `${(100 * wins / trades.length).toFixed(1)}%` : "—"],
    ["Gross P&L", signed(sum("gross"))],
    ["Net P&L", signed(sum("net"))],
    ["Brokerage & taxes", inr(sum("charges"))],
    ["Return on capital", ctx.account.capital ? `${(100 * sum("net") / ctx.account.capital).toFixed(2)}%` : "—"],
  ].map(([label, value]) => `<div class="tile"><span class="muted small">${label}</span><b>${value}</b></div>`).join("")
  ctx.ref("history").innerHTML = `<tr><th>Exit</th><th>Symbol</th><th>Side</th><th class="r">Qty</th>
    <th class="r">Entry</th><th class="r">Exit price</th><th>Reason</th><th class="r">Gross</th>
    <th class="r">Charges</th><th class="r">Net</th></tr>` + (trades.length ? trades.slice().reverse().map((t) => `
    <tr><td>${when(t.exit_time)}</td><td><b>${esc(t.symbol)}</b><div class="muted small">${esc(t.source)}</div></td>
      <td><span class="pill ${t.side === "BUY" ? "long" : "short"}">${t.side === "BUY" ? "LONG" : "SHORT"}</span></td>
      <td class="r">${t.qty.toLocaleString("en-IN")}</td><td class="r">${inr(t.entry)}</td><td class="r">${inr(t.exit)}</td>
      <td>${esc(t.reason)}</td><td class="r">${signed(t.gross)}</td><td class="r">${inr(t.charges)}</td>
      <td class="r">${signed(t.net)}</td></tr>`).join("")
    : `<tr><td class="empty" colspan="10">No settled trades yet.</td></tr>`)
}
