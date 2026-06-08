# Grid volatility pause — AVAX JUP FET RENDER (1–7 Jun 2026)

Paired grid sim: 8 rungs, $25 × 33x leverage, per-ticker `GRID_TRADING_TICKERS` band.
Data: `gridbot_study_01-07JUN.sqlite` (Binance USDT-M 5m).
Market inputs: **BTC, ETH** only. **NEAR excluded** (low correlation / idiosyncratic pump-crash).

## Market alignment (5m return correlation)

| Ticker | corr BTC | corr ETH | corr mkt avg |
|--------|----------|----------|--------------|
| AVAX | 0.81 | 0.84 | 0.84 |
| JUP | 0.75 | 0.77 | 0.78 |
| FET | 0.66 | 0.67 | 0.69 |
| RENDER | 0.73 | 0.74 | 0.75 |

## Global pause rules (joint sweep on these 4 tickers)

**Recommended (joint best score across AVAX/JUP/FET/RENDER):**
```
GRID_VOL_PAUSE_MARKET_RET=-0.02
GRID_VOL_PAUSE_TICKER_CUM_RET=-0.04
GRID_VOL_PAUSE_TICKER_BAR_RET=-0.012
GRID_VOL_PAUSE_VOL_RATIO=1.8
GRID_VOL_PAUSE_RESUME_BARS=18
GRID_VOL_PAUSE_REQUIRE_BOTH=1
```

**Production conservative (AND trigger — fewer pauses):**
```
GRID_VOL_PAUSE_MARKET_RET=-0.02
GRID_VOL_PAUSE_TICKER_CUM_RET=-0.04
GRID_VOL_PAUSE_TICKER_BAR_RET=-0.015
GRID_VOL_PAUSE_VOL_RATIO=2.0
GRID_VOL_PAUSE_RESUME_BARS=12
GRID_VOL_PAUSE_REQUIRE_BOTH=1
```

## Summary table

| Ticker | Band% | Price Δ | Base PnL | Base DD | Rec PnL | Rec DD | Δ PnL | Δ DD | Rec pauses | Best help? |
|--------|-------|---------|----------|---------|---------|--------|-------|------|------------|------------|
| AVAX | 2.0 | -25.4% | $-88 | $117 | $-130 | $224 | $-42 | $-107 | 3 | no |
| JUP | 2.5 | -15.8% | $27 | $117 | $253 | $137 | $+227 | $-21 | 6 | mixed |
| FET | 3.0 | -21.5% | $348 | $143 | $357 | $143 | $+10 | $+0 | 5 | yes |
| RENDER | 3.0 | -19.1% | $360 | $226 | $555 | $194 | $+195 | $+32 | 5 | yes |

## Per-ticker detail

### AVAX (band 2.0%)

- **Market corr:** BTC 0.81, ETH 0.84, avg 0.84
- **Price move:** -25.4% | final inv: 46.4 AVAX
- **Baseline:** PnL $-88.26, DD $117.40, max long $406, max short $316, fills 25/22
- **Global recommended:** PnL $-129.89 (-41.63), DD $224.20 (-106.80), 3 pauses (3 in Jun 4–7 dump)
- **Global production:** PnL $-144.60, DD $224.20, 2 pauses
- **Per-ticker best sweep:** PnL $-129.89, DD $224.20, score -343.8
- **Stress quantiles** (26 bars): 30m ret median -2.08%, worst -4.32%
- **Suggested refinement:** `ticker_cum_ret=-0.02`, `ticker_bar_ret=-0.0096`, `vol_ratio=1.8`, `require_both=1`
  - tight band — prefer require_market_and_ticker=1; global OR-pause underperformed; use per-ticker sweep or production conservative
- **Best sweep params:** market_ret=-0.01, ticker_cum=-0.04, ticker_bar=-0.016, vol=1.8, resume=12, both=1

### JUP (band 2.5%)

