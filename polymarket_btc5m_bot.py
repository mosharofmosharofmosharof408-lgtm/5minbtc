"""
Polymarket BTC 5-Minute Dual-Strike Momentum Bot
=================================================
Strategy:
  Trade 1 (Early, ~60s into window): Momentum follow from previous window result
  Trade 2 (Late, ~210s into window): Value re-entry if token still underpriced
  Resolution: Track P&L at window close when outcomePrices converge to [1,0] or [0,1]

Demo mode: $1,000 virtual balance, real API for prices only.
"""

import os, time, json, threading, logging
from datetime import datetime, timezone
import requests
from flask import Flask, render_template_string

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DEMO_MODE        = True          # False = live trading
STARTING_BALANCE = 1000.0        # Demo balance in USDC
TRADE1_SHARES    = 15            # Shares for early trade
TRADE2_SHARES    = 25            # Shares for late value trade
MAX_ENTRY_PRICE  = 0.92          # Skip if token > this (overpriced)
MIN_ENTRY_PRICE  = 0.30          # Skip if token < this (no conviction)
TAKER_FEE        = 0.02          # 2% Polymarket taker fee
TRADE1_OFFSET    = 60            # Seconds after window open → Trade 1
TRADE2_OFFSET    = 210           # Seconds after window open → Trade 2
CHECK_INTERVAL   = 2             # Price refresh seconds

GAMMA_API        = "https://gamma-api.polymarket.com"
CLOB_API         = "https://clob.polymarket.com"

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("BTC5M")

# ─── SHARED STATE ─────────────────────────────────────────────────────────────
state = {
    "balance":        STARTING_BALANCE,
    "total_pnl":      0.0,
    "wins":           0,
    "losses":         0,
    "current_window": None,     # window_ts int
    "window_slug":    "",
    "direction":      "—",      # UP or DOWN (current bet)
    "up_price":       0.0,
    "down_price":     0.0,
    "positions":      [],       # [{window, direction, shares, entry, trade_num}]
    "closed_trades":  [],       # resolved trades with P&L
    "log_lines":      [],       # last 40 log lines for dashboard
    "status":         "Booting…",
    "prev_window_result": "—",
    "time_in_window": 0,
    "window_close_in": 300,
}
lock = threading.Lock()

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def now_ts():
    return int(time.time())

def current_window_ts():
    t = now_ts()
    return t - (t % 300)

def slug(window_ts):
    return f"btc-updown-5m-{window_ts}"

def add_log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    log.info(msg)
    with lock:
        state["log_lines"].append(line)
        if len(state["log_lines"]) > 50:
            state["log_lines"].pop(0)

def fetch_event(window_ts):
    """Fetch Gamma API event for a given window timestamp."""
    url = f"{GAMMA_API}/events?slug={slug(window_ts)}"
    try:
        r = requests.get(url, timeout=8)
        data = r.json()
        if not data:
            return None
        return data[0]
    except Exception as e:
        add_log(f"Gamma fetch error: {e}", "ERROR")
        return None

def parse_market(event):
    """Extract token IDs and prices from an event."""
    try:
        market = event["markets"][0]
        token_ids = json.loads(market["clobTokenIds"])   # [UP_id, DOWN_id]
        prices    = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
        return {
            "market_id":    market["id"],
            "up_token":     token_ids[0],
            "down_token":   token_ids[1],
            "up_price":     float(prices[0]),
            "down_price":   float(prices[1]),
        }
    except Exception as e:
        add_log(f"Parse error: {e}", "ERROR")
        return None

def fetch_clob_prices(token_id):
    """Get best bid/ask from CLOB orderbook."""
    try:
        r = requests.get(f"{CLOB_API}/book?token_id={token_id}", timeout=6)
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        return best_bid, best_ask
    except:
        return None, None

def get_previous_result(prev_window_ts):
    """
    Determine which direction won in the previous window.
    outcomePrices[0] → UP price; if >=0.95 → UP won, else DOWN won.
    Returns: 'UP', 'DOWN', or None
    """
    event = fetch_event(prev_window_ts)
    if not event:
        return None
    market = parse_market(event)
    if not market:
        return None
    up_p  = market["up_price"]
    down_p = market["down_price"]
    if up_p >= 0.95:
        return "UP"
    elif down_p >= 0.95:
        return "DOWN"
    return None  # Not resolved yet

