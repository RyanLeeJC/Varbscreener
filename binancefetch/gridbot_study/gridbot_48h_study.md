# Gridbot 48h study — lighter defi/l1/l2 top-half cohort

Window: **2026-06-06 16:40 → 2026-06-08 16:40 SGT** (last 48h of Binance USDT-M 5m bars)

## Methodology

### 1. Ticker universe

Start from `Vari Listings/vari_crypto_categories.json`:

- Union **lighter-defi**, **lighter-l1**, **lighter-l2** (75 tickers)
- Scrub **vari-blacklist** + **BTC** + **ETH**
- Rank survivors by Vari **volume_24h** (`GET /api/metadata/supported_assets`)
- Take **top half by volume** (32 tickers)

### 2. Data fetch

- Script: `fetch_32tickers_48h5m.py`
- Source: Binance USDT-M perpetuals, **5m** klines, rolling **48h** window
- Output: `32_tickers_48h5m.sqlite` (+ BTC/ETH for market gate)
- Tickers not listed on Binance are skipped (e.g. **LIGHTER**)

### 3. Hyperparam backtest

- Script: `gridbot_48h_hyperparam.py`
- Sim engine: `grid_vol_pause_backtest.py` (production vol-pause, no baseline)
- **Bands tested per ticker:** 1.5%, 2.0%, 2.5%, 3.0%, 3.5%
- Pick **best band** = highest PnL per ticker over the 48h window
- Grid sim: 8 rungs, $25/rung, 33× leverage, paired limit re-arm
- Vol-pause: AND gate (BTC/ETH 1h ±2%, ticker 30m band×1.6, 5m bar ±1.2%, vol_ratio ≥ 1.3, min hold 60 cycles)

### 4. Post-filter (this table)

From all simmed tickers, keep only those with:

- **bps ≥ −50**, where `bps = PnL / grid traded vol × 10,000`
- **TRX** manually scrubbed

**14 tickers removed** for bps &lt; −50: HYPE, HBAR, SUI, FIL, SOL, VIRTUAL, PENDLE, LINK, TIA, ONDO, CRV, NEAR, JTO, MEGA.

Full per-band results: `gridbot_48h_hyperparam.json`.

---

## Final cohort (16 tickers)

| # | Ticker | Band% | PnL | Grid Vol | bps |
|---|--------|-------|-----|----------|-----|
| 1 | ENA | 3.0 | +$50.72 | $6,122 | 82.9 |
| 2 | XLM | 2.0 | +$38.58 | $8,974 | 43.0 |
| 3 | BCH | 2.5 | +$34.87 | $2,075 | 168.0 |
| 4 | AVAX | 2.5 | +$21.31 | $4,054 | 52.6 |
| 5 | LTC | 1.5 | +$19.36 | $6,987 | 27.7 |
| 6 | SYRUP | 1.5 | +$15.44 | $8,527 | 18.1 |
| 7 | OP | 3.0 | +$13.20 | $4,786 | 27.6 |
| 8 | ASTER | 3.0 | +$11.34 | $2,399 | 47.3 |
| 9 | ADA | 3.5 | +$3.41 | $2,307 | 14.8 |
| 10 | MON | 3.5 | +$0.71 | $1,984 | 3.6 |
| 11 | ICP | 3.5 | −$1.48 | $2,730 | −5.4 |
| 12 | DOT | 3.5 | −$2.86 | $1,790 | −16.0 |
| 13 | AAVE | 2.5 | −$6.37 | $2,380 | −26.8 |
| 14 | VVV | 3.5 | −$6.49 | $5,512 | −11.8 |
| 15 | BNB | 3.5 | −$6.95 | $1,803 | −38.6 |
| 16 | AERO | 1.5 | −$15.54 | $3,192 | −48.7 |

**Cohort total:** $65,622 grid vol | +$169.25 PnL | **+25.8 bps**

Skipped (no Binance perp): **LIGHTER**

## Live `GRID_TRADING_TICKERS` snippet

```python
GRID_TRADING_TICKERS = {
    "ENA": 3.0,
    "XLM": 2.0,
    "BCH": 2.5,
    "AVAX": 2.5,
    "LTC": 1.5,
    "SYRUP": 1.5,
    "OP": 3.0,
    "ASTER": 3.0,
    "ADA": 3.5,
    "MON": 3.5,
    "ICP": 3.5,
    "DOT": 3.5,
    "AAVE": 2.5,
    "VVV": 3.5,
    "BNB": 3.5,
    "AERO": 1.5,
}
```

---

## Regenerate from scratch

From repo root:

```bash
# 1) Fetch last 48h Binance 5m → 32_tickers_48h5m.sqlite
python3 binancefetch/gridbot_study/fetch_32tickers_48h5m.py

# 2) Band hyperparam (vol-pause only) → gridbot_48h_hyperparam.json
python3 binancefetch/gridbot_study/gridbot_48h_hyperparam.py --json
```

Then apply post-filters on results:

1. Per ticker: `bps = best_pnl / grid_traded_vol × 10,000` (vol from sim `volume_usd` at best band)
2. Drop tickers with **bps &lt; −50**
3. Drop **TRX**

To refresh the **32-ticker cohort** itself (before fetch), re-run volume ranking from `vari_crypto_categories.json` (lighter-defi ∪ lighter-l1 ∪ lighter-l2, minus blacklist/BTC/ETH, top half by Vari `volume_24h`).
