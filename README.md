# Polymarket BTC 5-Min Dual-Strike Bot

## Strategy Summary

Each 5-minute window gets **TWO trade opportunities**:

| Trade | Timing | Logic |
|-------|--------|-------|
| Trade 1 (Early) | ~60s into window | Momentum follow — same direction as previous window winner |
| Trade 2 (Late) | ~210s into window | Value entry — only if token is STILL priced below $0.82 with ~90s left |

### Filters
- Skip if token > $0.92 (overpriced, market already decided)
- Skip if token < $0.30 (no conviction, chaotic market)
- 2% taker fee baked into all P&L calculations

---

## Deploy to Railway

1. Push all 3 files to a GitHub repo
2. Connect repo to Railway → New Project
3. No environment variables needed for demo mode

### Optional env vars to customize:
```
PORT=5000
```

---

## Files
- `polymarket_btc5m_bot.py` — main bot + Flask dashboard
- `requirements.txt` — dependencies
- `Procfile` — Railway start command

---

## Market Structure (Confirmed)
- Slug: `btc-updown-5m-{window_ts}` where `window_ts = now - (now % 300)`
- API: `https://gamma-api.polymarket.com/events?slug=...`
- Response: list → `[0]` → `markets[0]`
- Token IDs: `json.loads(markets[0].clobTokenIds)` → `[UP, DOWN]`
- Prices: `json.loads(markets[0].outcomePrices)` → `[UP_price, DOWN_price]`
- Resolution: UP wins if `outcomePrices[0] >= 0.95`

---

## Dashboard
Visit your Railway URL to see:
- Live balance and P&L
- Window countdown bar
- Real-time UP/DOWN prices
- Open positions and settled trade history
- Bot activity log
