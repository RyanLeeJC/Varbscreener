# Vari Grid Bot

# Future Developments
- Reducing Cycle intervals
-
- creating BTC and or ETH counterweight

As grid trading bot, it's best to stay net flat?


### What is a Grid Bot?

A Grid Bot is an automated trading strategy that places multiple buy (Long) and sell (Short) orders at preset price intervals, creating a "grid" of orders. It profits from natural market volatility by buying low and selling high repeatedly — without requiring you to predict market direction.

**How it works:** The bot divides your chosen price range into equal levels (grids). When price drops to a grid level, it opens a long position. When price rises to the next level, it closes for profit. This cycle repeats automatically 24/7.

---

## Finding VIRTUAL-like grid tickers

Use this when one ticker (e.g. **VIRTUAL**) outperforms on a volatile / dump window and you want **similar alts** to add to `GRID_TRADING_TICKERS` in `strategy/gridstrat.py`.

For **absolute chop / low-drift screening** aimed at RPNL (no reference ticker), see **Screening tickers for grid RPNL** below.

**Motivation (example):** VIRTUAL had the best realized grid PnL over a ~12h dump/volatility window. Similar names should share a comparable **FDV tier** and **short-horizon price co-movement** with VIRTUAL so the same band / rung settings behave similarly.

### Workflow (5 steps)

| Step | What | Source |
|------|------|--------|
| 1 | Fetch all Omni perp metadata | `GET /api/metadata/supported_assets` (auth: `Varibot/.env`) |
| 2 | FDV filter | Keep `has_perp` tickers with FDV in **[−25%, +200%]** of reference FDV: `0.75 × ref_fdv ≤ fdv ≤ 3.0 × ref_fdv` |
| 3 | Price history | Binance USD-M perps `GET /fapi/v1/klines` — **5m**, **3 days** (≈864 bars), for ref + each candidate |
| 4 | Correlation & beta | Align timestamps; **log returns** on close; **corr** = Pearson; **beta** = Cov(r, r_ref) / Var(r_ref) |
| 5 | Rank & pick top 5 | Composite **grid_score** (below); sanity-check Vari **24h volume** / OI |

Only tickers with a Binance USDT perpetual mapping are scored (`TICKERUSDT`, or `1000TICKERUSDT` e.g. PEPE).

### grid_score (ranking for grid-like behaviour)

Weights (0–1 scale per component, then combined):

```
grid_score = 0.45 × corr
           + 0.25 × (1 − min(1, |beta − 1| / 0.5))
           + 0.20 × (1 − min(1, |rv − rv_ref| / rv_ref))
           + 0.10 × max(0, −AC1)
```

- **corr** — moves with VIRTUAL during the window  
- **beta ≈ 1** — similar amplitude per VIRTUAL move (good for similar fill churn)  
- **rv** — 5m returns dailyized (`std × √288`) vs VIRTUAL  
- **AC1** — lag-1 return autocorrelation; slight mean-reversion bonus (negative AC1)

**Pure correlation top 5** can differ (high corr + low beta → smaller swings, less grid churn unless bands are tighter).

### Run (maintainer)

From repo root (needs `numpy`, `Varibot/.env` with `VR_TOKEN` / `VR_WALLET_ADDRESS`):

```bash
python3 scripts/find_virtual_like_tickers.py
python3 scripts/find_virtual_like_tickers.py --days 3 --out GridbotData/virtual_similar_analysis.json
```

Writes `GridbotData/virtual_similar_analysis.json` (`top5_grid`, `top5_corr`, FDV band stats).

### Example output (Jun 2026, ref = VIRTUAL, FDV ≈ $708M)

**FDV band:** ~$531M – $2.12B · **49** Vari perps · **43** with Binance klines · **864** aligned 5m bars.

**Top 5 by grid_score**

| # | Ticker | Corr | Beta | FDV vs VIRTUAL | Notes |
|---|--------|------|------|----------------|-------|
| 1 | RENDER | 0.76 | 0.90 | +63% | Best composite; vol closest to VIRTUAL |
| 2 | XPL | 0.71 | 0.83 | +23% | Strong OI on Vari |
| 3 | APT | 0.84 | 0.64 | +41% | Highest corr in set; thinner Vari book |
| 4 | FIL | 0.80 | 0.65 | +142% | High corr; low Vari 24h vol |
| 5 | JUP | 0.70 | 0.69 | +92% | Solid mid-cap alt |

**Practical add (also validated live):** **PENGU** ranked ~#7 on score but **#4 on raw corr (0.84)** and nearly same FDV tier (−22%); worth including if already profitable on the bot.

**Highest raw corr (3d):** PEPE, ENS, APT, PENGU, ETC — several have **beta ~0.5** (smaller moves per VIRTUAL tick).

**Outside FDV band (excluded by step 2, but can still grid well):** ONDO, LINK, SUI, TAO (much larger FDV). Widen `--fdv-hi-pct` if you want them in the scan.

### After selection

1. Add tickers and **% band** to `GRID_TRADING_TICKERS` in `strategy/gridstrat.py` (bands: tune per ticker with 3d sim — see `scripts/grid_rearm_hyperparam.py` / paired sim in `strategy/gridstrat_rearm.py`).
2. Remove retired tickers from the dict; cancel their limits and flatten inventory:
   - `python3 Varibot/cancelalllimitorders.py --underlyings TICK1,TICK2 --live`
   - Flatten off-list positions (reduce-only market; use `position_info.qty` from `GET /api/positions`, not stale top-level fields).
3. Commit / push `gridstrat.py`; restart Varibot so env and file stay in sync.

### Related: per-ticker % band (3d hyperparam)

Once a ticker is shortlisted, sweep **1.5 / 2 / 2.5 / 3%** band on the same 3d Binance 5m path with your rung sizing (e.g. **$300/rung**, **10 rungs**, `halt` breach policy). Example Jun 2026:

| Ticker | 3d move | Best RPNL / tPNL band | Max volume band |
|--------|---------|------------------------|-----------------|
| RENDER | +3.7% | 3.0% | 1.5% |
| XPL | −6.8% | 3.0% | 1.5% |
| JUP | +6.7% | 2.5% RPNL; 3.0% least-bad tPNL | 1.5% |
| WLFI | +1.0% | 1.5% | 1.5% |

Wider bands → fewer fills, often better RPNL on trending windows; tighter bands → more churn.

---

## Screening tickers for grid RPNL (chop & oscillation)

