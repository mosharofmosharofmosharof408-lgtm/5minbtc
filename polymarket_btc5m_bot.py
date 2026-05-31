"""
Polymarket BTC 5-Minute Dual-Strike Bot — v3
=============================================
Fix: CLOB /book endpoint was returning 0.99 for both tokens (empty orderbook fallback).
     Correct real-time price source: CLOB /price?token_id=X&side=buy (best ask, no auth needed)
     Fallback: Gamma outcomePrices (re-fetched every tick, not cached)
"""

import os, time, json, threading, logging
from datetime import datetime, timezone
import requests
from flask import Flask, render_template_string

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DEMO_MODE        = True
STARTING_BALANCE = 1000.0
TRADE1_SHARES    = 15
TRADE2_SHARES    = 25
MAX_ENTRY_PRICE  = 0.92
MIN_ENTRY_PRICE  = 0.30
VALUE_THRESHOLD  = 0.82     # Trade 2 only if token still below this
TAKER_FEE        = 0.02
TRADE1_OFFSET    = 60       # Seconds into window → Trade 1
TRADE2_OFFSET    = 210      # Seconds into window → Trade 2
PRICE_INTERVAL   = 1        # Price ticker refresh (seconds)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("BTC5M")

# ─── SHARED STATE ─────────────────────────────────────────────────────────────
state = {
    "balance":            STARTING_BALANCE,
    "total_pnl":          0.0,
    "wins":               0,
    "losses":             0,
    "current_window":     0,
    "window_slug":        "—",
    "direction":          "—",
    "up_price":           0.0,
    "down_price":         0.0,
    "up_token":           None,
    "down_token":         None,
    "price_source":       "—",   # "CLOB" or "Gamma"
    "positions":          [],
    "closed_trades":      [],
    "log_lines":          [],
    "status":             "Starting…",
    "prev_window_result": "—",
    "time_in_window":     0,
    "window_close_in":    300,
    "last_tick":          "—",
    "tick_count":         0,
    "trade1_done":        False,
    "trade2_done":        False,
}
lock = threading.Lock()

# ─── UTILITIES ────────────────────────────────────────────────────────────────
def now_ts():
    return int(time.time())

def current_window_ts():
    t = now_ts()
    return t - (t % 300)

def window_slug(ts):
    return f"btc-updown-5m-{ts}"

def utc_time():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def add_log(msg):
    line = f"[{utc_time()}] {msg}"
    log.info(msg)
    with lock:
        state["log_lines"].append(line)
        if len(state["log_lines"]) > 60:
            state["log_lines"].pop(0)

# ─── PRICE FETCHING — THREE METHODS, BEST AVAILABLE ──────────────────────────

