"""
Polymarket BTC 5-Minute Dual-Strike Bot — v2
=============================================
Fixes vs v1:
  - Dedicated price ticker thread (1s interval, CLOB orderbook)
  - Trade logic thread runs separately from price thread
  - Resolve thread never blocks price updates
  - Polymarket clock alignment: all timing from system UTC epoch, not uptime
  - Verbose tick logging: every price refresh logged to dashboard
"""

import os, time, json, threading, logging
from datetime import datetime, timezone
import requests
from flask import Flask, render_template_string

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DEMO_MODE        = True
STARTING_BALANCE = 1000.0
TRADE1_SHARES    = 15       # Early trade shares
TRADE2_SHARES    = 25       # Value trade shares
MAX_ENTRY_PRICE  = 0.92     # Skip if overpriced
MIN_ENTRY_PRICE  = 0.30     # Skip if no conviction
VALUE_THRESHOLD  = 0.82     # Trade 2: only enter if token still below this
TAKER_FEE        = 0.02     # 2% Polymarket taker fee
TRADE1_OFFSET    = 60       # Seconds into window → Trade 1
TRADE2_OFFSET    = 210      # Seconds into window → Trade 2
PRICE_INTERVAL   = 1        # Seconds between price refreshes (ticker thread)
TRADE_INTERVAL   = 1        # Seconds between trade-logic checks

GAMMA_API        = "https://gamma-api.polymarket.com"
CLOB_API         = "https://clob.polymarket.com"

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
    "market_id":          None,
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

# ─── API CALLS ────────────────────────────────────────────────────────────────
def fetch_gamma_event(window_ts):
    """Fetch event metadata from Gamma API (market IDs, token IDs)."""
    url = f"{GAMMA_API}/events?slug={window_slug(window_ts)}"
    try:
        r = requests.get(url, timeout=8)
        data = r.json()
        if not data:
            return None
        return data[0]
    except Exception as e:
        add_log(f"⚠️ Gamma API error: {e}")
        return None

def parse_gamma_event(event):
    """Extract token IDs and current prices from a Gamma event."""
    try:
        market    = event["markets"][0]
        token_ids = json.loads(market["clobTokenIds"])
        prices    = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
        return {
            "market_id":  market["id"],
            "up_token":   token_ids[0],
            "down_token":  token_ids[1],
            "up_price":   float(prices[0]),
            "down_price":  float(prices[1]),
        }
    except Exception as e:
        add_log(f"⚠️ Gamma parse error: {e}")
        return None

def fetch_clob_best_price(token_id):
    """
    Fetch real-time best ask price from CLOB orderbook.
    Best ask = cheapest price you can BUY at right now.
    Returns float price or None on error.
    """
    try:
        r = requests.get(f"{CLOB_API}/book?token_id={token_id}", timeout=5)
        book = r.json()
        asks = book.get("asks", [])
        if asks:
            # asks are sorted ascending, [0] = lowest ask = best price to buy
            return float(asks[0]["price"])
        return None
    except Exception as e:
        add_log(f"⚠️ CLOB error (token {str(token_id)[:8]}…): {e}")
        return None

def get_prev_result(prev_window_ts):
    """
    Check if previous window has resolved.
    outcomePrices[0] >= 0.95 → UP won
    outcomePrices[1] >= 0.95 → DOWN won
    Returns 'UP', 'DOWN', or None
    """
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