Use this when you want tickers that **maximize realized grid PnL** on a lookback window — not names that merely **move like a reference** (see **Finding VIRTUAL-like grid tickers** above).

**Intuition:** many peak-to-trough oscillations, price chopping inside a range, and **start ≈ end** (not trending hard). A grid earns when price **crosses levels and reverses**, not when it drifts one way.

### What drives RPNL

In the paired re-arm model, each closed round-trip earns roughly:

```
RPNL ≈ (round-trips) × (grid spacing) × (qty per grid)
```

A strong RPNL candidate has enough path to hit many grid levels **and** enough reversals to close paired round-trips. High vol + strong trend often means one-sided fills, inventory buildup, and **low** realized PnL.

### Three pillars → quantifiable metrics

Data source: same as VIRTUAL scan — Binance USD-M **5m** klines, **3 days** (≈864 bars), close (optionally high/low for range).

#### 1. Low net drift (“start ≈ end”)

| Metric | Formula | WLFI example (3d, Jun 2026) | Interpretation |
|--------|---------|----------------------------|----------------|
| **Net move %** | `\|P_T / P_0 − 1\| × 100` | **1.01%** (0.0594 → 0.06) | Primary drift filter |
| **Trend R²** | R² of `log(price)` vs bar index | **0.16** | Low = not trending hard |
| **Drift / range** | `\|P_T − P_0\| / (max − min)` | **~0.20** | Small = chop inside range |

**Screen:** prefer `net_move_pct < 3%` over 3d (WLFI-like), and/or `trend_r2 < 0.3`.

#### 2. Many oscillations (“peak-to-trough churn”)

| Metric | Formula | WLFI example | Interpretation |
|--------|---------|--------------|----------------|
| **Local extrema count** | Peaks + troughs on close series | **235** / 864 bars | Raw “wiggle” count |
| **Extrema rate** | extrema / (bars − 2) | **0.27** | Normalize across tickers |
| **Zero-crossings** | Sign changes of `log(P) − mean(log P)` | **79** | Oscillation around mean |
| **Choppiness index** | `1 − \|P_T − P_0\| / Σ\|ΔP\|` | **0.99** | 1 = pure back-and-forth, 0 = one-way |

**Screen:** high choppiness (>0.85) and extrema rate above median for your universe.

Fills come from **cumulative** path through grid levels, not only single-bar jumps. Do not require large per-bar moves if choppiness and extrema rate are high.

#### 3. Enough vol to hit grids (band-matched)

| Metric | Formula | WLFI example | Interpretation |
|--------|---------|--------------|----------------|
| **RV (dailyized)** | `std(5m log returns) × √288` | **4.1%** | Must exceed ~half your band |
| **Range %** | `(max − min) / min × 100` | **5.0%** | Must span several grid spacings |
| **AC1** | corr(`r_t`, `r_{t−1}`) on 5m log returns | **−0.14** | Negative = slight mean-reversion |

Rule of thumb for planned band `b%`:

- **Range % ≥ 2–3 × b** (WLFI: 5% range vs 1.5% band ✓)
- **RV_daily ≥ ~0.5 × b** in % terms (WLFI: 4% vol vs 1.5% band ✓)

The existing **grid_score** already uses **AC1** as a mean-reversion bonus; that aligns with pillar 2 but ranks vs a reference ticker, not absolute grid profitability.

### grid_rpnl_score v1 (legacy — over-penalizes range names like JUP)

```
grid_rpnl_score_v1 =
    0.30 × (1 − min(1, net_move_pct / 5%))      # full-window net drift
  + 0.25 × choppiness
  + 0.20 × min(1, extrema_rate / 0.35)
  + 0.15 × min(1, range_pct / (3 × band_pct))
  + 0.10 × max(0, −AC1)
```

**Problem:** full-window `net_move_pct` and `trend_r2` punish tickers that trended early then ranged (e.g. JUP). Use **v2** for disable/keep decisions.

### grid_rpnl_score v2 (recommended)

Key extra metrics:

| Metric | Formula | Good grid |
|--------|---------|-----------|
| **drift_range** | `\|P_T − P_0\| / (max − min)` | Low (0.2–0.5) — net move small vs range |
| **range_bound** | `choppiness × (1 − min(1, drift_range/0.6))` | High |
| **last_half** | Same metrics on final 50% of bars | Low `trend_r2`, low `net_move_pct` |

```
grid_rpnl_score_v2 =
    0.25 × (1 − min(1, drift_range / 0.6))
  + 0.25 × choppiness
  + 0.20 × min(1, extrema_rate / 0.35)
  + 0.15 × min(1, range_pct / (3 × band_pct))
  + 0.10 × max(0, −AC1)
  + 0.05 × (1 − trend_penalty)

trend_penalty = min(1, max(0, (trend_r2_last_half − 0.25) / 0.5))
```

**Hard filters (v2 disable):**

1. `range_pct > 1.5 × band_pct` (else grids rarely fire)
2. **Disable** if `drift_range > 0.75` **and** `trend_r2_last_half > 0.5` (hard trend)
3. **Disable** if `net_move_pct_last_half > 8%` (ongoing directional move)

**Run:**

```bash
python3 scripts/score_grid_candidates.py
python3 scripts/score_grid_candidates.py --out GridbotData/grid_rpnl_screen_3d_v2.json
```

WLFI scores very high: ~1% net drift, choppiness ~0.99, negative AC1, range ~3× its 1.5% band → **215 sim fills** and **~$94.5 RPNL** at 1.5% band (`GridbotData/wlfi_band_analysis.json`, `GridbotData/WLFI_sim.html`).

### grid_score vs grid_rpnl_score

| | **grid_score** (existing) | **grid_rpnl_score_v2** |
|--|---------------------------|-------------------------|
| Goal | Find alts that *behave like VIRTUAL* | Find alts that *look good for grid RPNL* |
| Uses | corr, beta, RV vs ref, AC1 | drift_range, choppiness, last-half trend, range vs band |
| Script | `scripts/find_virtual_like_tickers.py` | `scripts/score_grid_candidates.py` |

Use **grid_rpnl_score** to find chop candidates; **grid_score** if you want names that co-move with a profitable reference; then **band sim** as the final gate.

### Band choice is not independent of regime

WLFI 3d band sweep (same path, ~$300/rung sizing in analysis):

| Band | Fills | RPNL |
|------|-------|------|
| 1.5% | 215 | **94.5** |
| 2.0% | 134 | 78.6 |
| 2.5% | 114 | 83.2 |
| 3.0% | 61 | 54.0 |

