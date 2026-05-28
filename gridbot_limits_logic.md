Gridbot new limits logic

Cycle Maintaining of Grid Limits



---
Current logic

Every ~1 min (Varibot one_cycle)
│
├─ 1. Fetch venue mark + pending limit orders (bulk GET)
│
├─ 2. gridstrat pick_tickers (per ticker, e.g. TON)
│      • Load gridstrat_state.json
│      • Resolve price band (lower/upper)
│      • Update sim: fills, paired re-arms (breach reset off by default)
│      • Output meta: grid_buy_rungs, grid_sell_rungs, grid_spacing, grid_anchor, mark, …
│
└─ 3. grid_limits_reconcile (remnant re-arm, no gridlimits.json template)
       • Mark-centered: need N/2 buys below + N/2 sells above mark within
         grid_band_pct around **current mark** (uses meta grid_band_pct, not pinned bounds width)
       • If sufficient → rest (optional cancel out-of-band only; cancels default OFF)
       • If not → infer spacing from remnants; POST missing rungs from mark outward
         (nearest to mark first, then further out)
       • Cancel limits outside band



       Step 1 only: marks + pending (bulk)
Part	API calls	Time
Venue marks (GET /api/metadata/supported_assets)
1
89 ms
Pending limits (GET /api/orders/v2?status=pending, paginated)
3
384 ms
Total
4
~473 ms
Details:

Marks: One bulk supported_assets call → all 15 tickers (AAVE, TON, …). No per-ticker indicative POSTs with your current config (VARIBOT_MARKS_SOURCE=supported_assets).
Pending: 150 resting limits across 15 tickers; 3 paginated pages (50 per page default). Would be up to 6 pages if the book were larger (VARIBOT_PENDING_BULK_MAX_PAGES default 6).
Full one_cycle adds before this
At the start of every cycle, Varibot also calls (not in “step 1” above):

GET portfolio (margin snapshot)
GET positions
So a typical grid cycle is about 6 API calls and ~0.5–1 s for venue reads before strategy/reconcile — plus whatever portfolio rebalance does.

Key env:
- VARIBOT_GRID_LIMITS_RECONCILE=0 — disable live limit POST/cancel
- VARIBOT_GRID_LIMITS_DRIFT_CANCEL=0 — disable keep-depth orphan cancels (default ON; uses CANCEL_ALL_SLEEP_BETWEEN_S pacing)
- VARIBOT_GRID_LIMITS_KEEP_DEPTH=10 — when cancels are enabled, keep nearest K limits per side to mark; cancel deeper ones
- GRID_REARM_ON_BREACH=halt — default; no sim GRID RESET on band breach (remnant maintains 5+5)
- GRID_NEAREST_N — protected window depth per side (default GRID_NUM/2)
