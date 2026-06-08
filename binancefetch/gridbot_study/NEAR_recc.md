# NEAR grid volatility pause — recommendations

Simulated NEAR on your Binance 5m data (1–7 Jun SGT) using the paired grid engine (`8` rungs, `±2.5%` band, `$25 × 33x`). Open **`binancefetch/gridbot_study/NEAR_vol_pause_sim.html`** for the chart.

## What happened (baseline, no pause)

| Metric | Value |
|--------|-------|
| Final PnL | **+$155** |
| Max drawdown | **$190** |
| Buy / sell fills | 46 / 51 |
| Final inventory | **−223 NEAR** (net short) |

NEAR **pumped first** (2.2 → 3.1, Jun 1–4), so the grid accumulated **shorts** on sell fills. The crash then triggered many **buy** fills on the way down. Max short exposure peaked at **~$1,392** on Jun 4 evening UTC — right before the heaviest leg down.

Your live book may look more long-heavy if sizing/rungs differ, or if you had pre-existing inventory. The pierce-down pattern (stacked buy fills, few sells) is still the core risk during cascades.

## Volatility pause — how it works

**Pause when ANY of these fire** (recommended set):

| Signal | Window | Threshold |
|--------|--------|-----------|
| **BTC or ETH** 1h return | 12 bars (1h) | ≤ **−1.0%** |
| **NEAR** 30m return | 6 bars | ≤ **−3.0%** |
| **NEAR** single 5m bar | 1 bar | ≤ **−1.2%** |
| **NEAR** realized vol | 36-bar std vs 24h median | > **1.8×** median |

On pause: cancel open limits, **no new fills**, keep inventory.

**Resume** after **18 bars (90 min)** of calm:
- BTC & ETH 1h return > **−0.5%**
- NEAR 30m return > **−1.0%**
- NEAR vol < **1.3×** median
- No fresh single-bar pierce

Then **re-init ladder at current mark** (same idea as flat rebalance).

## Sim results

| Config | PnL | Max DD | Pauses |
|--------|-----|--------|--------|
| **Baseline** | +$155 | $190 | 0 |
| **Recommended** (above) | **+$675** | **$180** | 27 |
| **Production conservative** (BTC **and** NEAR must stress) | +$241 | $288 | 4 |

Recommended catches the Jun 4–6 dump well — e.g. pauses at **Jun 5 09:00 SGT** (NEAR 30m −2.5%, BTC 1h −1.0%) and **Jun 6 12:40 SGT**.

Production conservative only pauses **3 times** in the dump window but with weaker DD control.

## Suggested parameters

**Recommended (best backtest score):**
```
GRID_VOL_PAUSE_MARKET_LB=12          # 1h @ 5m
GRID_VOL_PAUSE_MARKET_RET=-0.01      # BTC or ETH 1h ≤ -1%
GRID_VOL_PAUSE_TICKER_LB=6           # 30m
GRID_VOL_PAUSE_TICKER_CUM_RET=-0.03  # NEAR 30m ≤ -3%
GRID_VOL_PAUSE_TICKER_BAR_RET=-0.012 # single 5m ≤ -1.2%
GRID_VOL_PAUSE_VOL_LB=36             # 3h vol window
GRID_VOL_PAUSE_VOL_RATIO=1.8         # vs 24h median
GRID_VOL_PAUSE_RESUME_BARS=18        # 90 min calm
GRID_VOL_PAUSE_REQUIRE_BOTH=0        # pause on market OR ticker stress
```

**Production conservative (fewer false pauses):**
```
GRID_VOL_PAUSE_MARKET_RET=-0.02
GRID_VOL_PAUSE_TICKER_CUM_RET=-0.04
GRID_VOL_PAUSE_TICKER_BAR_RET=-0.015
GRID_VOL_PAUSE_VOL_RATIO=2.0
GRID_VOL_PAUSE_RESUME_BARS=12
GRID_VOL_PAUSE_REQUIRE_BOTH=1        # need BTC/ETH dump AND NEAR stress
```

## Stress-window calibration (from your data)

On bars where BTC 1h ≤ −2% or NEAR 30m ≤ −3%:
- BTC 1h: median **−1.2%**, worst **−3.1%**
- NEAR 30m: median **−3.3%**, worst **−6.0%**
- NEAR single 5m: median **−0.5%**, worst **−2.3%**

So **−1% market / −3% ticker / −1.2% bar** sit just inside normal stress, not only extremes.

## Practical notes

1. **Asymmetric pause** — For pierce-down long buildup, consider pausing **buy-side only** (keep sells for bounces). The sim pauses all fills; that's safer but leaves more short exposure when NEAR pumps first.

2. **Re-tune to your live config** — Sim uses defaults (`8` rungs, `$25`). Your live `GRID_NUM` / `GRID_INVESTMENT_USD` will change fill count and inventory skew.

3. **Regenerate:** `python3 binancefetch/gridbot_study/near_vol_pause_sim.py`

I can wire these env vars into `gridstrat.py` / `varibot.py` as a `GRID_VOL_PAUSE_*` guard if you want this live.