On a choppy, low-drift name, **tighter band → more round-trips → higher RPNL**. Score and sweep at the **band you plan to run**, not a fixed 3%.

### Recommended workflow

```
Binance 5m klines (3d)
    → compute drift, choppiness, extrema, RV, AC1, range
    → filter + rank by grid_rpnl_score
    → top N → band sweep (wlfi_band_analysis-style / WLFI_sim.html)
    → pick band with best sim RPNL / fill count
    → sanity-check Vari 24h vol + OI
    → add to GRID_TRADING_TICKERS in strategy/gridstrat.py
```

### Caveats

- **Past chop ≠ future chop** — use rolling 3d windows; drop tickers when drift spikes or range collapses.
- **Sim RPNL > live RPNL** — fees, slippage, mark vs Binance, partial fills. Use sim for *relative* ranking.
- **Trending winners look bad on drift metrics** — intentional; grids lose on one-way moves.
- **Correlation cluster** — many alts chop together; the book can still end up net long if everything dumps.

### Example v2 ranking (Jun 2026, 3d Binance 5m)

**Keep (not disabled):** VIRTUAL, WLFI, FET, XPL, **JUP**, ICP

**Disable (v2):** RENDER, ONDO, SEI, NEAR, AVAX, LINK, SUI, TAO, PENGU

JUP moves off the disable list vs v1: `drift_range` 0.62, last-half `trend_r2` 0.06, last-half net move 4.1%.

---

## End-to-End Initialisation Flow

1. **Auth** — `validate_vr_token.py` using `.env` credentials
   → `Auth OK (validate_vr_token)`

2. **Portfolio check** — `check_portfolio_stats.py`
   → `Port Value=498.43  Port uPNL=0.00  IM%=0.00  MM%=0.00`

3. **Mark price check** — pull asset's current mark price
   → `Checking BTC price... 80,000`

4. **Load grid logic and settings** — `gridbotstrat.py`
   → `Entering (GRID_NUM) grid limit orders, between GRID_LOWER and GRID_UPPER`

5. **Submit and verify** — check for valid `rfq_ids` / presence in Open Orders after orders are sent
   → `(GRID_NUM) grid limit orders successfully entered`

6. **Persist state** — write `gridstrat_state.json` with the full active order book, anchor price, and accounting fields (see schema below). This file is the **single source of truth** for the re-arm loop.

---

## Configuration

```
GRID_LOWER=
GRID_UPPER=
GRID_BAND_PCT=0.5        # symmetric ±% around mark when either bound is unset (default 0.5)
GRID_NUM=4               # number of equal steps from lower→upper (fenceposts = GRID_NUM+1 prices)
GRID_TYPE=arithmetic     # or geometric
GRID_INVESTMENT_USD=100
GRID_LEVERAGE=4
GRID_MARKET_SIZING=qty   # qty (default): events include base qty from usd_leg/mark; or 'usd' for legacy
GRID_MARK=               # optional override for mark (else from strategy listing JSON)
GRIDSTRAT_STATE_PATH=    # optional; default Varibot/gridstrat_state.json
GRIDSTRAT_RESET=1        # delete state file on next pick_tickers (one-shot)

# Re-arm policy knobs (NEW)
GRID_REARM_POLICY=paired       # paired (default) | mirror | none
GRID_REARM_ON_BREACH=halt      # halt (default) | slide | reanchor
GRID_REARM_MIN_SPACING_FEE_MULT=4   # require spacing ≥ this × taker_fee for re-arm
```

---

## Initial Grid Layout (worked example)

Given `GRID_NUM=4`, anchor mark = 80,000, spacing = 400:

```
80,800 - sell limit 2
80,400 - sell limit 1
80,000 - current price
79,600 - buy limit 1
79,200 - buy limit 2
```

Each limit order's value is entered in **qty**, not USD, to ensure no imbalance.

```
notional_per_grid = (GRID_INVESTMENT_USD × GRID_LEVERAGE) / GRID_NUM
                  = (100 × 4) / 4
                  = $100 per level

qty_per_grid      = notional_per_grid / mark
                  = 100 / 80,000
                  = 0.00125 BTC
```

Walking the cycle:
- Price up to **80,400** → sell 1 fills → inventory = −0.00125 BTC
- Price up to **80,800** → sell 2 fills → inventory = −0.00250 BTC
- Price back to **80,400** → re-armed buy fills → inventory = −0.00125 BTC, **realize $0.50 PnL**
- Price back to **80,000** → re-armed buy fills → inventory = 0, **realize another $0.50 PnL**

---

## Re-Arming Spec (the part Cursor needs to get right)

### Policy: Paired Replacement

**On every fill, immediately stage one new limit order, one grid spacing on the opposite side of the fill.**

| Fill side | Fill price `P` | New order side | New order price |
|-----------|----------------|----------------|-----------------|
| sell      | `P`            | buy            | `P − spacing`   |
| buy       | `P`            | sell           | `P + spacing`   |

Each new order is **paired** with its parent: when the new (child) order eventually fills, the round-trip closes and PnL = `spacing × qty`.

### State Schema (`gridstrat_state.json`)

```json
{
  "version": 1,
  "symbol": "BTC-PERP",
  "anchor_price": 80000,
  "spacing": 400,
  "grid_type": "arithmetic",
  "qty_per_grid": 0.00125,
  "grid_lower": 79200,
  "grid_upper": 80800,
  "rearm_policy": "paired",
  "rearm_on_breach": "halt",
  "active_orders": [
    {
      "order_id": "exch_id_or_rfq_id",
      "level": 80400,
      "side": "sell",
      "qty": 0.00125,
      "status": "open",
      "origin": "initial",
      "paired_from_order_id": null,
      "placed_at": "2026-05-15T08:00:00Z"
    }
  ],
  "filled_history": [
    {
      "order_id": "...",
      "level": 80400,
      "side": "sell",
      "qty": 0.00125,
      "filled_at": "2026-05-15T08:14:22Z",
      "paired_replacement_order_id": "...",
      "paired_from_order_id": null,
      "realized_pnl": 0.0
    }
  ],
  "net_inventory_qty": 0.0,
  "realized_pnl": 0.0,
  "round_trips": 0,
  "last_updated": "2026-05-15T08:14:22Z"
}
```

### The Re-Arm Loop — Pseudocode

