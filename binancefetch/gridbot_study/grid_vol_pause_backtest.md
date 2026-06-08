# Grid vol-pause backtest (rolling window)

Paired-grid sim on your live ticker list with **production vol-pause** logic (AND gate + BTC/ETH market gate).

## Quick start

```bash
# 1. Refresh 5m klines (incremental — appends from last bar)
python3 binancefetch/gridbot_study/fetch_gridbot_study.py

# 2. Backtest last 12 hours (default)
python3 binancefetch/gridbot_study/grid_vol_pause_backtest.py

# 3. Backtest last 24 hours + save JSON snapshot
python3 binancefetch/gridbot_study/grid_vol_pause_backtest.py --hours 24 --json
```

Run from **repo root**. Tickers and band % come from `GRID_TRADING_TICKERS` in `strategy/gridstrat.py` — edit that dict and re-run; no script changes needed.

## What it simulates

| Item | Value |
|------|-------|
| Engine | `strategy/gridstrat_rearm.py` paired limit (sequential mark steps) |
| Rungs | 8 |
| Notional | $25 × 33x per ticker |
| Data | `gridbot_study_01-07JUN.sqlite` — Binance USDT-M **5m** |
| Market gate | BTC or ETH **1h** return ≤ −2% or ≥ +2% |
| Ticker stress | 30m return ±(band% × **1.6**), 5m bar ±1.2%, vol_ratio ≥ **1.3** |
| Vol ratio | 36-bar σ / 72-bar median (5m) |
| Pause rule | **AND** — market **and** ticker must stress |
| Min hold | 60 min before resume |
| Resume | 18 calm 1-min cycles (≈ 5m bars × 5 cycles/bar) + relaxed thresholds |

Matches `Varibot/grid_vol_pause.py` defaults. Baseline = same grid with **no** vol pause.

## CLI

```
python3 binancefetch/gridbot_study/grid_vol_pause_backtest.py [--hours H] [--db PATH] [--json] [--quiet]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--hours` | 12 | Rolling lookback from latest DB bar |
| `--db` | `gridbot_study_01-07JUN.sqlite` | SQLite path |
| `--json` | off | Write `grid_vol_pause_backtest_last.json` |
| `--quiet` | off | JSON only (no table) |

## Output

Console table: per-ticker baseline PnL, vol-pause PnL, delta, pause count, price move over window.

`--json` writes `grid_vol_pause_backtest_last.json` with full payload (window, params, per-ticker rows, portfolio totals).

## Workflow

1. **Update tickers** — `strategy/gridstrat.py` → `GRID_TRADING_TICKERS`
2. **Fetch data** — `fetch_gridbot_study.py` (pulls grid tickers + BTC/ETH)
3. **Backtest** — `grid_vol_pause_backtest.py --hours N`
4. **Compare windows** — e.g. `--hours 12` vs `--hours 24` vs `--hours 168` (7d)

Warmup: ~110 bars before the window start so 1h market lookback and vol median are valid from the first simulated bar.

## Files

| File | Role |
|------|------|
| `fetch_gridbot_study.py` | Binance → SQLite |
| `grid_vol_pause_backtest.py` | Rolling backtest runner |
| `grid_vol_pause_backtest.md` | This doc |
| `grid_vol_pause_backtest_last.json` | Last `--json` run (gitignored optional) |
| `grid_vol_pause_study.py` | Older 4-ticker sweep (1–7 Jun fixed window) |

## Interpreting results

- **Vol PnL** — portfolio with production pause rules
- **Base PnL** — same grid, never paused
- **Δ$** — vol pause benefit (positive = pause helped)
- In **rising** markets, grids often lose (short inventory); pause can cut damage on sharp moves
- In **dump** windows, pause usually helps more (see `GRID_recc.md` full-week study)

## Example (24h)

```bash
python3 binancefetch/gridbot_study/fetch_gridbot_study.py
python3 binancefetch/gridbot_study/grid_vol_pause_backtest.py --hours 24 --json
```

Latest run (Jun 7 16:15 → Jun 8 16:15 SGT): **vol PnL +$109.17**, baseline +$129.80, Δ −$20.64 (pause hurt in this mixed window; NEAR +12% pump was costly). Results vary by window — compare `--hours 12` vs `24` vs `168`.