def fetch_clob_price(token_id, side="buy"):
    """
    CLOB /price endpoint — best ask (buy side) for a token.
    Returns float or None. No auth required.
    Docs: GET /price?token_id=<id>&side=buy
    """
    try:
        r = requests.get(
            f"{CLOB_API}/price",
            params={"token_id": token_id, "side": side},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            p = float(data.get("price", 0))
            if 0.01 <= p <= 0.99:   # Sanity check — reject 0.99/0.00 defaults
                return p, "CLOB"
        return None, None
    except Exception as e:
        return None, None

def fetch_clob_midpoint(token_id):
    """
    CLOB /midpoint — mid price between best bid and best ask.
    More stable than best-ask alone.
    """
    try:
        r = requests.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            p = float(data.get("mid", 0))
            if 0.01 <= p <= 0.99:
                return p, "CLOB-mid"
        return None, None
    except:
        return None, None

def fetch_gamma_prices(window_ts):
    """
    Gamma API outcomePrices — refreshed every tick, not cached.
    Used as fallback when CLOB is unavailable.
    Returns (up_price, down_price) or (None, None)
    """
    try:
        url = f"{GAMMA_API}/events?slug={window_slug(window_ts)}"
        r = requests.get(url, timeout=8)
        data = r.json()
        if not data:
            return None, None
        market = data[0]["markets"][0]
        prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
        up  = float(prices[0])
        down = float(prices[1])
        # outcomePrices hitting 0.95+ means window is resolving — valid
        # but both being 0.99 is impossible → reject
        if up == 0.99 and down == 0.99:
            return None, None
        return up, down
    except:
        return None, None

def get_live_prices(up_token, down_token, window_ts):
    """
    Try CLOB /price → CLOB /midpoint → Gamma outcomePrices.
    Returns (up_price, down_price, source_label)
    """
    # Try CLOB /price first
    up_p, src = fetch_clob_price(up_token)
    dn_p, _   = fetch_clob_price(down_token)
    if up_p and dn_p:
        return up_p, dn_p, "CLOB/price"

    # Try CLOB /midpoint
    up_p, src = fetch_clob_midpoint(up_token)
    dn_p, _   = fetch_clob_midpoint(down_token)
    if up_p and dn_p:
        return up_p, dn_p, "CLOB/mid"

    # Fallback: Gamma outcomePrices (re-fetched, not cached)
    up_p, dn_p = fetch_gamma_prices(window_ts)
    if up_p and dn_p:
        return up_p, dn_p, "Gamma"

    return None, None, "None"

# ─── GAMMA EVENT PARSING ──────────────────────────────────────────────────────
def fetch_gamma_event(window_ts):
    try:
        r = requests.get(f"{GAMMA_API}/events?slug={window_slug(window_ts)}", timeout=8)
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        add_log(f"⚠️ Gamma error: {e}")
        return None

def parse_gamma_event(event):
    try:
        market    = event["markets"][0]
        token_ids = json.loads(market["clobTokenIds"])
        prices    = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
        return {
            "market_id":  market["id"],
            "up_token":   token_ids[0],
            "down_token": token_ids[1],
            "up_price":   float(prices[0]),
            "down_price": float(prices[1]),
        }
    except Exception as e:
        add_log(f"⚠️ Parse error: {e}")
        return None

def get_prev_result(prev_window_ts):
    event = fetch_gamma_event(prev_window_ts)
    if not event:
        return None
    m = parse_gamma_event(event)
    if not m:
        return None
    if m["up_price"] >= 0.95:
        return "UP"
    if m["down_price"] >= 0.95:
        return "DOWN"
    return None

# ─── DEMO TRADE ───────────────────────────────────────────────────────────────
def execute_demo_trade(direction, shares, entry_price, trade_num, window_ts):
    cost = shares * entry_price * (1 + TAKER_FEE)
    with lock:
        if state["balance"] < cost:
            add_log(f"⚠️  Insufficient balance for T{trade_num}. Need ${cost:.2f}")
            return False
        state["balance"] -= cost
        state["positions"].append({
            "window":    window_ts,
            "direction": direction,
            "shares":    shares,
            "entry":     entry_price,
            "cost":      cost,
            "trade_num": trade_num,
            "resolved":  False,
            "time":      utc_time(),
        })
    add_log(f"🟢 TRADE {trade_num} | {direction} | {shares} shares @ ${entry_price:.4f} | Cost ${cost:.2f}")
    return True

def resolve_window(window_ts, winning_direction):
    with lock:
        remaining = []
        for pos in state["positions"]:
            if pos["window"] != window_ts or pos["resolved"]:
                remaining.append(pos)
                continue
            won    = (pos["direction"] == winning_direction)
            payout = pos["shares"] * 1.0 if won else 0.0
            pnl    = payout - pos["cost"]
            pos["resolved"] = True
            state["total_pnl"] += pnl
            state["balance"]   += payout
            state["wins" if won else "losses"] += 1
            state["closed_trades"].append({**pos, "won": won, "payout": payout, "pnl": pnl})
            emoji = "✅" if won else "❌"
            add_log(f"{emoji} SETTLED T{pos['trade_num']} | {pos['direction']} | PnL ${pnl:+.2f} | Bal ${state['balance']:.2f}")
        state["positions"] = remaining

# ─── THREAD 1: PRICE TICKER ──────────────────────────────────────────────────
def price_ticker_thread():
    add_log("📡 Price ticker started (1s, CLOB/price → CLOB/mid → Gamma fallback)")
    last_loaded_window = None

    while True:
        try:
            win_ts = current_window_ts()

            # Load token IDs when window changes
            with lock:
                up_token   = state["up_token"]
                down_token = state["down_token"]
                loaded_win = state["current_window"]

            if win_ts != loaded_win or up_token is None:
                event = fetch_gamma_event(win_ts)
                if event:
                    m = parse_gamma_event(event)
                    if m:
                        with lock:
                            state["current_window"] = win_ts
                            state["window_slug"]    = window_slug(win_ts)
                            state["up_token"]       = m["up_token"]
                            state["down_token"]     = m["down_token"]
                            state["up_price"]       = m["up_price"]
                            state["down_price"]     = m["down_price"]
                            state["price_source"]   = "Gamma(seed)"
                        add_log(f"🕐 Window {window_slug(win_ts)} | tokens loaded | Gamma seed UP=${m['up_price']:.4f} DOWN=${m['down_price']:.4f}")
                        up_token   = m["up_token"]
                        down_token = m["down_token"]
                        last_loaded_window = win_ts

            # Fetch live prices every tick
            if up_token and down_token:
                up_p, dn_p, source = get_live_prices(up_token, down_token, win_ts)

                now_t   = now_ts()
                elapsed  = now_t - win_ts
                remaining = 300 - elapsed

                with lock:
                    if up_p:
                        state["up_price"]    = up_p
                    if dn_p:
                        state["down_price"]  = dn_p
                    state["price_source"]    = source
                    state["time_in_window"]  = elapsed
                    state["window_close_in"] = max(0, remaining)
                    state["tick_count"]     += 1
                    state["last_tick"]       = utc_time()

                up_show  = up_p  if up_p  else state["up_price"]
                dn_show  = dn_p  if dn_p  else state["down_price"]
                add_log(f"📈 [{source}] UP ${up_show:.4f} | DOWN ${dn_show:.4f} | T+{elapsed}s | -{remaining}s")

        except Exception as e:
            add_log(f"❌ Ticker error: {e}")

        time.sleep(PRICE_INTERVAL)

# ─── THREAD 2: TRADE LOGIC ───────────────────────────────────────────────────
def trade_logic_thread():
    add_log("🧠 Trade logic thread started")
    last_window   = None
    trade1_done   = False
    trade2_done   = False
    resolved_done = False
    prev_result   = None

    while True:
        try:
            win_ts = current_window_ts()

            with lock:
                elapsed    = state["time_in_window"]
                up_price   = state["up_price"]
                down_price = state["down_price"]
                source     = state["price_source"]

            # ── New window ────────────────────────────────────────────────────
            if win_ts != last_window:
                last_window   = win_ts
                trade1_done   = False
                trade2_done   = False
                resolved_done = False
                prev_result   = None
                with lock:
                    state["trade1_done"]        = False
                    state["trade2_done"]        = False
                    state["direction"]          = "—"
                    state["prev_window_result"] = "Fetching…"
                    state["status"]             = "New window"

                prev_win_ts = win_ts - 300
                add_log(f"🔍 Fetching prev result: {window_slug(prev_win_ts)}")
                result = get_prev_result(prev_win_ts)
                if result:
                    prev_result = result
                    with lock:
                        state["prev_window_result"] = result
                        state["direction"]          = result
                    add_log(f"📊 Prev window: {result} → momentum = {result}")
                else:
                    add_log("⚠️  Prev window not resolved yet")
                    with lock:
                        state["prev_window_result"] = "Pending"

            # ── Retry prev result ─────────────────────────────────────────────
            if not prev_result and elapsed < 45:
                result = get_prev_result(win_ts - 300)
                if result:
                    prev_result = result
                    with lock:
                        state["prev_window_result"] = result
                        state["direction"]          = result
                    add_log(f"📊 Late confirm: {result}")

            # ── Guard: don't trade on bad prices ─────────────────────────────
            prices_valid = (up_price != down_price) and \
                           (0.01 < up_price < 0.99 or 0.01 < down_price < 0.99) and \
                           not (up_price >= 0.98 and down_price >= 0.98)

            # ── TRADE 1 ───────────────────────────────────────────────────────
            if not trade1_done and elapsed >= TRADE1_OFFSET and prev_result:
                if not prices_valid:
                    add_log(f"⚠️  T1 held — prices look invalid ({source}) UP={up_price:.4f} DOWN={down_price:.4f}")
                else:
                    direction   = prev_result
                    entry_price = up_price if direction == "UP" else down_price
                    if MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:
                        add_log(f"⚡ T1 | {direction} @ ${entry_price:.4f} [{source}] T+{elapsed}s")
                        ok = execute_demo_trade(direction, TRADE1_SHARES, entry_price, 1, win_ts)
                        if ok:
                            with lock:
                                state["status"]      = f"T1 placed → {direction}"
                                state["trade1_done"] = True
                    else:
                        add_log(f"🚫 T1 skipped | ${entry_price:.4f} outside [{MIN_ENTRY_PRICE}–{MAX_ENTRY_PRICE}]")
                        with lock:
                            state["status"]      = "T1 skipped (price filter)"
                            state["trade1_done"] = True
                trade1_done = True

            # ── TRADE 2 ───────────────────────────────────────────────────────
            if not trade2_done and elapsed >= TRADE2_OFFSET and prev_result:
                if not prices_valid:
                    add_log(f"⚠️  T2 held — prices look invalid ({source}) UP={up_price:.4f} DOWN={down_price:.4f}")
                else:
                    direction   = prev_result
                    entry_price = up_price if direction == "UP" else down_price
                    if entry_price <= VALUE_THRESHOLD and MIN_ENTRY_PRICE <= entry_price:
                        add_log(f"💎 T2 VALUE | {direction} @ ${entry_price:.4f} (<{VALUE_THRESHOLD}) T+{elapsed}s")
                        ok = execute_demo_trade(direction, TRADE2_SHARES, entry_price, 2, win_ts)
                        if ok:
                            with lock:
                                state["status"]      = f"T2 placed → {direction}"
                                state["trade2_done"] = True
                    elif entry_price > VALUE_THRESHOLD:
                        add_log(f"⏭️  T2 skipped | ${entry_price:.4f} > {VALUE_THRESHOLD} (priced in)")
                        with lock:
                            state["status"]      = "T2 skipped (priced in)"
                            state["trade2_done"] = True
                    else:
                        add_log(f"🚫 T2 skipped | ${entry_price:.4f} below min filter")
                        with lock:
                            state["trade2_done"] = True
                trade2_done = True

            # ── RESOLVE ───────────────────────────────────────────────────────
            remaining = 300 - elapsed
            if not resolved_done and remaining <= 3:
                add_log("⏳ Window closing — waiting 8s for resolution…")
                time.sleep(8)
                winner = get_prev_result(win_ts)
                if winner:
                    resolve_window(win_ts, winner)
                    resolved_done = True
                    with lock:
                        state["status"] = f"Settled → {winner}"
                    add_log(f"🏁 {window_slug(win_ts)} settled → {winner}")
                else:
                    add_log("⚠️  Resolution pending, retrying…")

        except Exception as e:
            add_log(f"❌ Trade logic error: {e}")

        time.sleep(1)

# ─── FLASK DASHBOARD ──────────────────────────────────────────────────────────
app = Flask(__name__)

DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="2">
<title>BTC 5M Bot v3</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#e2e8f0;font-family:'Segoe UI',sans-serif;font-size:14px}
.hdr{background:#111827;padding:14px 16px;border-bottom:2px solid #f7931a;display:flex;justify-content:space-between;align-items:center}
.hdr h1{color:#f7931a;font-size:18px;font-weight:700}
.badge{display:inline-block;background:#1e3a5f;color:#60a5fa;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;margin-left:6px}
.src{font-size:10px;padding:2px 7px;border-radius:8px;margin-left:6px;background:#1e2535;color:#94a3b8}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px}
.card{background:#1a1f2e;border-radius:10px;padding:14px;border:1px solid #2d3748}
.full{grid-column:1/-1}
.lbl{color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.val{font-size:22px;font-weight:700}
.green{color:#22c55e}.red{color:#ef4444}.gold{color:#f7931a}.blue{color:#60a5fa}.gray{color:#64748b}
.bar-wrap{background:#2d3748;border-radius:6px;height:10px;margin-top:8px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#22c55e,#f7931a)}
.px{display:flex;gap:8px;margin-top:10px}
.pbox{flex:1;text-align:center;border-radius:8px;padding:10px 4px;font-weight:700;font-size:17px}
.pup{background:#14532d;color:#22c55e;border:1px solid #22c55e}
.pdown{background:#450a0a;color:#ef4444;border:1px solid #ef4444}
.log{background:#0d1117;border-radius:8px;padding:10px;max-height:260px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.7;color:#64748b}
.log div:last-child{color:#e2e8f0}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#64748b;text-align:left;padding:5px 6px;border-bottom:1px solid #2d3748;font-size:10px;text-transform:uppercase}
td{padding:7px 6px;border-bottom:1px solid #1e2535}
.b{padding:2px 7px;border-radius:10px;font-size:10px;font-weight:700}
.bup{background:#14532d;color:#22c55e}.bdn{background:#450a0a;color:#ef4444}
.t1{color:#a78bfa}.t2{color:#f59e0b}
.status{font-size:11px;color:#94a3b8;margin-top:3px}
.warn{color:#f59e0b;font-size:11px;margin-top:4px}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <h1>⚡ BTC 5-Min Bot <span class="badge">DEMO v3</span><span class="src">src: {{ src }}</span></h1>
    <div class="status">{{ slug }} · {{ status }}</div>
  </div>
  <div style="text-align:right;font-size:11px;color:#64748b">🟢 Tick #{{ ticks }}<br>{{ last_tick }}</div>
</div>

<div class="grid">

  <div class="card">
    <div class="lbl">Balance</div>
    <div class="val gold">${{ "%.2f"|format(balance) }}</div>
    <div style="font-size:12px;margin-top:4px;color:{{ '#22c55e' if pnl>=0 else '#ef4444' }}">P&L {{ "%+.2f"|format(pnl) }}</div>
  </div>

  <div class="card">
    <div class="lbl">Win / Loss</div>
    <div class="val blue">{{ wins }}W &nbsp; {{ losses }}L</div>
    <div style="font-size:12px;margin-top:4px;color:#94a3b8">
      {% if wins+losses>0 %}{{ "%.0f"|format(wins/(wins+losses)*100) }}% rate{% else %}No trades yet{% endif %}
    </div>
  </div>

  <div class="card full">
    <div class="lbl">Window · T+{{ elapsed }}s · closes in {{ close_in }}s</div>
    <div class="bar-wrap"><div class="bar-fill" style="width:{{ [elapsed/300*100,100]|min }}%"></div></div>
    <div class="px">
      <div class="pbox pup">↑ UP &nbsp; ${{ "%.4f"|format(up_price) }}</div>
      <div class="pbox pdown">↓ DOWN ${{ "%.4f"|format(down_price) }}</div>
    </div>
    {% if up_price >= 0.98 and down_price >= 0.98 %}
    <div class="warn">⚠️ Both prices ≥0.98 — price feed issue detected, trades paused</div>
    {% endif %}
    <div style="font-size:11px;color:#64748b;margin-top:8px">
      Prev: <strong style="color:#e2e8f0">{{ prev_result }}</strong>
      &nbsp;·&nbsp; Signal: <strong style="color:#f7931a">{{ direction }}</strong>
      &nbsp;·&nbsp; T1: <span class="{{ 'green' if t1 else 'gray' }}">{{ '✓' if t1 else '…' }}</span>
      &nbsp;·&nbsp; T2: <span class="{{ 'green' if t2 else 'gray' }}">{{ '✓' if t2 else '…' }}</span>
    </div>
  </div>

  <div class="card full">
    <div class="lbl">Open Positions</div>
    {% if positions %}
    <table>
      <tr><th>Trade</th><th>Dir</th><th>Shares</th><th>Entry</th><th>Cost</th><th>Time</th></tr>
      {% for p in positions %}
      <tr>
        <td class="{{ 't1' if p.trade_num==1 else 't2' }}">#{{ p.trade_num }}</td>
        <td><span class="b {{ 'bup' if p.direction=='UP' else 'bdn' }}">{{ p.direction }}</span></td>
        <td>{{ p.shares }}</td><td>${{ "%.4f"|format(p.entry) }}</td>
        <td>${{ "%.2f"|format(p.cost) }}</td><td>{{ p.time }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<div class="gray" style="padding:10px 0">No open positions</div>{% endif %}
  </div>

  <div class="card full">
    <div class="lbl">Recent Settled Trades</div>
    {% if closed %}
    <table>
      <tr><th>Trade</th><th>Dir</th><th>Shares</th><th>Entry</th><th>Cost</th><th>Payout</th><th>P&L</th></tr>
      {% for t in closed[-10:]|reverse %}
      <tr>
        <td class="{{ 't1' if t.trade_num==1 else 't2' }}">#{{ t.trade_num }}</td>
        <td><span class="b {{ 'bup' if t.direction=='UP' else 'bdn' }}">{{ t.direction }}</span></td>
        <td>{{ t.shares }}</td><td>${{ "%.4f"|format(t.entry) }}</td>
        <td>${{ "%.2f"|format(t.cost) }}</td><td>${{ "%.2f"|format(t.payout) }}</td>
        <td style="font-weight:700;color:{{ '#22c55e' if t.pnl>=0 else '#ef4444' }}">{{ "%+.2f"|format(t.pnl) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<div class="gray" style="padding:10px 0">No settled trades yet</div>{% endif %}
  </div>

  <div class="card full">
    <div class="lbl">Live Activity Log</div>
    <div class="log">{% for line in log_lines[-40:]|reverse %}<div>{{ line }}</div>{% endfor %}</div>
  </div>

</div>
</body>
</html>
"""

@app.route("/")
def dashboard():
    with lock:
        s = dict(state)
    return render_template_string(
        DASHBOARD,
        balance    = s["balance"],
        pnl        = s["total_pnl"],
        wins       = s["wins"],
        losses     = s["losses"],
        slug       = s["window_slug"],
        status     = s["status"],
        up_price   = s["up_price"],
        down_price = s["down_price"],
        elapsed    = s["time_in_window"],
        close_in   = s["window_close_in"],
        prev_result= s["prev_window_result"],
        direction  = s["direction"],
        positions  = s["positions"],
        closed     = s["closed_trades"],
        log_lines  = s["log_lines"],
        ticks      = s["tick_count"],
        last_tick  = s["last_tick"],
        t1         = s["trade1_done"],
        t2         = s["trade2_done"],
        src        = s["price_source"],
    )

@app.route("/health")
def health():
    with lock:
        return {"status": "ok", "balance": state["balance"],
                "pnl": state["total_pnl"], "ticks": state["tick_count"],
                "price_source": state["price_source"]}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=price_ticker_thread, daemon=True).start()
    threading.Thread(target=trade_logic_thread,  daemon=True).start()
    add_log(f"🌐 Dashboard → http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