```
loop every POLL_INTERVAL_SECONDS:
    state = load_state_file()
    open_orders_on_exchange = fetch_open_orders(symbol)
    fills = diff(state.active_orders, open_orders_on_exchange)
        # any order in state.active_orders but not in open_orders is a fill

    for fill in fills:
        process_fill_and_rearm(fill, state)

    save_state_file(state)
```

### `process_fill_and_rearm()` — Step by Step

```
function process_fill_and_rearm(fill, state):

    # 1. Update inventory & accounting
    if fill.side == 'sell':
        state.net_inventory_qty -= fill.qty
    else:
        state.net_inventory_qty += fill.qty

    # 2. If this fill closes a paired position, realize PnL
    if fill.paired_from_order_id is not None:
        state.realized_pnl += state.spacing * fill.qty
        state.round_trips += 1
        log("Round-trip closed: +$" + (spacing * qty))

    # 3. Move the order from active_orders → filled_history
    state.filled_history.append(fill_record)
    state.active_orders.remove(fill)

    # 4. Compute paired replacement
    if fill.side == 'sell':
        new_level = fill.level - state.spacing
        new_side  = 'buy'
    else:
        new_level = fill.level + state.spacing
        new_side  = 'sell'

    # 5. SAFETY CHECKS — bail out if any fail
    if not within_grid_bounds(new_level, state):
        log("Re-arm SKIPPED: " + new_level + " outside grid bounds")
        return                                        # per rearm_on_breach=halt

    if has_open_order_at(state, new_level, new_side):
        log("Re-arm SKIPPED: order already exists at " + new_level)
        return

    if not passes_fee_check(state.spacing, mark):
        log("Re-arm SKIPPED: spacing too tight vs fees")
        return

    # 6. Submit the new order to the exchange
    new_order_id = submit_limit_order(
        symbol  = state.symbol,
        side    = new_side,
        price   = new_level,
        qty     = state.qty_per_grid,
    )

    if new_order_id is None:
        log("Re-arm FAILED: exchange rejected order")
        return

    # 7. Append to state
    state.active_orders.append({
        order_id: new_order_id,
        level: new_level,
        side: new_side,
        qty: state.qty_per_grid,
        status: 'open',
        origin: 'rearm',
        paired_from_order_id: fill.order_id,
        placed_at: now(),
    })

    log("Re-arm: placed " + new_side + " @ " + new_level
        + " (paired with " + fill.level + ")")
```

### Critical Edge Cases (DO NOT SKIP)

1. **Multi-fill ticks (price gaps).** The detect-fills step must handle the case where **two or more levels were crossed since the last poll**. Process fills in price order (from the previous mark outward), not arbitrarily — this ensures paired replacements are placed correctly even if both sells fill in one tick.

2. **Conflict detection before placing.** Before submitting a paired order, check `state.active_orders` for an existing open order at the same `(level, side)`. This happens naturally when price oscillates within the grid: a re-armed sell at 80,400 will conflict with the original sell 1 at 80,400 if that's still open. **Skip the placement** — do not double up.

3. **Breach handling (`rearm_on_breach=halt`).** If `new_level` falls outside `[grid_lower, grid_upper]`, skip the placement and log a warning. Do NOT extend the grid automatically in v1. The `slide` and `reanchor` policies are deferred to v2.

4. **Partial fills.** v1 should re-arm **only on full fill** (`status == 'filled'`, not `'partially_filled'`). Track partials in state but don't trigger re-arm logic until the fill completes.

5. **Crash recovery.** On startup, before placing any new orders:
   - Load `gridstrat_state.json`
   - Fetch current open orders from the exchange
   - Reconcile: any order in state but not on exchange = missed fill → process it through `process_fill_and_rearm()` before resuming the main loop.

6. **Idempotency.** Use exchange-issued `order_id` as the primary key everywhere. Never identify an order by `(level, side)` alone — after a few cycles there can be multiple historical orders at the same level.

7. **Atomic state writes.** Write to `gridstrat_state.json.tmp` then `os.replace()` — never leave a half-written state file if the process is killed mid-write.

8. **Fee sanity check** (`passes_fee_check`). Require `spacing / mark ≥ GRID_REARM_MIN_SPACING_FEE_MULT × taker_fee_rate`. If spacing is too tight, fees eat the PnL on every round-trip. Default multiplier 4× leaves room for actual profit.

### What This Re-Arm Policy Optimizes For

- **Volume churn**: every fill immediately stages its own take-profit, so capital is never idle within the grid.
- **Bounded order count**: the total number of working orders never exceeds `GRID_NUM` (a fill is replaced 1:1, so the count is conserved).
- **Deterministic PnL**: each closed round-trip realizes exactly `spacing × qty`, making backtest math trivial.
- **Sliding grid behavior**: as price trends, the working orders shift with it — a feature in ranging markets, a risk in trending ones (which is why `GRID_REARM_ON_BREACH=halt` is the default).

---

## Build Order for Cursor

1. **State module** (`gridstrat_state.py`) — load/save with atomic writes, schema validation, reconciliation on startup.
2. **Re-arm logic** (`gridstrat_rearm.py`) — pure function `process_fill_and_rearm(fill, state) -> (new_orders, log_events)`, no I/O. Unit test this against synthetic fill sequences (use scenarios from the simulator: rally-pullback, oscillate, strong-trend).
3. **Exchange adapter** (`gridstrat_exchange.py`) — `submit_limit_order`, `fetch_open_orders`, `cancel_order`. Keep all exchange-specific quirks here.
4. **Main loop** (`gridbotstrat.py`) — wire the above together, add the polling loop, handle SIGTERM gracefully (cancel-all-on-exit is configurable).
5. **CLI entry points** — `init_grid`, `resume_grid`, `cancel_grid`, `print_state`.

Write `gridstrat_rearm.py` first and test it standalone before touching the exchange adapter — that's where the bugs will be, and it's the only part that has to be perfectly correct.

---

## IM high usage trim (IM ≥ 80%)

When **initial margin (IM) usage** is **≥ 80%** (same trigger as interval risk), Varibot first runs a **reduce-only market trim of 50% of each open position** (`VARIBOT_IM_HIGH_USAGE_TRIM_FRACTION`, default `0.5`; set `0` to disable and fall through to interval risk). Pending grid limits are **not** canceled. The equal-notional interval risk rebalance below does **not** run on the same cycle when this trim fires.

## Interval risk rebalance (IM ≥ 80%, trim disabled)