# ─── DEMO TRADE EXECUTION ─────────────────────────────────────────────────────
def execute_demo_trade(direction, shares, entry_price, trade_num, window_ts):
    cost = shares * entry_price * (1 + TAKER_FEE)
    with lock:
        if state["balance"] < cost:
            add_log(f"⚠️  Insufficient balance for T{trade_num}. Need ${cost:.2f}, have ${state['balance']:.2f}")
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
    """
    Runs every 1 second. Fetches live UP/DOWN prices from CLOB.
    Completely independent — never blocked by trade logic or resolves.
    """
    add_log("📡 Price ticker started (CLOB, 1s interval)")
    last_loaded_window = None

    while True:
        try:
            win_ts = current_window_ts()

            # Load token IDs when window changes (from Gamma)
            with lock:
                up_token   = state["up_token"]
                down_token = state["down_token"]
                current_w  = state["current_window"]

            if win_ts != current_w or up_token is None:
                # New window — load market structure from Gamma
                event = fetch_gamma_event(win_ts)
                if event:
                    m = parse_gamma_event(event)
                    if m:
                        with lock:
                            state["current_window"] = win_ts
                            state["window_slug"]    = window_slug(win_ts)
                            state["up_token"]       = m["up_token"]
                            state["down_token"]     = m["down_token"]
                            state["market_id"]      = m["market_id"]
                            # Seed prices from Gamma while CLOB warms up
                            state["up_price"]       = m["up_price"]
                            state["down_price"]     = m["down_price"]
                            state["status"]         = f"Market loaded | ID {m['market_id']}"
                        add_log(f"🕐 Window {window_slug(win_ts)} | Gamma seed UP=${m['up_price']:.4f} DOWN=${m['down_price']:.4f}")
                        up_token   = m["up_token"]
                        down_token = m["down_token"]
                        last_loaded_window = win_ts

            # Fetch live CLOB prices every tick
            if up_token and down_token:
                up_ask   = fetch_clob_best_price(up_token)
                down_ask = fetch_clob_best_price(down_token)

                now  = now_ts()
                elapsed   = now - win_ts
                remaining = 300 - elapsed

                with lock:
                    if up_ask is not None:
                        state["up_price"] = up_ask
                    if down_ask is not None:
                        state["down_price"] = down_ask
                    state["time_in_window"]  = elapsed
                    state["window_close_in"] = max(0, remaining)
                    state["tick_count"]      += 1
                    state["last_tick"]        = utc_time()

                # Log every tick so dashboard shows live activity
                up_show   = up_ask   if up_ask   is not None else state["up_price"]
                down_show = down_ask if down_ask is not None else state["down_price"]
                add_log(f"📈 Tick | UP ${up_show:.4f} | DOWN ${down_show:.4f} | T+{elapsed}s | -{remaining}s")

        except Exception as e:
            add_log(f"❌ Ticker error: {e}")

        time.sleep(PRICE_INTERVAL)

