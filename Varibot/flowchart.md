# Varibot flowchart → `varibot.py`

This document maps **`Varibot/varibot.py`** to the **VariBotFlowchart** workflow (see `VariBotFlowchart.jpg` at repo root).

## New file: `Varibot/varibot.py`

### Startup

- Loads `.env` via `load_config()`, then calls **`validate_vr_token.validate_vr_token()`** (same behavior as `validate_vr_token.py`). On failure it logs and exits with a non-zero code.

### Loop (default every 15 minutes on the clock)

1. **Portfolio / TP** — `GET /api/portfolio` + `parse_portfolio_snapshot`, then the same logic as **`check_portfolio_stats`** (`_build_out_dict` + `_apply_tp_check` with `--tp-pct`, default **5%**).
2. **Positions** — `GET /api/positions`; non-zero size ⇒ “has positions” (same idea as **`positions.py`**, no subprocess).
3. **If positions**
   - **TP path:** if `tp_check == Yes` and **`--live`** → runs **`closeallpositions.py --live`** (flowchart’s “closeall”).
   - Else **time-in-position:** compare a **reference time** to **`--time-exit-periods` × T** (default **1 × 15 min**); if age ≥ limit and **`--live`** → **`closeallpositions.py --live`**.
   - **Reference time** (see `--time-exit-source`):
     - **`marketstate` (default):** **`Vari Listings/marketstate.json`** — `fetched_at_unix` (written by `marketstate.py`) or parsed `fetched_at` (SGT string). This matches the flowchart: regime is refreshed **just before** `median_filter` and multimarket orders, so the file timestamp approximates “cycle start” for the current batch (assuming orders ran right after).
     - **`auto`:** `marketstate` → **`/api/orders/v2`** (non–reduce-only order timestamps) → **disk latch**.
     - **`latch`:** only **`Varibot/.varibot_position_latch.json`**.
     - **`orders`:** only the orders API.
4. **If flat** — runs (as subprocesses, same as your runner):
   - **`Vari Listings/listingtable.py`** → `listingtabledata.json` (with timeout + cache fallback like `runner_dir_median_c1s.py`),
   - **`Vari Listings/marketstate.py`**,
   - **`median_filter`** in-process (regime from `marketstate.json`, same long/short rules),
   - **`multimarketorder.py`** with `--long` / `--short` / `--usd` and **`--live`** only if you passed **`--live`**.

### Paths

- Repo root = parent of `Varibot/` (expects **`Vari Listings/`** next to **`Varibot/`**).

## CLI (short)

| Flag | Default | Role |
|------|---------|------|
| `--live` | off | Real **close-all** + real **multimarket** |
| `--period-min` | 15 | Wall-clock step (:00, :15, …) |
| `--tp-pct` | 5 | TP vs portfolio % |
| `--time-exit-periods` | 1 | Max age = this × T for time exit |
| `--usd` | 20 | Per-ticker notional for multimarket |
| `--multi-script` | `multimarketorder.py` | e.g. `multimarketorder_cadence_1s.py` |
| `--once` | — | Single cycle (good for tests) |
| `--no-align` | — | Sleep `period_min` between cycles instead of wall alignment |
| `--time-exit-source` | `marketstate` | `marketstate` \| `auto` \| `latch` \| `orders` — see below |
| `--marketstate-json` | (repo default) | Override path to `marketstate.json` for time-in-position |

Additional flags: `--median-top-n`, `--median-exclude`, `--median-max-oi-skew`, `--listing-timeout-s`, `--marketstate-timeout-s` (see `python3 varibot.py --help`).

### Time-in-position and `marketstate.json`

**Default (`--time-exit-source marketstate`):** use the timestamp from **`marketstate.json`** (`fetched_at_unix` preferred; `fetched_at` string is parsed for older files). `marketstate.py` runs in the entry path **immediately before** `median_filter` and multimarket orders, so under normal operation this time is “just before orders,” as in your flowchart.

**Caveats:** If you have positions but `marketstate.json` was **not** refreshed on that entry cycle (manual trades, failed `marketstate.py`, or stale file), the age can be wrong — use **`--time-exit-source auto`** or **`latch`** as fallback, or re-run `marketstate.py`.

**Other modes:** `orders` = `/api/orders/v2` heuristics; `latch` = Varibot’s `.varibot_position_latch.json`; `auto` tries marketstate → orders → latch.

## Examples

```bash
cd /Users/ryanlee/Documents/Dev/Vari/Varibot
python3 varibot.py --once --usd 20                    # dry-run one cycle
python3 varibot.py --live --usd 20                    # continuous, real trading
python3 varibot.py --once --multi-script multimarketorder_cadence_1s.py --usd 20
```

## Note

Without **`--live`**, TP and time-based exits only **log** what would run; listing/median/multimarket still run but multimarket stays **dry-run** unless you add **`--live`**.

## Script name mapping (flowchart vs repo)

| Flowchart | Repository |
|-----------|------------|
| `closeall.py` | `closeallpositions.py` |
| `listingtable.json` | `listingtabledata.json` (under `Vari Listings/`) |