When IM is high but **`VARIBOT_IM_HIGH_USAGE_TRIM_FRACTION=0`**, Varibot runs equal **target notional** rebalance **every interval** while positions exist and IM stays above the threshold: 7 long / 7 short on 14 tickers when N=15, smallest dropped. One **market order per ticker** (net delta, not reduce-only). Pending grid limits are **not** canceled. There is **no latch file** — each cycle decides independently from the latest portfolio snapshot.

### Run live from terminal

From repo root (needs `Varibot/.env` with `VR_TOKEN`, `VR_WALLET_ADDRESS`):

```bash
cd Varibot
python3 rebalance_run.py --live --force
```

- `--live` — place real market orders  
- `--force` — skip the `yes` confirmation prompt  
- Without `--force`: `python3 rebalance_run.py --live` prompts `Type yes to continue:`

### Dry-run (plan only, no orders)

```bash
cd Varibot
python3 rebalance_run.py
```

### Env overrides (optional)

| Variable | Default | Meaning |
|----------|---------|---------|
| `VARIBOT_REBALANCE_IM_TRIGGER` | `0.80` | Fire IM high-usage trim and/or interval risk when IM usage ≥ this |
| `VARIBOT_IM_HIGH_USAGE_TRIM_FRACTION` | `0.5` | Reduce-only trim fraction on **every** position when IM ≥ trigger (`0` = skip; use interval risk only) |
| `VARIBOT_REBALANCE_MM_TRIGGER` | *(deprecated)* | Alias for IM trigger if `IM_TRIGGER` unset |
| `VARIBOT_REBALANCE_IM_TARGET` | `0.20` | Target notional sizing fraction in planner formula |
| `VARIBOT_REBALANCE_ROUND_TO` | `10` | Round target notional to nearest $10 |
| `VARIBOT_REBALANCE_MIN_ORDER_USD` | `5` | Skip legs smaller than this |
| `VARIBOT_REBALANCE_ORDER_INTERVAL_S` | *(auto)* | Seconds between each ticker’s market order; default ≈3.2s at Vari 10 req/10s (3 HTTP calls per leg) |
| `VARI_RATE_LIMIT_MAX` / `VARI_RATE_LIMIT_WINDOW_S` | `10` / `10` | Per-IP cap used to compute default pacing (also enforced on every HTTP call) |
| `MAX_SLIPPAGE` | `0.002` | Market order slippage cap |
| `VARIBOT_FLATTEN_SLIPPAGE_EXTRA` | `0.001` | +0.10% added to reduce-only trim/flatten slippage (IM trim, notional cap, close-all path) |
| `VARIBOT_POSITION_NOTIONAL_CAP_TRIM_MULTIPLE` | `30` | Per-ticker reduce-only trim when notional exceeds multiple × grid rung (`investment×lev/grid_num`; `0` = off) |
| `VARIBOT_POSITION_NOTIONAL_CAP_TRIM_FRACTION` | `0.5` | Fraction of position qty to cut per trim (default 50%) |

**Notional cap trim** runs every cycle when you have open positions (before IM rebalance): if any ticker’s position notional is **over 30× grid rung USD** (default 30× $400 = **$12,000** with 80×50/10), the bot places a **reduce-only market** order for **50%** of that position’s qty. Grid limits are not canceled.

`varibot.py --live` calls the same logic at the **start of each cycle** when positions exist (no latch).

---

## Railway — Cloudflare error after deploy

After a **git push** deploy, logs may show:

```text
VariCloudflareError: Cloudflare HTML challenge for GET /api/portfolio?compute_margin=true
```

Vari’s API returned a Cloudflare/WAF HTML page instead of JSON (common on cloud egress IPs). The bot may still print `cycle: complete` and sleep until the next interval, but API calls will keep failing until the challenge is cleared.

### Fix (do this)

1. In Railway, open the **Gridbot** service.
2. **Remove deployment** (delete the active deployment / service instance — not just restart).
3. **Deploy again** (new deployment from the same branch, e.g. `GridBot`).

That gives you a **fresh** runtime and often a new egress path, which clears the challenge.

### Do not do this

- **Redeploy** (re-run the same deployment in place) — usually **does not** clear the Cloudflare challenge; you will keep seeing the same error.

### RWA pending orders (`XAU`, `CL`, `XAG`, `COPPER`)

If logs show:

```text
GET /api/orders/v2?instrument=P-XAU-USDC … 400 Incorrect number of fields provided
```

Omni expects **four** hyphen segments for crypto (`P-ETH-USDC-3600`). Commodity perps reject `P-XAU-USDC` (three segments). The bot **omits** the `instrument` query param for RWA and filters by underlying client-side.

To confirm the correct filter string (if Vari adds one later), in DevTools → Network → filter `orders/v2` → copy the **`instrument`** query value from a working pending-orders request on an RWA market (e.g. XAU). Send that full string; we can wire it into `instrument_query_param()` in `variationalbot/vari/endpoints.py`.

### Optional hardening

If challenges recur, set a proxy Cloudflare accepts (see `Varibot/env.example`):

```bash
HTTPS_PROXY=https://user:pass@host:port
```

---

## Bootstrap performance (cycle-1 cold start)

On the first cycle, `grid_limits_reconcile.run_grid_limits_bootstrap` runs a **heavy map** per grid ticker: pending limits + (optionally) paginated `GET /api/orders/v2` history (`GRID_ORDERS_MAX_PAGES`, default 40). With ~20 tickers that can mean **10+ minutes** of silent API work before the first `posting N missing limit(s)` line.

### Implemented

- **Skip order history on fresh flat** — When the account has **no open positions** and a ticker has **no pending limits**, cycle-1 map is **pending-only** (no history pagination). Logs: `gridlimits[ETH] map: skip order history (flat account, no pending limits)`. Code: `_skip_order_history_on_flat_map` in `Varibot/grid_limits_reconcile.py`.

- **Bulk marks via `GET /api/metadata/supported_assets`** — Default `VARIBOT_MARKS_SOURCE=supported_assets`: one request for all Omni marks (~0.1s) instead of 20×2 `POST /api/quotes/indicative`. Marks are prefetched once per cycle (`cycle_bulk_marks`) and reused for listing snapshot, `pick_tickers`, and rebalance. Set `VARIBOT_MARKS_SOURCE=indicative` to restore per-ticker quotes. Parsed from each row’s `price` (then `mark_price`, then `index_price`); see **supported_assets mark lag** below when using the bulk default.

### supported_assets mark lag (~3 minutes behind wall clock)