# ─── THREAD 2: TRADE LOGIC ───────────────────────────────────────────────────
def trade_logic_thread():
    """
    Checks every 1 second if it's time to fire trades.
    Reads prices from shared state (set by ticker thread).
    Never fetches prices itself.
    """
    add_log("🧠 Trade logic thread started")
    last_window    = None
    trade1_done    = False
    trade2_done    = False
    resolved_done  = False
    prev_result    = None

    while True:
        try:
            win_ts = current_window_ts()
            now    = now_ts()

            with lock:
                elapsed    = state["time_in_window"]
                up_price   = state["up_price"]
                down_price = state["down_price"]

            # ── New window reset ──────────────────────────────────────────────
            if win_ts != last_window:
                last_window   = win_ts
                trade1_done   = False
                trade2_done   = False
                resolved_done = False
                prev_result   = None
                with lock:
                    state["trade1_done"] = False
                    state["trade2_done"] = False
                    state["direction"]   = "—"
                    state["prev_window_result"] = "Fetching…"
                    state["status"]      = "New window — fetching prev result"

                # Fetch previous window result
                prev_win_ts = win_ts - 300
                add_log(f"🔍 Checking prev window {window_slug(prev_win_ts)}")
                result = get_prev_result(prev_win_ts)
                if result:
                    prev_result = result
                    with lock:
                        state["prev_window_result"] = result
                        state["direction"]          = result
                    add_log(f"📊 Prev window: {result} → momentum signal = {result}")
                else:
                    add_log("⚠️  Prev window not resolved yet, will retry…")
                    with lock:
                        state["prev_window_result"] = "Unresolved"

            # ── Retry prev result if not found yet ────────────────────────────
            if not prev_result and elapsed < 30:
                prev_win_ts = win_ts - 300
                result = get_prev_result(prev_win_ts)
                if result:
                    prev_result = result
                    with lock:
                        state["prev_window_result"] = result
                        state["direction"]          = result
                    add_log(f"📊 Late prev confirm: {result}")

            # ── TRADE 1: ~60s into window ─────────────────────────────────────
            if not trade1_done and elapsed >= TRADE1_OFFSET and prev_result:
                direction   = prev_result
                entry_price = up_price if direction == "UP" else down_price

                if MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:
                    add_log(f"⚡ T1 trigger @ T+{elapsed}s | {direction} @ ${entry_price:.4f}")
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

            # ── TRADE 2: ~210s into window (value entry) ──────────────────────
            if not trade2_done and elapsed >= TRADE2_OFFSET and prev_result:
                direction   = prev_result
                entry_price = up_price if direction == "UP" else down_price

                if entry_price <= VALUE_THRESHOLD and MIN_ENTRY_PRICE <= entry_price:
                    add_log(f"💎 T2 VALUE @ T+{elapsed}s | {direction} still @ ${entry_price:.4f} (<{VALUE_THRESHOLD}) — entering!")
                    ok = execute_demo_trade(direction, TRADE2_SHARES, entry_price, 2, win_ts)
                    if ok:
                        with lock:
                            state["status"]      = f"T2 placed → {direction}"
                            state["trade2_done"] = True
                elif entry_price > VALUE_THRESHOLD:
                    add_log(f"⏭️  T2 skipped | ${entry_price:.4f} > {VALUE_THRESHOLD} — market priced in")
                    with lock:
                        state["status"]      = "T2 skipped (already priced)"
                        state["trade2_done"] = True
                else:
                    add_log(f"🚫 T2 skipped | ${entry_price:.4f} below min filter")
                    with lock:
                        state["trade2_done"] = True
                trade2_done = True

            # ── RESOLVE: After window closes ──────────────────────────────────
            remaining = 300 - elapsed
            if not resolved_done and remaining <= 3:
                add_log(f"⏳ Window closing, waiting 8s for resolution…")
                time.sleep(8)
                winner = get_prev_result(win_ts)   # This window is now the prev
                if winner:
                    resolve_window(win_ts, winner)
                    resolved_done = True
                    with lock:
                        state["status"] = f"Settled → {winner}"
                    add_log(f"🏁 Window {window_slug(win_ts)} → {winner}")
                else:
                    add_log("⚠️  Resolution pending, retrying next tick…")

        except Exception as e:
            add_log(f"❌ Trade logic error: {e}")

        time.sleep(TRADE_INTERVAL)

# ─── FLASK DASHBOARD ──────────────────────────────────────────────────────────
app = Flask(__name__)

DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="2">
<title>BTC 5M Bot v2</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#e2e8f0;font-family:'Segoe UI',sans-serif;font-size:14px}
.hdr{background:#111827;padding:14px 16px;border-bottom:2px solid #f7931a;display:flex;justify-content:space-between;align-items:center}
.hdr h1{color:#f7931a;font-size:18px;font-weight:700}
.hdr .tick{font-size:11px;color:#64748b}
.badge{display:inline-block;background:#1e3a5f;color:#60a5fa;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;margin-left:6px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px}
.card{background:#1a1f2e;border-radius:10px;padding:14px;border:1px solid #2d3748}
.full{grid-column:1/-1}
.lbl{color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.val{font-size:22px;font-weight:700}
.green{color:#22c55e}.red{color:#ef4444}.gold{color:#f7931a}.blue{color:#60a5fa}.gray{color:#64748b}
.bar-wrap{background:#2d3748;border-radius:6px;height:10px;margin-top:8px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#22c55e,#f7931a);transition:width .5s}
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
</style>
</head>
<body>
<div class="hdr">
  <div>
    <h1>⚡ BTC 5-Min Bot <span class="badge">DEMO</span></h1>
    <div class="status">{{ slug }} &nbsp;·&nbsp; {{ status }}</div>
  </div>
  <div class="tick">🟢 Tick #{{ ticks }}<br>{{ last_tick }}</div>
</div>

<div class="grid">

  <div class="card">
    <div class="lbl">Balance</div>
    <div class="val gold">${{ "%.2f"|format(balance) }}</div>
    <div style="font-size:12px;margin-top:4px;color:{{ '#22c55e' if pnl>=0 else '#ef4444' }}">
      P&L {{ "%+.2f"|format(pnl) }}
    </div>
  </div>

  <div class="card">
    <div class="lbl">Win / Loss</div>
    <div class="val blue">{{ wins }}W &nbsp; {{ losses }}L</div>
    <div style="font-size:12px;margin-top:4px;color:#94a3b8">
      {% if wins+losses>0 %}{{ "%.0f"|format(wins/(wins+losses)*100) }}% rate{% else %}No trades yet{% endif %}
    </div>
  </div>

  <div class="card full">
    <div class="lbl">Window — T+{{ elapsed }}s elapsed &nbsp;|&nbsp; closes in {{ close_in }}s</div>
    <div class="bar-wrap"><div class="bar-fill" style="width:{{ [elapsed/300*100,100]|min }}%"></div></div>
    <div class="px">
      <div class="pbox pup">↑ UP &nbsp; ${{ "%.4f"|format(up_price) }}</div>
      <div class="pbox pdown">↓ DOWN ${{ "%.4f"|format(down_price) }}</div>
    </div>
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
        <td>{{ p.shares }}</td>
        <td>${{ "%.4f"|format(p.entry) }}</td>
        <td>${{ "%.2f"|format(p.cost) }}</td>
        <td>{{ p.time }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="gray" style="padding:10px 0">No open positions</div>
    {% endif %}
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
        <td>{{ t.shares }}</td>
        <td>${{ "%.4f"|format(t.entry) }}</td>
        <td>${{ "%.2f"|format(t.cost) }}</td>
        <td>${{ "%.2f"|format(t.payout) }}</td>
        <td style="font-weight:700;color:{{ '#22c55e' if t.pnl>=0 else '#ef4444' }}">{{ "%+.2f"|format(t.pnl) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="gray" style="padding:10px 0">No settled trades yet</div>
    {% endif %}
  </div>

  <div class="card full">
    <div class="lbl">Live Activity Log</div>
    <div class="log" id="log">
      {% for line in log_lines[-40:]|reverse %}
      <div>{{ line }}</div>
      {% endfor %}
    </div>
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
        balance     = s["balance"],
        pnl         = s["total_pnl"],
        wins        = s["wins"],
        losses      = s["losses"],
        slug        = s["window_slug"],
        status      = s["status"],
        up_price    = s["up_price"],
        down_price  = s["down_price"],
        elapsed     = s["time_in_window"],
        close_in    = s["window_close_in"],
        prev_result = s["prev_window_result"],
        direction   = s["direction"],
        positions   = s["positions"],
        closed      = s["closed_trades"],
        log_lines   = s["log_lines"],
        ticks       = s["tick_count"],
        last_tick   = s["last_tick"],
        t1          = s["trade1_done"],
        t2          = s["trade2_done"],
    )

@app.route("/health")
def health():
    with lock:
        return {
            "status":  "ok",
            "balance": state["balance"],
            "pnl":     state["total_pnl"],
            "ticks":   state["tick_count"],
            "window":  state["window_slug"],
        }

# ─── ENTRY ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    # Thread 1: Price ticker (every 1s, CLOB)
    t1 = threading.Thread(target=price_ticker_thread, daemon=True)
    t1.start()

    # Thread 2: Trade logic (every 1s, reads from state)
    t2 = threading.Thread(target=trade_logic_thread, daemon=True)
    t2.start()

    add_log(f"🌐 Dashboard → http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
