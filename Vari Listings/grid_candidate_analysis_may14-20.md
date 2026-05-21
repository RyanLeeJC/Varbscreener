# Grid candidate analysis — May 14–20, 2026

Generated from `vari_railway_db/vari_railway_db.sqlite` (15m `mark_price` snapshots).  
Simulation logic matches **GridBot branch** paired re-arm grid (`scripts/grid_rearm_hyperparam.py` / `grid_rearm_sim.html`).

---

## Data window

| Item | Value |
|------|--------|
| **Period** | `2026-05-14T00:00:00Z` → `2026-05-20T23:59:59Z` (UTC, inclusive) |
| **Bars per ticker** | 660 (15m cadence) |
| **Universe** | Top **40 by FDV** at last snapshot in window (`2026-05-20T23:45:00Z`, run_id 1324) |
| **Price field** | `listings.mark_price` joined to `runs.fetched_at_utc` |

---

## 1. Range-bound candidate filter (SUI-style)

Matches visually boxed consolidation (e.g. SUI 1h chart): price spends most of the week inside a **tight horizontal channel**.

### Rule

| Step | Definition |
|------|------------|
| **Bar** | Each 15m snapshot |
| **24h channel** | Over prior **96 bars** (24h): `(high − low) / midpoint` |
| **In-box bar** | 24h channel width ≤ **7%** |
| **Pass** | In-box on ≥ **70%** of eval bars (564 bars after 24h warmup) |

### Calibration

- **SUI:** 80.1% in-box, median 24h range 5.1% — passes (user reference chart).
- **Failed (<70% in-box):** LAB, STABLE, HYPE, H, ZEC, ONDO, KITE, TON, MON, CC.

### Top 10 alts (ranked for grid suitability)

Score: `in_box_pct − 0.25 × med_24h_range` (prefer high in-box % and tighter channels).  
Excludes BTC/ETH from alt list; both majors still pass in-box filter.

| # | Ticker | In-box % | Med 24h range |
|---|--------|----------|---------------|
| 1 | TRX | 100.0% | 1.2% |
| 2 | BNB | 100.0% | 2.3% |
| 3 | LTC | 100.0% | 3.0% |
| 4 | HBAR | 100.0% | 3.0% |
| 5 | SOL | 100.0% | 3.4% |
| 6 | AVAX | 100.0% | 3.5% |
| 7 | XMR | 100.0% | 3.6% |
| 8 | MNT | 99.6% | 3.1% |
| 9 | UNI | 100.0% | 5.7% |
| 10 | SUI | 80.1% | 5.1% |

### Honorable mentions (70%+ in-box, ~4–6% med range)

LINK (97.9%), FIL (93.8%), DOT (90.4%), DOGE (89.4%), CRO (88.3%), TRUMP (84.9%), NEAR (77.7%), TAO (75.2%), M (74.6%), WLD (72.3%), WLFI (72.0%).

---

## 2. Hyperparameter testing (max volume churn)

### Simulator settings (GridBot default)

| Parameter | Value |
|-----------|--------|
| `investment_usd` | 1,000 |
| `leverage` | 10× |
| `grid_reset` | ON |
| **Policy** | Paired re-arm after each fill |
| **Sweep** | `band_pct` 0.5%–10.0% (step 0.1), `grid_num` 10 / 20 / 30 / 40 |
| **Objective** | Maximize **volume_usd** (sum of `qty × fill_price` per fill) |

### Practical band window

Avoid unrealistic tight bands that maximize sim churn but cause excessive resets:

```
band_lo = max(2.0%, 0.45 × med_24h_range)
band_hi = min(10.0%, p75_24h_range + 0.5%)
```

Pick `(grid_num, band_pct)` with highest volume inside `[band_lo, band_hi]`.

### Recommended settings (practical max churn, May 14–20)

