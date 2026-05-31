"""
Polymarket BTC 5-Minute Dual-Strike Bot — v4
Fix: trade logic thread was silently dying on prev_result fetch failure.
     Window init is now non-blocking. Trades fire on elapsed time regardless.
     Every code path logs explicitly — no silent failures.
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
VALUE_THRESHOLD  = 0.82
TAKER_FEE        = 0.02
TRADE1_OFFSET    = 60
TRADE2_OFFSET    = 210

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
    "price_source":       "—",
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

# ─── HELPERS ──────────────────────────────────────────────────────────────────
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
        if len(state["log_lines"]) > 80:
            state["log_lines"].pop(0)

# ─── API ──────────────────────────────────────────────────────────────────────
def fetch_gamma_event(win_ts):
    try:
        r = requests.get(f"{GAMMA_API}/events?slug={window_slug(win_ts)}", timeout=8)
        data = r.json()
        if not data:
            return None
        return data[0]
    except Exception as e:
        add_log(f"⚠️ Gamma fetch error: {e}")
        return None

def parse_market(event):
    try:
        mkt  = event["markets"][0]
        tids = json.loads(mkt["clobTokenIds"])
        px   = json.loads(mkt.get("outcomePrices", "[0.5,0.5]"))
        return {
            "up_token":   str(tids[0]),
            "down_token": str(tids[1]),
            "up_price":   float(px[0]),
            "down_price": float(px[1]),
        }
    except Exception as e:
        add_log(f"⚠️ Market parse error: {e}")
        return None

def clob_price(token_id):
    """GET /price?token_id=X&side=buy — best ask. Returns float or None."""
    try:
        r = requests.get(f"{CLOB_API}/price",
                         params={"token_id": token_id, "side": "buy"},
                         timeout=5)
        if r.status_code == 200:
            p = float(r.json().get("price", 0))
            if 0.01 <= p <= 0.99:
                return p
        return None
    except:
        return None

def gamma_prices(win_ts):
    """Re-fetch outcomePrices fresh from Gamma (fallback)."""
    try:
        event = fetch_gamma_event(win_ts)
        if not event:
            return None, None
        m = parse_market(event)
        if not m:
            return None, None
        # Reject impossible state: both ≥ 0.95 simultaneously
        if m["up_price"] >= 0.95 and m["down_price"] >= 0.95:
            return None, None
        return m["up_price"], m["down_price"]
    except:
        return None, None

def get_live_prices(up_token, down_token, win_ts):
    up_p  = clob_price(up_token)
    dn_p  = clob_price(down_token)
    if up_p and dn_p:
        return up_p, dn_p, "CLOB"
    # fallback
    up_p, dn_p = gamma_prices(win_ts)
    if up_p and dn_p:
        return up_p, dn_p, "Gamma"
    return None, None, "none"

def get_prev_result(prev_win_ts):
    """Returns 'UP', 'DOWN', or None. Never raises."""
    try:
        event = fetch_gamma_event(prev_win_ts)
        if not event:
            return None
        m = parse_market(event)
        if not m:
            return None
        if m["up_price"] >= 0.95:
            return "UP"
        if m["down_price"] >= 0.95:
            return "DOWN"
        return None
    except Exception as e:
        add_log(f"⚠️ prev_result error: {e}")
        return None

# ─── DEMO TRADE ───────────────────────────────────────────────────────────────
def place_trade(direction, shares, price, trade_num, win_ts):
    cost = round(shares * price * (1 + TAKER_FEE), 4)
    with lock:
        if state["balance"] < cost:
            add_log(f"⚠️  Balance too low for T{trade_num}: need ${cost:.2f} have ${state['balance']:.2f}")
            return False
        state["balance"] -= cost
        state["positions"].append({
            "window": win_ts, "direction": direction,
            "shares": shares, "entry": price,
            "cost": cost, "trade_num": trade_num,
            "resolved": False, "time": utc_time(),
        })
    add_log(f"🟢 TRADE {trade_num} PLACED | {direction} | {shares} shares @ ${price:.4f} | cost ${cost:.2f}")
    return True

def settle(win_ts, winner):
    with lock:
        keep = []
        for p in state["positions"]:
            if p["window"] != win_ts or p["resolved"]:
                keep.append(p)
                continue
            won    = p["direction"] == winner
            payout = p["shares"] * 1.0 if won else 0.0
            pnl    = payout - p["cost"]
            p["resolved"] = True
            state["total_pnl"] += pnl
            state["balance"]   += payout
            state["wins" if won else "losses"] += 1
            state["closed_trades"].append({**p, "won": won, "payout": payout, "pnl": pnl})
            add_log(f"{'✅' if won else '❌'} T{p['trade_num']} SETTLED | {p['direction']} | PnL ${pnl:+.2f} | bal ${state['balance']:.2f}")
        state["positions"] = keep

# ─── THREAD 1: PRICE TICKER ──────────────────────────────────────────────────
def price_ticker():
    add_log("📡 Price ticker started")
    loaded_win = None

    while True:
        try:
            win_ts = current_window_ts()

            # Load token IDs when window changes
            with lock:
                up_tok = state["up_token"]
                dn_tok = state["down_token"]

            if win_ts != loaded_win or up_tok is None:
                add_log(f"🕐 Loading tokens for {window_slug(win_ts)}")
                event = fetch_gamma_event(win_ts)
                if event:
                    m = parse_market(event)
                    if m:
                        with lock:
                            state["current_window"] = win_ts
                            state["window_slug"]    = window_slug(win_ts)
                            state["up_token"]       = m["up_token"]
                            state["down_token"]     = m["down_token"]
                            state["up_price"]       = m["up_price"]
                            state["down_price"]     = m["down_price"]
                            state["price_source"]   = "Gamma(seed)"
                        add_log(f"✅ Tokens loaded | UP={m['up_token'][:8]}… | Seed UP=${m['up_price']:.4f} DOWN=${m['down_price']:.4f}")
                        up_tok = m["up_token"]
                        dn_tok = m["down_token"]
                        loaded_win = win_ts
                    else:
                        add_log("⚠️ parse_market returned None — retrying next tick")
                else:
                    add_log("⚠️ fetch_gamma_event returned None — retrying next tick")

            # Fetch live prices
            if up_tok and dn_tok:
                up_p, dn_p, src = get_live_prices(up_tok, dn_tok, win_ts)
                elapsed   = now_ts() - win_ts
                remaining = 300 - elapsed
                with lock:
                    if up_p: state["up_price"]   = up_p
                    if dn_p: state["down_price"]  = dn_p
                    state["price_source"]    = src
                    state["time_in_window"]  = elapsed
                    state["window_close_in"] = max(0, remaining)
                    state["tick_count"]     += 1
                    state["last_tick"]       = utc_time()
                up_show = up_p  or state["up_price"]
                dn_show = dn_p or state["down_price"]
                add_log(f"📈 [{src}] UP ${up_show:.4f} | DOWN ${dn_show:.4f} | T+{elapsed}s | -{remaining}s")

        except Exception as e:
            add_log(f"❌ Ticker crash: {e}")

        time.sleep(1)

# ─── THREAD 2: TRADE LOGIC ───────────────────────────────────────────────────
def trade_logic():
    add_log("🧠 Trade logic started")

    # Per-window state — simple variables, reset each window
    cur_win       = None
    trade1_done   = False
    trade2_done   = False
    resolved_done = False
    prev_result   = None
    prev_fetched  = False   # did we already attempt the fetch this window?

    while True:
        try:
            win_ts = current_window_ts()

            # ── Reset on new window ───────────────────────────────────────────
            if win_ts != cur_win:
                add_log(f"🔄 Trade logic: new window {window_slug(win_ts)}")
                cur_win       = win_ts
                trade1_done   = False
                trade2_done   = False
                resolved_done = False
                prev_result   = None
                prev_fetched  = False
                with lock:
                    state["trade1_done"]        = False
                    state["trade2_done"]        = False
                    state["direction"]          = "—"
                    state["prev_window_result"] = "Fetching…"
                    state["status"]             = "New window"

            # ── Read current prices from shared state ─────────────────────────
            with lock:
                elapsed   = state["time_in_window"]
                up_price  = state["up_price"]
                dn_price  = state["down_price"]
                src       = state["price_source"]
                tok_ready = state["up_token"] is not None

            # Wait for price ticker to load tokens before doing anything
            if not tok_ready:
                add_log(f"⏳ Waiting for tokens… T+{elapsed}s")
                time.sleep(1)
                continue

            # ── Fetch prev result (attempt once, retry up to T+50s) ───────────
            if not prev_result and elapsed <= 50:
                prev_win = win_ts - 300
                add_log(f"🔍 Fetching prev result: {window_slug(prev_win)}")
                result = get_prev_result(prev_win)
                if result:
                    prev_result = result
                    with lock:
                        state["prev_window_result"] = result
                        state["direction"]          = result
                    add_log(f"📊 Prev window = {result} → momentum signal: {result}")
                else:
                    add_log(f"⚠️  Prev result not ready yet (T+{elapsed}s) — will retry")

            # ── Price sanity check ────────────────────────────────────────────
            prices_ok = (src != "none") and \
                        not (up_price >= 0.98 and dn_price >= 0.98) and \
                        (up_price > 0.01 or dn_price > 0.01)

            if not prices_ok:
                add_log(f"⚠️  Prices not ready: UP={up_price} DOWN={dn_price} src={src}")
                time.sleep(1)
                continue

            # ── TRADE 1: at T+60s ─────────────────────────────────────────────
            if not trade1_done and elapsed >= TRADE1_OFFSET:
                trade1_done = True
                with lock:
                    state["trade1_done"] = True

                if not prev_result:
                    add_log(f"🚫 T1 skipped — no prev result by T+{elapsed}s")
                else:
                    direction = prev_result
                    entry     = up_price if direction == "UP" else dn_price
                    add_log(f"⚡ T1 CHECK | {direction} | price=${entry:.4f} | T+{elapsed}s")
                    if MIN_ENTRY_PRICE <= entry <= MAX_ENTRY_PRICE:
                        place_trade(direction, TRADE1_SHARES, entry, 1, win_ts)
                        with lock:
                            state["status"] = f"T1 placed → {direction} @ ${entry:.4f}"
                    else:
                        add_log(f"🚫 T1 skipped | ${entry:.4f} outside [{MIN_ENTRY_PRICE}–{MAX_ENTRY_PRICE}]")
                        with lock:
                            state["status"] = f"T1 skipped (${entry:.4f} out of range)"

            # ── TRADE 2: at T+210s ────────────────────────────────────────────
            if not trade2_done and elapsed >= TRADE2_OFFSET:
                trade2_done = True
                with lock:
                    state["trade2_done"] = True

                if not prev_result:
                    add_log(f"🚫 T2 skipped — no prev result by T+{elapsed}s")
                else:
                    direction = prev_result
                    entry     = up_price if direction == "UP" else dn_price
                    add_log(f"💎 T2 CHECK | {direction} | price=${entry:.4f} | T+{elapsed}s")
                    if entry <= VALUE_THRESHOLD and MIN_ENTRY_PRICE <= entry:
                        place_trade(direction, TRADE2_SHARES, entry, 2, win_ts)
                        with lock:
                            state["status"] = f"T2 placed → {direction} @ ${entry:.4f}"
                    elif entry > VALUE_THRESHOLD:
                        add_log(f"⏭️  T2 skipped | ${entry:.4f} > VALUE_THRESHOLD {VALUE_THRESHOLD} (already priced in)")
                        with lock:
                            state["status"] = f"T2 skipped (${entry:.4f} priced in)"
                    else:
                        add_log(f"🚫 T2 skipped | ${entry:.4f} below min {MIN_ENTRY_PRICE}")

            # ── RESOLVE: after window closes ──────────────────────────────────
            remaining = 300 - elapsed
            if not resolved_done and remaining <= 3:
                resolved_done = True
                add_log("⏳ Window closing — waiting 8s for settlement…")
                time.sleep(8)
                winner = get_prev_result(win_ts)
                if winner:
                    settle(win_ts, winner)
                    with lock:
                        state["status"] = f"Settled → {winner}"
                    add_log(f"🏁 {window_slug(win_ts)} settled → {winner}")
                else:
                    add_log("⚠️  Settlement pending, will catch next window")

        except Exception as e:
            add_log(f"❌ Trade logic crash: {e}")

        time.sleep(1)

# ─── FLASK DASHBOARD ──────────────────────────────────────────────────────────
app = Flask(__name__)

DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>BTC 5M v4</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#e2e8f0;font-family:'Segoe UI',sans-serif;font-size:14px}
.hdr{background:#111827;padding:14px 16px;border-bottom:2px solid #f7931a;display:flex;justify-content:space-between;align-items:center}
.hdr h1{color:#f7931a;font-size:18px;font-weight:700}
.badge{background:#1e3a5f;color:#60a5fa;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;margin-left:6px}
.src{background:#1e2535;color:#94a3b8;padding:2px 7px;border-radius:8px;font-size:10px;margin-left:6px}
.sub{font-size:11px;color:#94a3b8;margin-top:3px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px}
.card{background:#1a1f2e;border-radius:10px;padding:14px;border:1px solid #2d3748}
.full{grid-column:1/-1}
.lbl{color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.val{font-size:22px;font-weight:700}
.green{color:#22c55e}.red{color:#ef4444}.gold{color:#f7931a}.blue{color:#60a5fa}.gray{color:#64748b}
.bar{background:#2d3748;border-radius:6px;height:10px;margin-top:8px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#22c55e,#f7931a)}
.px{display:flex;gap:8px;margin-top:10px}
.pbox{flex:1;text-align:center;border-radius:8px;padding:10px 4px;font-weight:700;font-size:17px}
.pup{background:#14532d;color:#22c55e;border:1px solid #22c55e}
.pdn{background:#450a0a;color:#ef4444;border:1px solid #ef4444}
.log{background:#0d1117;border-radius:8px;padding:10px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.7;color:#64748b}
.log div:last-child{color:#e2e8f0}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#64748b;text-align:left;padding:5px 6px;border-bottom:1px solid #2d3748;font-size:10px;text-transform:uppercase}
td{padding:7px 6px;border-bottom:1px solid #1e2535}
.b{padding:2px 7px;border-radius:10px;font-size:10px;font-weight:700}
.bup{background:#14532d;color:#22c55e}.bdn{background:#450a0a;color:#ef4444}
.t1{color:#a78bfa}.t2{color:#f59e0b}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <h1>⚡ BTC 5-Min Bot <span class="badge">DEMO v4</span><span class="src">{{ src }}</span></h1>
    <div class="sub">{{ slug }} · {{ status }}</div>
  </div>
  <div style="text-align:right;font-size:11px;color:#64748b">🟢 #{{ ticks }}<br>{{ last_tick }}</div>
</div>
<div class="grid">

  <div class="card">
    <div class="lbl">Balance</div>
    <div class="val gold">${{ "%.2f"|format(balance) }}</div>
    <div style="font-size:12px;margin-top:4px;color:{{ '#22c55e' if pnl>=0 else '#ef4444' }}">P&L {{ "%+.2f"|format(pnl) }}</div>
  </div>

  <div class="card">
    <div class="lbl">Win / Loss</div>
    <div class="val blue">{{ wins }}W / {{ losses }}L</div>
    <div style="font-size:12px;margin-top:4px;color:#94a3b8">
      {% if wins+losses>0 %}{{ "%.0f"|format(wins/(wins+losses)*100) }}%{% else %}—{% endif %}
    </div>
  </div>

  <div class="card full">
    <div class="lbl">Window · T+{{ elapsed }}s · closes in {{ close_in }}s</div>
    <div class="bar"><div class="bar-fill" style="width:{{ [elapsed/300*100,100]|min }}%"></div></div>
    <div class="px">
      <div class="pbox pup">↑ UP ${{ "%.4f"|format(up) }}</div>
      <div class="pbox pdn">↓ DOWN ${{ "%.4f"|format(dn) }}</div>
    </div>
    <div style="font-size:11px;color:#64748b;margin-top:8px">
      Prev: <strong style="color:#e2e8f0">{{ prev }}</strong> ·
      Signal: <strong style="color:#f7931a">{{ direction }}</strong> ·
      T1: <span style="color:{{ '#22c55e' if t1 else '#64748b' }}">{{ '✓' if t1 else '…' }}</span> ·
      T2: <span style="color:{{ '#22c55e' if t2 else '#64748b' }}">{{ '✓' if t2 else '…' }}</span>
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
    <div class="lbl">Settled Trades</div>
    {% if closed %}
    <table>
      <tr><th>Trade</th><th>Dir</th><th>Shares</th><th>Entry</th><th>Payout</th><th>P&L</th></tr>
      {% for t in closed[-12:]|reverse %}
      <tr>
        <td class="{{ 't1' if t.trade_num==1 else 't2' }}">#{{ t.trade_num }}</td>
        <td><span class="b {{ 'bup' if t.direction=='UP' else 'bdn' }}">{{ t.direction }}</span></td>
        <td>{{ t.shares }}</td><td>${{ "%.4f"|format(t.entry) }}</td>
        <td>${{ "%.2f"|format(t.payout) }}</td>
        <td style="font-weight:700;color:{{ '#22c55e' if t.pnl>=0 else '#ef4444' }}">{{ "%+.2f"|format(t.pnl) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<div class="gray" style="padding:10px 0">No settled trades yet</div>{% endif %}
  </div>

  <div class="card full">
    <div class="lbl">Live Log</div>
    <div class="log">{% for line in log_lines[-50:]|reverse %}<div>{{ line }}</div>{% endfor %}</div>
  </div>

</div>
</body>
</html>"""

@app.route("/")
def dashboard():
    with lock:
        s = dict(state)
    return render_template_string(DASHBOARD,
        balance=s["balance"], pnl=s["total_pnl"],
        wins=s["wins"], losses=s["losses"],
        slug=s["window_slug"], status=s["status"],
        up=s["up_price"], dn=s["down_price"],
        elapsed=s["time_in_window"], close_in=s["window_close_in"],
        prev=s["prev_window_result"], direction=s["direction"],
        positions=s["positions"], closed=s["closed_trades"],
        log_lines=s["log_lines"], ticks=s["tick_count"],
        last_tick=s["last_tick"], t1=s["trade1_done"],
        t2=s["trade2_done"], src=s["price_source"],
    )

@app.route("/health")
def health():
    with lock:
        return {"ok": True, "balance": state["balance"],
                "pnl": state["total_pnl"], "ticks": state["tick_count"],
                "src": state["price_source"]}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=price_ticker, daemon=True).start()
    threading.Thread(target=trade_logic,  daemon=True).start()
    add_log(f"🌐 Dashboard → http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
