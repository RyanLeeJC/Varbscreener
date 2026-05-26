# TON Loss Study — 25–26 May 2026

Forensic review of grid bot behavior during the TON pump (~+22%) and dump, using:

- `BINANCE_TONUSDT_1m.csv`
- `Trades.csv`
- `Realised PNL.csv`
- `TON_12hours_25-26May2026.txt` (Varibot logs)

---

## Bottom line

Losses came mainly from **stacking a large short during a gradual pump**, then **hitting the $4,500 notional cap near the top** and being forced to **market-buy half the position at ~$2.13** (realizing **−$153** in one shot). Re-arming was active, but **pinned grid bounds stayed stuck around $1.75–$1.86 while TON traded above $2.00**, so the limit ladder and reconcile logic were out of sync with price for hours.

The gradual move **did** reach the cap — not from one 20% spike, but from **many small sells over time**.

---

## What the data says

| Metric | Value |
|--------|--------|
| TON realized PnL (period) | **−$307.56** (96 events) |
| Worst single event | **−$153.23** at 17:10 UTC (trim buy) |
| Worst hour | **17:00 UTC: −$242** |
| TON trades | 525 (436 sells / 89 buys) |
| Typical grid leg | ~55 TON ≈ **~$110** at $2 |
| Binance move (CSV) | $1.805 → high **$2.198 (+21.8%)** |

After **18:00 UTC**, realized PnL on TON turns **positive** (short working on the dump: +$0.16 up to +$3.60). Most damage was **during the pump and at the peak**, not on the way down.

---

## The $4,500 cap did trigger

From logs (`TON_12hours_25-26May2026.txt`):

```
notional cap trim[TON]: short notional=$4563.60 > $4500 — buy 50% qty=1083.91
notional cap trim[TON]: short notional=$4715.75 > $4500 — buy 50% qty=1111.66
```

Matches `Trades.csv`: a **1,111 TON market buy** at 17:10 UTC @ $2.1295 — not a normal ~55 TON grid leg.

The gradual pump built enough short exposure over hours (up to **170 consecutive sells** in the trade log), not a single vertical +20% candle.

---

## Timeline (UTC / SGT)

| Time (UTC) | Time (SGT) | Event |
|------------|------------|--------|
| ~14:46 | 22:46 | First TON sells; price ~$1.86; grid **buy=9, sell=1** |
| 14:48–17:11 | 22:48–01:11 | **12 GRID RESET** breaches (anchors 1.856 → … → 2.18) |
| 16:00–17:00 | 00:00–01:00 | Many small **−$4 to −$6** realized losses (covers during pump) |
| 17:09–17:10 | 01:09–01:10 | Cap trim ×2; **−$153** on 1,111 TON buy |
| 17:17–17:56 | 01:17–01:56 | More losses near top |
| 18:00+ | 02:00+ | PnL flips positive on dump |

Price sketch:

```
  2.20 |                    * peak ~01:31 SGT
  2.10 |               ****  | trim @ 01:09-01:10
  2.00 |          ****       |
  1.90 |     ****            |
  1.85 | ****  pinned upper bound stuck ~1.856
  1.75 |---- pinned lower ~1.748 ----
```

---

## Re-arming: active but wrong band

Counts from logs:

| Event | Count |
|-------|-------|
| `gridlimits[TON] re-arm` | 390 |
| Sim `SELL filled` | 160 |
| Sim `BUY filled` | 127 |
| `GRID RESET` | 12 |

Example at pump start (22:48 SGT): top sell @ **1.856** fills → breach → reset → re-post **5 missing limits**.

### Pinned bounds bug (main structural issue)

At **mark ≈ $2.15**, bounds stayed pinned:

```
lower=1.7481276074845737  upper=1.8562592120712484
```

While sim showed **buy=0 sell=10** at mark **2.148** — ladder logic thinks 10 sells, but all template rungs sit **below** a band capped at $1.856.

`resolve_grid_bounds` in `strategy/gridstrat.py` **pins** `lower`/`upper` so the bracket does not recentre every cycle. On a **one-way trend**, sim **re-anchors on breach** but **pinned bounds do not move**. Reconcile often reported `no missing buy/sell limits to refill` while the book was wrong for current price.

Early pump: **buy=9 sell=1** (almost all sell rungs already filled, price above ladder).  
Late pump: **buy=0 sell=10** (stale band below market).

---

## Why a gradual pump still lost money

On a mean-reverting grid, a slow rally should fill sells then buys on pullbacks. Here:

1. **Trend, not chop** — hours of grind up; 436 sells vs 89 buys (full sample).
2. **Short inventory grew** — trim window **> $4.5k short** (~2,100+ TON at ~$2.10).
3. **Trim locked in loss** — 50% market cover at peak (−$153); **sold again at 17:11** right after trim.
4. **Pinned band** — could not rebuild a symmetric ladder around $2.00+.

Log start (22:37 SGT) already showed **Port uPNL −$33** with TON at **$1.85**.

---

## Grid sizing context (from earlier in session)

| Config | Per-rung | Notes |
|--------|----------|--------|
| `gridlimits.json` snapshot | $50/rung | Stale file (`updated_at` 2026-05-23); not in git |
| `DEFAULT_GRID_INVESTMENT_USD=20`, `LEVERAGE=50`, `GRID_NUM=10` | **$100/rung** | After commit `4269014` |
| Live trades in this period | ~$110/leg | ~55.49 TON × ~$2 |

Theoretical one-shot +20% with full re-anchor: **~33 sells → ~$3,300** at $100/rung. Live session accumulated via **many hours of fills**, not one spike.

---

## Direct Q&A

| Question | Answer |
|----------|--------|
| Why losses on pump + dump? | Short built on pump; realized losses on covers during rally; **trim at peak**; dump helped after 18:00 UTC. |
| Should gradual move avoid $4.5k cap? | **No** — reached **$4,563–$4,716** over ~3–4 hours. |
| Was re-arm broken? | **Partially** — re-arm fired often; **pinned bounds + breach re-anchor** left limits in wrong zone; reconcile missed refills above $1.86. |

---

## Recommended fix (direction)

**Re-pin `lower`/`upper` on breach re-anchor** (or when mark exits pinned band for N cycles), so `gridlimits` reconcile posts rungs around **current** price.

Secondary: $4.5k cap worked as designed; **50% market buy into a live pump** is expensive — root issue is short drift while the ladder is broken.

---

## Source files

- `TON_25-26May2026/BINANCE_TONUSDT_1m.csv`
- `TON_25-26May2026/Trades.csv`
- `TON_25-26May2026/Realised PNL.csv`
- `TON_25-26May2026/TON_12hours_25-26May2026.txt`
- `strategy/gridstrat.py` — `resolve_grid_bounds`, pinned bounds
- `Varibot/grid_limits_reconcile.py` — limit reconcile / re-arm

*Generated from Cursor agent session, May 2026.*
