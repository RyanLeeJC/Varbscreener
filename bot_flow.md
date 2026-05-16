# How the grid bot runs (Varibot + gridstrat)

This document describes how the **grid** path works end-to-end: **Varibot** wires venue data, **strategy/gridstrat.py** defines the ladder and state machine, and **multimarketorder.py** places orders. Non-grid paths in Varibot use the same **Varibot-local** strategy feed JSON (no ``Vari Listings`` pipeline).

---

## 1. Initialization (when flat)

When `one_cycle` runs with **no open positions** and the strategy is grid-like (`vari_grid` / `gridstrat` / `invert_extreme` normalized to grid):

### Step A — Mark / listing (venue only)

Varibot does **not** use CoinGecko or ``Vari Listings/``. It calls **`_prepare_varibot_strategy_feed`**, which uses Omni’s indicative quote for `GRID_ASSET` and writes **`Varibot/strategy_listing_snapshot.json`** (one-row `listings` with `mark_price`) plus **`Varibot/strategy_marketstate.json`** so `run_strategy` / gridstrat can read the mark.

Relevant code: `Varibot/varibot.py` — flat branch calls `_prepare_varibot_strategy_feed`, then `run_strategy_pick_tickers` with that `listing_json`.

### Step B — Strategy (`pick_tickers`)

`run_strategy_pick_tickers` loads **`strategy/gridstrat.py`** `pick_tickers`, which:

- Loads **`gridstrat_state.json`** (path from `GRIDSTRAT_STATE_PATH` or default under `Varibot/`).
- Resolves grid bounds (explicit `GRID_LOWER` / `GRID_UPPER` **or** symmetric % band around mark — see §2).
- Builds interior rung prices via `build_price_ladder` (`GRID_NUM`, `GRID_TYPE`).
- Calls **`advance_grid_state`** with current mark, previous `last_mark`, template levels, and a **template fingerprint**.

Returned **`meta`** includes `grid_mode`, `grid_market_events`, ladder metadata (`grid_lower`, `grid_upper`, `grid_num`, buy/sell rung lists, etc.).

### Step C — Optional limit-seed reconciliation (live limit mode)

If limit mode is on and seed/fingerprint tracking is **not** disabled, Varibot may call **`_maybe_clear_grid_limit_seed_when_no_pending_limits`**: if persisted state says limit seeds were built but **GET pending orders** shows **zero** limits for the asset, it clears `limit_seed_built_for_fp` in state so the next `pick_tickers` can emit **catch-up** seed events (e.g. user cancelled all orders in the UI).

### Step D — `grid_limits_reconcile` bootstrap

Before posting strategy events, Varibot calls **`grid_limits_reconcile.run_grid_limits_bootstrap`**:

- Syncs **`gridlimits.json`** from strategy meta (template for recovery / mental map).
- Optionally logs a “mental map” (positions, pending limits, paginated order history) on first cycles or when `VARIBOT_GRID_LIMITS_MAP_EACH_CYCLE` is set.
- Optionally **refills** missing limits from the template when env flags allow (see §5).

### Step E — Execution

**`_execute_grid_market_events(meta, args)`** walks `meta["grid_market_events"]` and, for each `open_buy` / `open_sell` (and limit path specifics), calls **`run_multimarket_asset_side`** → subprocess **`multimarketorder.py`** with `--usd`, and for limits `--limit-price` and optional `--limit-qty`, `--limit-use-mark-price`, `--live`, `--quiet`.

**Summary:** one cycle while flat = **refresh mark → run gridstrat once → optional pending/seed fix → reconcile hook → post every emitted event**.

---

## 2. % band (price bracket)

Handled in **`resolve_grid_bounds`** in `strategy/gridstrat.py`:

| Case | Behaviour |
|------|-----------|
| **Both** `GRID_LOWER` and `GRID_UPPER` set | Those values define the bracket. Auto-pin fields tied to % band are cleared. |
| **Either** bound missing | Symmetric band around **current mark**: `lower = mark × (1 − GRID_BAND_PCT/100)`, `upper = mark × (1 + GRID_BAND_PCT/100)`. |

Auto bounds are **pinned** in persisted state (`pinned_lower`, `pinned_upper`, `pinned_band_pct`) so the bracket **does not re-center every cycle** unless you change `GRID_BAND_PCT` or clear pins via `GRIDSTRAT_RESET` (and no explicit bounds), etc.

**Default `GRID_BAND_PCT`** in code is **`0.5`** (±0.5% if unset). Always confirm your environment overrides.

Interior rungs are **`build_price_ladder(lower, upper, GRID_NUM, GRID_TYPE)`** — prices strictly between lower and upper (arithmetic or geometric per `GRID_TYPE`).

---

## 3. Amount per rung (notional and limit qty)

### USD passed to multimarket (`--usd`)

Per-rung **USD notional** is:

\[
\text{usd\_leg} = \frac{\text{GRID\_INVESTMENT\_USD} \times \text{GRID\_LEVERAGE}}{\text{number of interior rungs}}
\]

Implemented as `per_rung_usd_notional` in `gridstrat.py` (denominator uses the ladder length in that path). Varibot passes this as **`--usd`** on each child `multimarketorder` invocation.

### Limit sizing mode (`GRID_LIMIT_SIZING`)

- **`qty` (default)** — Strategy attaches the same **`qty`** string on every limit event (fixed token size per rung, derived from that USD leg and mark; see gridstrat `_limit_event_qty_field` / `_per_rung_qty_str`). Multimarket still receives `--usd` for IM / sizing guards.
- **`usd`** — No fixed `qty` on events; multimarket derives qty per rung from USD at that rung’s limit price via `qty_string_for_usd_at_price`.