def execute_demo_trade(direction, shares, entry_price, trade_num, window_ts):
    """Simulate a trade in demo mode."""
    cost = shares * entry_price * (1 + TAKER_FEE)
    with lock:
        if state["balance"] < cost:
            add_log(f"⚠️  Insufficient demo balance for Trade {trade_num}. Need ${cost:.2f}")
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
        })
    add_log(f"🟢 DEMO Trade {trade_num} | {direction} | {shares} shares @ ${entry_price:.3f} | Cost: ${cost:.2f}")
    return True

def resolve_positions(window_ts, winning_direction):
    """Settle all positions for a closed window."""
    with lock:
        remaining = []
        for pos in state["positions"]:
            if pos["window"] != window_ts:
                remaining.append(pos)
                continue
            if pos["resolved"]:
                continue
            won = (pos["direction"] == winning_direction)
            payout = pos["shares"] * 1.0 if won else 0.0
            pnl    = payout - pos["cost"]
            pos["resolved"] = True
            state["total_pnl"] += pnl
            state["balance"]   += payout
            if won:
                state["wins"] += 1
            else:
                state["losses"] += 1
            state["closed_trades"].append({
                **pos,
                "won":    won,
                "payout": payout,
                "pnl":    pnl,
            })
            emoji = "✅" if won else "❌"
            add_log(f"{emoji} Settled Trade {pos['trade_num']} | {pos['direction']} | PnL: ${pnl:+.2f}")
        state["positions"] = remaining

# ─── MAIN BOT LOOP ────────────────────────────────────────────────────────────
def bot_loop():
    add_log("🚀 BTC 5-Min Dual-Strike Bot started (DEMO MODE)" if DEMO_MODE else "🚀 BTC 5-Min Bot started — LIVE MODE")
    
    last_window    = None
    trade1_done    = False
    trade2_done    = False
    resolved_this  = False
    current_market = None

    while True:
        try:
            now          = now_ts()
            win_ts       = current_window_ts()
            prev_win_ts  = win_ts - 300
            elapsed      = now - win_ts
            remaining    = 300 - elapsed

            # ── New window detection ──────────────────────────────────────────
            if win_ts != last_window:
                add_log(f"🕐 New window: {slug(win_ts)} | Prev: {slug(prev_win_ts)}")
                last_window   = win_ts
                trade1_done   = False
                trade2_done   = False
                resolved_this = False
                current_market = None

                # Check previous window result for momentum signal
                prev_result = get_previous_result(prev_win_ts)
                with lock:
                    state["prev_window_result"] = prev_result or "Pending…"
                    state["current_window"]     = win_ts
                    state["window_slug"]        = slug(win_ts)

                if prev_result:
                    add_log(f"📊 Previous window result: {prev_result} → Momentum signal set")
                else:
                    add_log("⚠️  Previous window not resolved yet — will retry")

            # ── Fetch current market if not loaded ────────────────────────────
            if current_market is None:
                event = fetch_event(win_ts)
                if event:
                    current_market = parse_market(event)
                    if current_market:
                        add_log(f"📋 Market loaded | UP: ${current_market['up_price']:.3f} | DOWN: ${current_market['down_price']:.3f}")

            # ── Live price update ─────────────────────────────────────────────
            if current_market:
                event = fetch_event(win_ts)
                if event:
                    m = parse_market(event)
                    if m:
                        current_market = m
                        with lock:
                            state["up_price"]   = m["up_price"]
                            state["down_price"] = m["down_price"]

            # ── Recalculate prev_result if still pending ──────────────────────
            prev_result = None
            with lock:
                pr = state["prev_window_result"]
                if pr in ("UP", "DOWN"):
                    prev_result = pr
            if not prev_result:
                prev_result = get_previous_result(prev_win_ts)
                if prev_result:
                    with lock:
                        state["prev_window_result"] = prev_result
                    add_log(f"📊 Late prev result confirmed: {prev_result}")

            # ── TRADE 1: Early momentum entry (~60s into window) ──────────────
            if not trade1_done and elapsed >= TRADE1_OFFSET and prev_result and current_market:
                direction = prev_result  # Momentum follow
                entry_price = current_market["up_price"] if direction == "UP" else current_market["down_price"]
                
                if MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:
                    add_log(f"⚡ Trade 1 trigger | Direction: {direction} | Price: ${entry_price:.3f}")
                    execute_demo_trade(direction, TRADE1_SHARES, entry_price, 1, win_ts)
                    trade1_done = True
                    with lock:
                        state["direction"] = direction
                        state["status"]    = f"Trade 1 placed → {direction}"
                else:
                    add_log(f"🚫 Trade 1 skipped | Price ${entry_price:.3f} out of filter [{MIN_ENTRY_PRICE}–{MAX_ENTRY_PRICE}]")
                    trade1_done = True  # Don't retry this slot

            # ── TRADE 2: Late value entry (~210s into window) ─────────────────
            if not trade2_done and elapsed >= TRADE2_OFFSET and prev_result and current_market:
                direction   = prev_result
                entry_price = current_market["up_price"] if direction == "UP" else current_market["down_price"]
                
                # Value filter: token still below 0.82 with ~90s left = underpriced
                VALUE_THRESHOLD = 0.82
                if entry_price <= VALUE_THRESHOLD and MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:
                    add_log(f"💎 Trade 2 (Value) | {direction} still @ ${entry_price:.3f} (<{VALUE_THRESHOLD}) — entering!")
                    execute_demo_trade(direction, TRADE2_SHARES, entry_price, 2, win_ts)
                    trade2_done = True
                    with lock:
                        state["status"] = f"Trade 2 placed → {direction}"
                elif entry_price > VALUE_THRESHOLD:
                    add_log(f"⏭️  Trade 2 skipped | Token priced at ${entry_price:.3f} — market already pricing in outcome")
                    trade2_done = True
                else:
                    add_log(f"🚫 Trade 2 skipped | Price ${entry_price:.3f} out of filter")
                    trade2_done = True

            # ── RESOLVE: After window closes (prices converge) ────────────────
            if not resolved_this and remaining <= 5:
                # Give 5s buffer then check
                time.sleep(6)
                winner = get_previous_result(win_ts)  # This window is now prev
                if winner:
                    resolve_positions(win_ts, winner)
                    resolved_this = True
                    add_log(f"🏁 Window closed | Winner: {winner} | Balance: ${state['balance']:.2f}")
                    with lock:
                        state["status"] = f"Window settled → {winner}"

            # ── State update ──────────────────────────────────────────────────
            with lock:
                state["time_in_window"]  = elapsed
                state["window_close_in"] = max(0, remaining)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            add_log(f"❌ Bot loop error: {e}", "ERROR")
            time.sleep(5)