- **Market corr:** BTC 0.75, ETH 0.77, avg 0.78
- **Price move:** -15.8% | final inv: 566.3 JUP
- **Baseline:** PnL $26.93, DD $116.52, max long $101, max short $853, fills 42/40
- **Global recommended:** PnL $253.50 (+226.57), DD $137.43 (-20.90), 6 pauses (5 in Jun 4–7 dump)
- **Global production:** PnL $254.81, DD $137.43, 6 pauses
- **Per-ticker best sweep:** PnL $485.66, DD $193.26, score 330.3
- **Stress quantiles** (43 bars): 30m ret median -3.15%, worst -7.30%
- **Suggested refinement:** `ticker_cum_ret=-0.0268`, `ticker_bar_ret=-0.012`, `vol_ratio=1.8`, `require_both=0`
- **Best sweep params:** market_ret=-0.015, ticker_cum=-0.025, ticker_bar=-0.01, vol=1.8, resume=18, both=0

### FET (band 3.0%)

- **Market corr:** BTC 0.66, ETH 0.67, avg 0.69
- **Price move:** -21.5% | final inv: -4283.9 FET
- **Baseline:** PnL $347.74, DD $143.43, max long $205, max short $2041, fills 66/71
- **Global recommended:** PnL $357.29 (+9.55), DD $143.43 (+0.00), 5 pauses (5 in Jun 4–7 dump)
- **Global production:** PnL $296.61, DD $143.43, 3 pauses
- **Per-ticker best sweep:** PnL $459.30, DD $169.42, score 405.6
- **Stress quantiles** (48 bars): 30m ret median -3.25%, worst -6.23%
- **Suggested refinement:** `ticker_cum_ret=-0.0277`, `ticker_bar_ret=-0.0144`, `vol_ratio=2.0`, `require_both=0`
  - wide band — tolerate larger single-bar moves before pause
- **Best sweep params:** market_ret=-0.015, ticker_cum=-0.05, ticker_bar=-0.016, vol=1.8, resume=12, both=0

### RENDER (band 3.0%)

- **Market corr:** BTC 0.73, ETH 0.74, avg 0.75
- **Price move:** -19.1% | final inv: -659.1 RENDER
- **Baseline:** PnL $360.03, DD $226.18, max long $103, max short $2249, fills 49/67
- **Global recommended:** PnL $555.44 (+195.41), DD $194.23 (+31.96), 5 pauses (4 in Jun 4–7 dump)
- **Global production:** PnL $443.38, DD $226.18, 3 pauses
- **Per-ticker best sweep:** PnL $920.95, DD $321.96, score 726.3
- **Stress quantiles** (41 bars): 30m ret median -3.32%, worst -5.38%
- **Suggested refinement:** `ticker_cum_ret=-0.0282`, `ticker_bar_ret=-0.0144`, `vol_ratio=2.0`, `require_both=0`
  - wide band — tolerate larger single-bar moves before pause
- **Best sweep params:** market_ret=-0.02, ticker_cum=-0.04, ticker_bar=-0.01, vol=1.8, resume=12, both=0


## Executive summary

- **Joint global params:** market_ret=-0.02, ticker_cum=-0.04, ticker_bar=-0.012, vol_ratio=1.8, resume_bars=18, require_both=1 (AND trigger).
- **Global helps PnL:** JUP, RENDER
- **Global hurts PnL:** AVAX
- **OR vs AND:** OR = pause when market **or** ticker stresses; AND = both must stress.
- Joint sweep on this cohort picked **AND** (not NEAR-era OR) — market + ticker must stress together.
- Per-ticker best sweep still beats joint global — use overrides where needed.

## Cross-ticker refinements (4-ticker cohort)

1. **All four correlate with BTC/ETH** (0.66–0.84 on 5m returns). Highest: **AVAX**; lowest: **FET**. NEAR was correctly excluded from this cohort.
2. **AVAX (2% band, corr 0.84):** Moves with market but pause still hurts PnL — directional drift (-25%) dominates; consider **disabling pause** or looser AND thresholds.
3. **JUP (corr 0.78):** Biggest pause winner (+$227 PnL) — best fit for joint AND gate.
4. **RENDER (+$195 PnL, −$32 DD):** Strong pause benefit with AND trigger; per-ticker sweep even better (+$561).
5. **FET (+$10 PnL, flat DD):** Pause neutral — baseline already profitable; optional.
6. **Buy-side-only pause (future):** FET/RENDER end net-short; full pause blocks bounce sells.

## Regenerate

```bash
python3 binancefetch/gridbot_study/grid_vol_pause_study.py
```