| Ticker | In-box % | Med 24h | **Rec. `GRID_BAND_PCT` (±)** | **Grid #** | Sim volume | Resets |
|--------|----------|---------|------------------------------|------------|------------|--------|
| TRX | 100% | 1.2% | 2.0% | 40 | $65,166 | 1 |
| BNB | 100% | 2.3% | 2.0% | 40 | $146,941 | 4 |
| LTC | 100% | 3.0% | 2.0% | 40 | $168,793 | 7 |
| HBAR | 100% | 3.0% | 2.0% | 40 | $197,991 | 6 |
| SOL | 100% | 3.4% | 2.0% | 40 | $187,516 | 7 |
| AVAX | 100% | 3.5% | 2.0% | 40 | $197,716 | 6 |
| XMR | 100% | 3.6% | 2.0% | 40 | $306,842 | 8 |
| MNT | 99.6% | 3.1% | 2.0% | 40 | $183,380 | 11 |
| UNI | 100% | 5.7% | 2.6% | 40 | $242,932 | 6 |
| SUI | 80.1% | 5.1% | **2.3%** | 40 | $260,868 | 11 |

### SUI band sensitivity (`grid_num=20`)

| Band ± | Sim volume | Resets |
|--------|------------|--------|
| 0.5% | $956,572 | 141 |
| **2.3%** (recommended) | **$260,868** | **11** |
| 2.6% | $180,097 | 9 |

**Note:** Raw max churn at ±0.5% / 60 grids is not recommended live — extreme reset count and poor economics on several names.

---

## 3. Fixed-scenario backtest: ±1.5% band, 10 grids

**Request:** Run each of the top 10 alts with **±1.5% band** (`GRID_BAND_PCT=1.5`), **`grid_num=10`**, same period and sim stack.

| Setting | Value |
|---------|--------|
| **Band** | ±1.5% above / below anchor (period open `mark_price`) |
| **Grids** | 10 |
| **Capital per ticker** | $1,000 × 10× = **$10,000** notional each |
| **Grid reset** | ON |

### Results

| Ticker | Fills | Grid resets | **Volume (USD)** | Sim total PnL |
|--------|------:|------------:|-----------------:|---------------:|
| TRX | 45 | 1 | $45,633 | $29 |
| BNB | 128 | 5 | $125,948 | $324 |
| LTC | 140 | 9 | $137,630 | $122 |
| HBAR | 178 | 11 | $174,532 | $220 |
| SOL | 171 | 12 | $164,394 | $391 |
| AVAX | 184 | 11 | $178,384 | $422 |
| XMR | 284 | 15 | $279,398 | $535 |
| MNT | 153 | 17 | $149,124 | $410 |
| UNI | 309 | 30 | $303,312 | $409 |
| SUI | 324 | 20 | $294,180 | $897 |
| **TOTAL** | **2,016** | **131** | **$1,852,535** | **$3,760** |

**Total sim volume (all 10 tickers): $1,852,535**

Per-ticker volumes are independent sums (each ticker simulated with its own $10k stack). Scale volume ~linearly with investment × leverage if live sizing differs.

Highest churn at ±1.5% / 10 grids: **SUI, UNI, XMR**. Lowest: **TRX** (tight range, few crosses).

---

## 4. ETH / BTC note (same week)

| | Net May 14–20 | In-box % | Comment |
|--|---------------|----------|---------|
| BTC | −2.4% | 100% | Good chop; fine for grid with ~±2% band |
| ETH | −5.9% | 100% | More directional; use wider band (~±2–2.5%) |

ETH is not “bad” for grids — it was slightly more one-sided than BTC this week.

---

## 5. Reproduce

From repo root (`Vari/`):

```bash
# Pull fresh DB (requires railway login)
# See Vari Listings/vari_data_logger.md

sqlite3 "./Vari Listings/vari_railway_db/vari_railway_db.sqlite" \
  "SELECT COUNT(*), MIN(fetched_at_utc), MAX(fetched_at_utc) FROM runs;"
```

Re-run analysis with the inline Python used in the May 2026 Cursor session, or port logic into `scripts/grid_rearm_hyperparam.py` with DB price input.

### Sim assumptions (limitations)

- Fills when 15m bar **crosses** a grid level (no intra-bar path).
- No fees, slippage, partial fills, or latency.
- Anchor = first bar price in window; live bots may re-anchor on reset differently.
- PnL includes open inventory marked to final bar.

---

## References

- `Vari Listings/vari_data_logger.md` — DB pull / schema
- `Vari Listings/grid_corr.md` — corr / β vs BTC/ETH methodology
- GridBot branch: `scripts/grid_rearm_hyperparam.py`, `gridbot_new.md`