**Investigated May 2026 (VIRTUAL spike, `Remnant delay_case.txt`).** The venue mark logged as `step: venue mark … (supported_assets)` is **not** the same as the live mark on the Omni 1m chart at fetch time. In a fast move, `GET /api/metadata/supported_assets` can trail wall clock by about **three minutes** — not merely the ~1–2s between the minute boundary and when Varibot finishes the GET.

**Anchor example:** a fetch at **13:08:01** aligned with the chart’s **13:05** 1m bar **close** (not 13:08). During the incident, 13:06’s fetch looked like **13:04** pricing (~0.7077 vs chart ~0.714+), and 13:07’s fetch aligned with **13:05 open** (~0.7090 vs 13:05 O 0.7092).

| Cycle minute | Log time (SGT) | Bot VIRTUAL mark (`supported_assets`) |
|--------------|----------------|---------------------------------------|
| 13:05 | 13:05:02.304 | 0.710336 |
| 13:06 | 13:06:01.709 | 0.707738 |
| 13:07 | 13:07:02.147 | 0.709037 |
| 13:08 | 13:08:01.691 | 0.713925 |

**Operational impact:** gridstrat + remnant anchor on this stale mark for the whole cycle. With existing sell limits still near the live tape (~0.714) and a low bulk mark (~0.708), remnant can post sell rungs **below** the floating mark → immediate fills (see `Remnant delay_case.txt`, cycle 990).

**Mitigation (implemented):** live remnant reconcile fetches a fresh `POST /api/quotes/indicative` mark **per ticker** immediately before posting missing limits (after the bulk pending sweep). Gridstrat / listing still use one bulk `supported_assets` GET by default (`VARIBOT_MARKS_SOURCE=supported_assets`). Set `VARIBOT_GRID_LIMITS_MARK_INDICATIVE=0` to revert reconcile to the stale `grid_mark` from gridstrat.

- **Per-ticker indicative marks in remnant reconcile (May 2026)** — Cycle order: bulk pending → gridstrat (bulk marks for sim/listing) → for each ticker: indicative `mark_price` → remnant post. Logs: `gridlimits[ETH] indicative mark=… (POST /api/quotes/indicative)` then `gridlimits[ETH] remnant: … (mark=…)`.

- **Bulk pending via paginated `GET /api/orders/v2?status=pending`** — Default `VARIBOT_PENDING_BULK=1`: one sweep (up to `VARIBOT_PENDING_BULK_MAX_PAGES` pages) per cycle, bucketed by `instrument.underlying`, reused in `_grid_venue_inputs_for_cycle` (pass 1) and `run_grid_limits_bootstrap` (pass 2). Per-ticker refresh still runs after drift cancel. Set `VARIBOT_PENDING_BULK=0` for legacy per-ticker GETs.

### Future dev (not yet implemented)

1. **Progress logs per asset in heavy_map** — e.g. `gridlimits[ETH] map: done (pending=0, history_pages=2)` so long map phases are visible in Railway logs.

2. **`set_leverage` once per asset per cycle** — `_grid_limits_place_limit_fn` calls `set_leverage` on every limit POST (~10× per ticker). Cache per-asset leverage for the reconcile loop.

3. **Env tuning (ops, no code)** — `GRID_ORDERS_MAX_PAGES=2` if full history is needed on cycle 1; avoid `VARIBOT_GRID_LIMITS_MAP_EACH_CYCLE=1` unless debugging.

---

## Render logs (`fetch_render_logs.py`)

Download service logs via the Render API (`GET /v1/logs`). The path `/v1/services/{id}/logs` returns 404; use `Varibot/fetch_render_logs.py` instead (paginates with `hasMore`, max 100 lines per page).

### Usage

From repo root — loads `Render_API_KEY` / `RENDER_API_KEY` from `Varibot/.env` automatically (shell export optional):

```bash
python3 Varibot/fetch_render_logs.py -o logs.txt
```

**Defaults:**

- Service: `srv-d86tvu3eo5us73ccj3jg`
- Window: last 24 hours
- Output: `logs.txt` (tab-separated `timestamp` + `message`)

**Options:**

```bash
python3 Varibot/fetch_render_logs.py --hours 48 -o logs.txt
python3 Varibot/fetch_render_logs.py --all-types          # include build/request logs
python3 Varibot/fetch_render_logs.py --resume             # after 429 or Ctrl-C
python3 Varibot/fetch_render_logs.py --delay 2 --chunk-hours 4   # slower / safer
```

Defaults to **`app` logs only** (fewer API pages than build/request). Paces at **1.25s/page**, splits the window into **4h chunks** with a **5s pause** between chunks, **retries 429** with backoff, and writes **`logs.txt.progress.json`** for `--resume`.

Resolves `ownerId` from the service automatically. A full 24h run can take **~20–40+ minutes** depending on log volume; use `--resume` if rate-limited.

---

## Grid ticker rotation (auto roster)

Daily pipeline: rank top **40** Vari tickers by 24h volume (minus `vari-blacklist` in [`Vari Listings/vari_crypto_categories.json`](Vari%20Listings/vari_crypto_categories.json)) → 48h band hyperparam on Binance 5m → top **10** sim PnL candidates → replace bottom **10** of the live roster by **24h total PnL** (RPNL + uPNL from exports). Varibot **cancels limits and flattens** removed tickers; new bands live in `.grid_trading_roster.json`.

**Do not** use `GRID_TRADING_TICKERS` env for daily updates (Render env requires redeploy). Roster file + Key Value handoff instead.

| Component | Role |
|-----------|------|
| **Cron Job** (Render) or manual CLI | Heavy eval: volume → klines → hyperparam → write pending plan |
| **Gridbot** (`varibot.py`) | Each cycle: read pending → exit removed tickers → save roster → reconcile orphans |
| **Active roster** | `Varibot/.grid_trading_roster.json` (or path in `GRID_TRADING_ROSTER_PATH`) |
| **Pending swap** | File (local) or Render Key Value (production) |

Code: [`Varibot/grid_ticker_rotation.py`](Varibot/grid_ticker_rotation.py), sim: [`binancefetch/gridbot_study/grid_rotation_sim.py`](binancefetch/gridbot_study/grid_rotation_sim.py).

### Env vars

