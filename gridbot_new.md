# Vari Grid Bot

### What is a Grid Bot?

A Grid Bot is an automated trading strategy that places multiple buy (Long) and sell (Short) orders at preset price intervals, creating a "grid" of orders. It profits from natural market volatility by buying low and selling high repeatedly — without requiring you to predict market direction.

**How it works:** The bot divides your chosen price range into equal levels (grids). When price drops to a grid level, it opens a long position. When price rises to the next level, it closes for profit. This cycle repeats automatically 24/7.

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

## Interval risk rebalance (MM ≥ 50%)

When maintenance margin usage hits **50%**, the bot can rebalance all live positions to equal **target notional** per leg (7 long / 7 short on 14 tickers when N=15, smallest dropped). One **market order per ticker** (net delta, not reduce-only). Pending grid limits are **not** canceled. Rebalance runs **once per MM episode** (latch until MM drops below trigger).

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

### Run again after a previous rebalance

Latch file blocks repeat runs while MM stays above trigger. Clear it only if you intend another full rebalance:

```bash
rm -f Varibot/.varibot_rebalance_latch.json
cd Varibot && python3 rebalance_run.py --live --force
```

### Env overrides (optional)

| Variable | Default | Meaning |
|----------|---------|---------|
| `VARIBOT_REBALANCE_MM_TRIGGER` | `0.50` | Fire when MM usage ≥ this |
| `VARIBOT_REBALANCE_IM_TRIGGER` | *(deprecated)* | Alias for MM trigger if `MM_TRIGGER` unset |
| `VARIBOT_REBALANCE_IM_TARGET` | `0.20` | Target sizing IM in notional formula |
| `VARIBOT_REBALANCE_ROUND_TO` | `10` | Round target notional to nearest $10 |
| `VARIBOT_REBALANCE_MIN_ORDER_USD` | `5` | Skip legs smaller than this |
| `VARIBOT_REBALANCE_ORDER_INTERVAL_S` | *(auto)* | Seconds between each ticker’s market order; default ≈3.2s at Vari 10 req/10s (3 HTTP calls per leg) |
| `VARI_RATE_LIMIT_MAX` / `VARI_RATE_LIMIT_WINDOW_S` | `10` / `10` | Per-IP cap used to compute default pacing (also enforced on every HTTP call) |
| `MAX_SLIPPAGE` | `0.002` | Market order slippage cap |

`varibot.py --live` also calls the same logic at the start of each cycle when positions exist (respects latch).

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