---

## 4. Maintaining the grid (state machine)

All cross / re-arm logic lives in **`advance_grid_state`** in `gridstrat.py` after the template row matches persisted `fingerprint` (unless reset / mismatch triggers a full template rebuild).

Persisted state includes:

- **`buy_armed`**, **`sell_armed`** — which rungs still wait for a cross.
- **`last_mark`** — previous snapshot mark (next cycle’s `prev_mark`).
- **`first_sell_price`** — anchor for the buy-restoration rule.
- **`levels_template`**, **`fingerprint`**, etc.

### Crosses (discrete mark model)

On each cycle, strategy compares **current mark** to **`prev_mark`**:

- **Down through a buy rung** → `open_buy` at that price (`down_cross_buy_rung`), rung removed from `buy_armed`.
- **Up through a sell rung** → `open_sell` (`up_cross_sell_rung`), rung removed from `sell_armed`.

This is driven by **listing mark snapshots**, not by native exchange order-book events.

### Buy restoration (gridbot rule)

After buys fire on the way down, **all buy rungs strictly below the current mark** are re-armed only after mark **crosses upward through** the stored **first sell anchor** (`first_sell_price`). Then a **`grid_restore_buys`** event is recorded and, in limit mode with seeding, **`open_buy`** events can be emitted again for each re-armed rung (`restore_seed_limit_buy`).

### Full template rebuild

When **`fingerprint` ≠** persisted fingerprint **or** **`GRIDSTRAT_RESET`** is truthy, state is rebuilt for the new template: new `buy_armed` / `sell_armed` from the template, and in **limit** mode optional **template seed** events for every buy below mark and every sell above mark (`template_seed_limit_*`). State is saved; `limit_seed_built_for_fp` may be set when seeds actually emit (unless disabled by `GRIDSTRAT_DISABLE_LIMIT_SEED_AND_FP`).

---

## 5. Refilling limit orders (both sides)

Three mechanisms interact:

### A — Normal gridstrat limit seeding / catch-up

- **Template seed** — On new template / mismatch / reset: emit limit **`open_buy` / `open_sell`** for all rungs on the correct side of mark (`template_seed_limit_*`).
- **Catch-up** — If `limit_seed_built_for_fp` does not match the current template fingerprint, emit **`template_seed_limit_*_catchup`** for all buys/sells from the split template.
- **Re-arm after anchor** — After up-cross of first sell anchor, emit **`restore_seed_limit_buy`** for each re-armed buy rung (limit seed path).

### B — Varibot “zero pending limits” clear

If state claims seeds were built but the venue has **no** pending limits for the asset, Varibot strips **`limit_seed_built_for_fp`** from `gridstrat_state.json` so the next `pick_tickers` can emit catch-up seeds again.

### C — `grid_limits_reconcile` (optional)

When **`VARIBOT_GRID_LIMITS_RECONCILE=1`** (and live, limit mode, and **this cycle** `gridstrat` emitted **zero** events):

- Compares **`gridlimits.json`** template rows to **pending** limit keys from the API.
- For each **missing** limit: **buy** only if `limit_price < mark`, **sell** only if `limit_price > mark`; POSTs via the same `place_limit` callback as Varibot uses elsewhere.

With **open positions**, reconcile refill is skipped unless **`VARIBOT_GRID_LIMITS_RECONCILE_WITH_POSITIONS=1`**.

---

## 6. Component roles (quick reference)

| Piece | Role |
|--------|------|
| **`Varibot/varibot.py`** `one_cycle` (flat, grid strategy) | Venue mark → listing row → `run_strategy_pick_tickers` → `grid_limits_reconcile.run_grid_limits_bootstrap` → `_execute_grid_market_events` |
| **`strategy/gridstrat.py`** | Bounds (% or explicit), ladder, persisted arms/crosses, `grid_market_events`, state file |
| **`Varibot/multimarketorder.py`** | One subprocess per event: `--usd` and, for limits, `--limit-price` / `--limit-qty` / etc. |
| **`Varibot/grid_limits_reconcile.py`** | Persist `gridlimits.json`, optional mental map, optional refill when strategy emitted 0 events |

---

## 7. Related behaviour (limits latency and errors)

- **Sequential posting** — Each grid rung is typically a **separate** `multimarketorder.py` subprocess; wall time scales roughly with rung count.
- **Client rate limit** — `VariClient` defaults to a sliding window (**10 requests / 10s** per process); tunable via `VARI_RATE_LIMIT_MAX` / `VARI_RATE_LIMIT_WINDOW_S`. This can add sleeps; it is not “Vari refusing parallel posts” by itself.
- **Exit codes** — For live `--limit-price`, `multimarketorder` now prints a **`multimarket LIMIT … -> OK/FAIL`** line and returns **non-zero** if any order row has an error (so `rc=0` is not assumed when the book is empty).
- **422 qty / tick** — Omni expects `qty × leg ratio` on a `min_qty_tick` lattice; gridstrat / endpoints / `--limit-qty` normalization round and floor qty (see `GRID_LIMIT_QTY_TICK`, significant figures for fixed-qty mode in gridstrat).

---

## 8. See also

- **`gridbot.md`** — Original grid design (restoration rule, ladder semantics).
- **`strategy/gridstrat.py`** module docstring — Env var list (`GRID_*`, `GRIDSTRAT_*`).
- **`Varibot/limit_test.py`** — Isolated-state smoke test for limit ladder posting.
- **`Varibot/limit_order_latency_diag.py`** — Explains rate limiter and subprocess structure.