# ─── FLASK DASHBOARD ──────────────────────────────────────────────────────────
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="3">
<title>BTC 5M Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0f14; color: #e2e8f0; font-family: 'Segoe UI', sans-serif; font-size: 15px; }
  .header { background: linear-gradient(135deg, #1a1f2e, #0f1829); padding: 16px 20px; border-bottom: 2px solid #f7931a; }
  .header h1 { color: #f7931a; font-size: 20px; font-weight: 700; }
  .header .sub { color: #64748b; font-size: 12px; margin-top: 2px; }
  .mode-badge { display: inline-block; background: #1e3a5f; color: #60a5fa; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; margin-left: 10px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 16px; }
  .card { background: #1a1f2e; border-radius: 10px; padding: 16px; border: 1px solid #2d3748; }
  .card-full { grid-column: 1 / -1; }
  .label { color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .value { font-size: 22px; font-weight: 700; }
  .value.green { color: #22c55e; }
  .value.red   { color: #ef4444; }
  .value.gold  { color: #f7931a; }
  .value.blue  { color: #60a5fa; }
  .progress-bar { background: #2d3748; border-radius: 6px; height: 10px; margin-top: 8px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 6px; background: linear-gradient(90deg, #22c55e, #f7931a); transition: width 1s; }
  .prices { display: flex; gap: 10px; margin-top: 8px; }
  .price-box { flex: 1; text-align: center; border-radius: 8px; padding: 10px 6px; font-weight: 700; font-size: 16px; }
  .price-up   { background: #14532d; color: #22c55e; border: 1px solid #22c55e; }
  .price-down { background: #450a0a; color: #ef4444; border: 1px solid #ef4444; }
  .log-box { background: #0d1117; border-radius: 8px; padding: 12px; max-height: 280px; overflow-y: auto; font-family: monospace; font-size: 12px; line-height: 1.6; color: #94a3b8; }
  .log-box div:last-child { color: #e2e8f0; }
  .positions-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .positions-table th { color: #64748b; text-align: left; padding: 6px 8px; border-bottom: 1px solid #2d3748; font-size: 11px; text-transform: uppercase; }
  .positions-table td { padding: 8px 8px; border-bottom: 1px solid #1e2535; }
  .badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 700; }
  .badge-up   { background: #14532d; color: #22c55e; }
  .badge-down { background: #450a0a; color: #ef4444; }
  .stats-row { display: flex; gap: 8px; margin-top: 8px; }
  .stat-chip { flex: 1; background: #0d1117; border-radius: 8px; padding: 10px 8px; text-align: center; }
  .stat-chip .sv { font-size: 18px; font-weight: 700; }
  .stat-chip .sl { font-size: 10px; color: #64748b; margin-top: 2px; }
</style>
</head>
<body>

<div class="header">
  <h1>⚡ BTC 5-Minute Bot <span class="mode-badge">DEMO</span></h1>
  <div class="sub">{{ slug }} &nbsp;|&nbsp; {{ status }}</div>
</div>

<div class="grid">

  <!-- Balance -->
  <div class="card">
    <div class="label">Balance</div>
    <div class="value gold">${{ "%.2f"|format(balance) }}</div>
    <div style="color: {{ 'green' if pnl >= 0 else 'red' }}; font-size:13px; margin-top:4px;">
      Total P&L: {{ "%+.2f"|format(pnl) }}
    </div>
  </div>

  <!-- Win Rate -->
  <div class="card">
    <div class="label">Win / Loss</div>
    <div class="value blue">{{ wins }}W / {{ losses }}L</div>
    <div style="color:#94a3b8; font-size:12px; margin-top:4px;">
      {% if wins+losses > 0 %}{{ "%.0f"|format(wins/(wins+losses)*100) }}% win rate{% else %}No trades yet{% endif %}
    </div>
  </div>

  <!-- Window Timer -->
  <div class="card card-full">
    <div class="label">Window Timer — closes in {{ close_in }}s (elapsed {{ elapsed }}s)</div>
    <div class="progress-bar">
      <div class="progress-fill" style="width: {{ elapsed/300*100 }}%"></div>
    </div>
    <div class="prices">
      <div class="price-box price-up">↑ UP &nbsp; ${{ "%.3f"|format(up_price) }}</div>
      <div class="price-box price-down">↓ DOWN ${{ "%.3f"|format(down_price) }}</div>
    </div>
    <div style="color:#64748b; font-size:12px; margin-top:8px;">
      Prev window: <strong style="color:#e2e8f0">{{ prev_result }}</strong> &nbsp;|&nbsp; 
      Current signal: <strong style="color:#f7931a">{{ direction }}</strong>
    </div>
  </div>

  <!-- Open Positions -->
  <div class="card card-full">
    <div class="label">Open Positions</div>
    {% if positions %}
    <table class="positions-table">
      <tr><th>Trade</th><th>Direction</th><th>Shares</th><th>Entry</th><th>Cost</th></tr>
      {% for p in positions %}
      <tr>
        <td>#{{ p.trade_num }}</td>
        <td><span class="badge {{ 'badge-up' if p.direction == 'UP' else 'badge-down' }}">{{ p.direction }}</span></td>
        <td>{{ p.shares }}</td>
        <td>${{ "%.3f"|format(p.entry) }}</td>
        <td>${{ "%.2f"|format(p.cost) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div style="color:#4a5568; padding: 12px 0; font-size:13px;">No open positions</div>
    {% endif %}
  </div>

  <!-- Recent Trades -->
  <div class="card card-full">
    <div class="label">Recent Settled Trades</div>
    {% if closed %}
    <table class="positions-table">
      <tr><th>Trade</th><th>Dir</th><th>Shares</th><th>Entry</th><th>Cost</th><th>Payout</th><th>P&L</th></tr>
      {% for t in closed[-10:]|reverse %}
      <tr>
        <td>#{{ t.trade_num }}</td>
        <td><span class="badge {{ 'badge-up' if t.direction == 'UP' else 'badge-down' }}">{{ t.direction }}</span></td>
        <td>{{ t.shares }}</td>
        <td>${{ "%.3f"|format(t.entry) }}</td>
        <td>${{ "%.2f"|format(t.cost) }}</td>
        <td>${{ "%.2f"|format(t.payout) }}</td>
        <td style="color: {{ 'green' if t.pnl >= 0 else '#ef4444' }}; font-weight:700;">{{ "%+.2f"|format(t.pnl) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div style="color:#4a5568; padding: 12px 0; font-size:13px;">No settled trades yet</div>
    {% endif %}
  </div>

  <!-- Bot Log -->
  <div class="card card-full">
    <div class="label">Bot Activity Log</div>
    <div class="log-box" id="log">
      {% for line in log_lines[-30:]|reverse %}
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
        DASHBOARD_HTML,
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
    )

@app.route("/health")
def health():
    return {"status": "ok", "balance": state["balance"], "pnl": state["total_pnl"]}

# ─── ENTRY ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    
    # Start bot in background thread
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    
    add_log(f"🌐 Dashboard at http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