| Variable | Local default | Render | Meaning |
|----------|---------------|--------|---------|
| `GRID_TICKER_ROTATION_ENABLED` | `1` (see `.env`) | `1` | Master switch |
| `GRID_ROTATION_STORE` | `file` | `keyvalue` | `file` = local JSON; `keyvalue` = Render Key Value |
| `GRID_ROSTER_SIZE` | `20` | `20` | Target active tickers |
| `GRID_ROTATION_CANDIDATE_POOL` | `40` | `40` | Volume-ranked universe before hyperparam |
| `GRID_ROTATION_SWAP_COUNT` | `10` | `10` | Max replacements per day |
| `GRID_TRADING_ROSTER_PATH` | `.grid_trading_roster.json` | same | Active roster (under `Varibot/`) |
| `GRID_ROTATION_PENDING_PATH` | `.grid_rotation_pending.json` | — | Pending plan (file store only) |
| `KEY_VALUE_URL` | — | **required** | Redis URL from Render Key Value |
| `GRID_ROTATION_KEYVALUE_KEY` | `grid:rotation:pending` | same | Redis key for pending JSON |
| `GRID_ROTATION_BLACKLIST_JSON` | `../Vari Listings/vari_crypto_categories.json` | same path in image | `vari-blacklist` source |

Also scrubbed automatically: **BTC**, **ETH**, **RWA** tickers, non–crypto-perp rows from `supported_assets`.

---

### Enable locally (file store)

**1. Install deps** (includes `redis` for optional Key Value; file store does not need Redis running):

```bash
cd /path/to/Vari
python3 -m pip install -r requirements.txt
```

**2. Env** — already in `Varibot/.env` if you pulled latest:

```bash
GRID_TICKER_ROTATION_ENABLED=1
GRID_ROTATION_STORE=file
GRID_ROSTER_SIZE=20
GRID_ROTATION_CANDIDATE_POOL=40
GRID_ROTATION_SWAP_COUNT=10
GRID_TRADING_ROSTER_PATH=.grid_trading_roster.json
GRID_ROTATION_PENDING_PATH=.grid_rotation_pending.json
GRID_ROTATION_BLACKLIST_JSON=../Vari Listings/vari_crypto_categories.json
```

**3. Dry-run eval** (no pending write; ~3–5 min, Binance + Vari API):

```bash
cd Varibot
python3 grid_ticker_rotation.py --evaluate --json
```

Inspect `plan.remove`, `plan.add`, `plan.roster_after` in the JSON.

**4. Write pending plan** (does not touch the venue):

```bash
cd Varibot
python3 grid_ticker_rotation.py --evaluate --write-pending
cat .grid_rotation_pending.json
```

**5. Apply manually once** (optional test before leaving it to varibot):

```bash
cd Varibot
python3 grid_ticker_rotation.py --apply-pending --live   # live flatten + roster update
# or without --live for dry-run log only
```

**6. Run bot** — each `one_cycle()` applies any unapplied pending file, then reconciles orphans:

```bash
cd Varibot
python3 varibot.py --live
```

**7. Seed roster** — on first run, if `.grid_trading_roster.json` is missing, it is created from static `GRID_TRADING_TICKERS` in `strategy/gridstrat.py`. After rotation, `grid_trading_ticker_band_pcts()` reads the roster file (unless `GRID_TRADING_TICKERS` env override is set).

**Local checklist**

- [ ] `--evaluate --json` looks sane (no surprise evictions)
- [ ] `--write-pending` then inspect `.grid_rotation_pending.json`
- [ ] `--apply-pending` once in dry-run, then `--live` when ready
- [ ] `varibot.py --live` with `GRID_TICKER_ROTATION_ENABLED=1`

---

### Enable on Render (step-by-step)

Render splits **heavy eval** (Cron Job, ~3–5 min/day) from **light apply** (Gridbot web service, seconds per cycle). Cron and Gridbot **cannot share a disk**; use **Render Key Value** (Redis) to pass one JSON blob per day.

#### Phase A — Prerequisites

1. **Code on branch** — push commits that include `grid_ticker_rotation.py`, `grid_rotation_sim.py`, `render.yaml`, and updated `requirements.txt` (`redis` package).
2. **Existing Gridbot** — your Production Docker service (`python3 Varibot/varibot.py --live`) in Singapore (or same region as Cron).
3. **Blacklist file in image** — `Vari Listings/vari_crypto_categories.json` is in the repo and copied by `Dockerfile` (`COPY . .`), so Cron and Gridbot both see it at `/app/Vari Listings/vari_crypto_categories.json`. Set on both services:
   ```bash
   GRID_ROTATION_BLACKLIST_JSON=/app/Vari Listings/vari_crypto_categories.json
   ```

#### Phase B — Create Render Key Value

1. Open [Render Dashboard](https://dashboard.render.com) → your **Production** environment (or create one).
2. Click **+ New** → **Key Value** (Redis-compatible).
3. Name it e.g. `gridbot-rotation-kv`, region **Singapore** (same as Gridbot).
4. Create the instance. When ready, open it and copy the **Internal Redis URL** (or **External** if Cron/Gridbot require it — prefer **Internal** when both services are in the same Render account/region).
5. Save the URL — looks like:
   ```text
   redis://red-xxxxxxxx:6379
   ```
   Render may also expose `REDIS_URL`; either works as `KEY_VALUE_URL`.

#### Phase C — Environment Group (shared secrets)

1. Dashboard → **Environment Groups** → **+ New Environment Group** (e.g. `gridbot-production`).
2. Add variables (same values Gridbot already uses, plus rotation):

   | Key | Value |
   |-----|--------|
   | `VR_WALLET_ADDRESS` | *(existing)* |
   | `VR_TOKEN` | *(existing)* |
   | `GRID_TICKER_ROTATION_ENABLED` | `1` |
   | `GRID_ROTATION_STORE` | `keyvalue` |
   | `KEY_VALUE_URL` | *(paste Redis URL from Phase B)* |
   | `GRID_ROTATION_KEYVALUE_KEY` | `grid:rotation:pending` |
   | `GRID_ROSTER_SIZE` | `20` |
   | `GRID_ROTATION_CANDIDATE_POOL` | `40` |
   | `GRID_ROTATION_SWAP_COUNT` | `10` |
   | `GRID_ROTATION_BLACKLIST_JSON` | `/app/Vari Listings/vari_crypto_categories.json` |
   | `GRID_TRADING_ROSTER_PATH` | `/app/Varibot/.grid_trading_roster.json` |

3. **Do not** set `GRID_TRADING_TICKERS` for daily rotation (manual override only).
4. Link this group to **both** Gridbot and the Cron Job (Phase D/E): service → **Environment** → **Link Environment Group**.

**Optional — persistent roster on Gridbot:** attach a **Persistent Disk** to the **Gridbot web service only** (not Cron), mount e.g. at `/var/data`, set `GRID_TRADING_ROSTER_PATH=/var/data/.grid_trading_roster.json`. Cron still writes pending to Key Value only.

#### Phase D — Update existing Gridbot web service

1. Open service **Gridbot** → **Environment**.
2. Confirm the Environment Group from Phase C is linked (or paste the same vars manually).
3. Verify **Start Command**:
   ```bash
   python3 Varibot/varibot.py --live
   ```
4. **Deploy** (manual deploy or push to connected branch).
5. After deploy, check **Logs** on first cycle — you should **not** see rotation apply until Cron writes pending (unless you manually seeded Key Value). Orphan reconcile runs only when `GRID_TICKER_ROTATION_ENABLED=1`.

#### Phase E — Create Cron Job (daily eval)

1. **+ New** → **Cron Job**.
2. **Name:** `grid-rotation-eval`
3. **Region:** Singapore (match Gridbot).
4. **Source:** same Git repo + branch as Gridbot.
5. **Runtime:** **Docker** (use repo `Dockerfile`, same as Gridbot).
6. **Schedule:** `0 4 * * *` = 04:00 UTC daily (~12:00 SGT). Adjust:
   - `0 20 * * *` = 04:00 SGT (UTC+8)
7. **Command:**
   ```bash
   python3 Varibot/grid_ticker_rotation.py --evaluate --write-pending
   ```
8. **Instance type:** **Standard** (2 GB RAM) — enough for 40 tickers × 5 bands.
9. **Environment:** link the **same Environment Group** as Gridbot (must include `VR_*`, `KEY_VALUE_URL`, `GRID_ROTATION_STORE=keyvalue`).
10. **Create Cron Job**.

Alternatively import [`render.yaml`](render.yaml) via Blueprint and merge with your existing Gridbot service (avoid duplicating the web service if one already exists).

#### Phase F — First run (safe rollout)

**Option 1 — Manual eval before enabling live apply**

1. Locally (with production `VR_*` in env), dry-run:
   ```bash
   cd Varibot
   python3 grid_ticker_rotation.py --evaluate --json
   ```
2. On Render, open Cron Job → **Trigger Run** (if available) or wait for schedule.
3. Cron logs should end with universe count + `Wrote pending plan to store` (or `plan_skipped` if no changes).
4. Within ~1 minute, Gridbot logs should show:
   ```text
   grid_rotation: applied swap remove=[...] add=[...] roster_size=20
   ```
5. If something looks wrong, set `GRID_TICKER_ROTATION_ENABLED=0` on Gridbot to stop apply (Cron can keep evaluating).

**Option 2 — Staged enable**

1. Deploy Cron + Key Value with `GRID_TICKER_ROTATION_ENABLED=0` on Gridbot.
2. Run Cron once; inspect pending in Redis (CLI or temporary log line).
3. Set `GRID_TICKER_ROTATION_ENABLED=1` on Gridbot and redeploy.

#### Phase G — Daily operation

```mermaid
sequenceDiagram
  participant Cron as Cron_04UTC
  participant KV as Key_Value
  participant Bot as Gridbot

  Cron->>Cron: Top40 vol blacklist scrub
  Cron->>Cron: Fetch 48h klines hyperparam
  Cron->>Cron: Bottom10 live PnL evict
  Cron->>KV: SET grid:rotation:pending
  Bot->>KV: GET pending each cycle
  Bot->>Bot: Cancel limits flatten removed
  Bot->>Bot: Save roster JSON
  Bot->>KV: Mark applied_at
  Bot->>Bot: Trade new roster
```

| Time | What happens |
|------|----------------|
| Cron schedule | Eval runs 3–5 min, writes pending to Key Value |
| Next Gridbot cycle (~1 min) | Applies swap: cancel + flatten removed tickers, updates roster |
| Every cycle | Orphan check: any limit/position on non-roster ticker → exit |
| Vol pause / pain pause | Unchanged; paused tickers skipped for eviction that day |

#### Phase H — Troubleshooting

| Symptom | Check |
|---------|--------|
| Cron fails immediately | Logs: missing `VR_*`, Binance rate limit, or `KEY_VALUE_URL` |
| Gridbot never applies | `GRID_TICKER_ROTATION_ENABLED=1`, `GRID_ROTATION_STORE=keyvalue`, same `KEY_VALUE_URL`, pending has no `applied_at` |
| Wrong tickers in universe | Edit `vari-blacklist` in repo; redeploy both services |
| Roster not persisting across deploys | Add Persistent Disk on Gridbot + `GRID_TRADING_ROSTER_PATH` on disk |
| Want to pause rotation | `GRID_TICKER_ROTATION_ENABLED=0` on Gridbot only |
| Manual override roster | Set `GRID_TRADING_TICKERS=ENA:3,XLM:2,...` on Gridbot (disables roster file until unset) |

#### Render checklist

- [ ] Key Value created; `KEY_VALUE_URL` in Environment Group
- [ ] Environment Group linked to **Gridbot** and **grid-rotation-eval** Cron
- [ ] Cron command: `python3 Varibot/grid_ticker_rotation.py --evaluate --write-pending`
- [ ] Cron schedule set (e.g. `0 4 * * *`)
- [ ] Gridbot: `GRID_TICKER_ROTATION_ENABLED=1`, `GRID_ROTATION_STORE=keyvalue`
- [ ] First Cron run logged; Gridbot applied swap on next cycle
- [ ] Removed tickers: limits canceled, positions flat in Omni UI

---

Phase 2 development

When doing gridbot trading on single ticker, or on 2 tickers, at this time, there's not way to hedge the inventory.

If single ticker, you have buy limits below and sell limits above current price.
What if price dumps or pumps massively? The account UPNL will be stuck in huge loss, and impossible to unstuck unless price recovers back or it oscillate sideways enough times.
Doing on two tickers simply doubles the exposure of such risk.

It's not possible to do the opposite on the second ticker, because on Vari you can't set sell limits below the current mark price, or set buy limits above the current mark price.

I thought of a way to hedge the inventory risk, and still have a way to perform opposite grid trading on two tickers.

First, during initialisation of the gridbot,
long $1,000 of ETH
short $1,000 of LINK (2nd highest correlation to ETH other than BTC)
-> inventory is hedged.

For ETH,
initial $1,000 long
do the usual grid trading. buy limits below current mark price. sell limits above current mark price. Re-arms limit orders as per current codebase.

For LINK, 
initial $1,000 short
set s.l orders at the grid rungs above.
set t.p orders at the grid rungs below.