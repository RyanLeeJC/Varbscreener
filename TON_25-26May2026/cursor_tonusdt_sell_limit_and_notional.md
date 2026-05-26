# TONUSDT sell limit and notional calculation
_Exported on 5/26/2026 at 11:13:50 GMT+8 from Cursor (3.4.20)_

---

**User**

Based on the grid rung for TON that I have,

If TON went straight one-sided up 20%, hitting all sell limit rungs one after another,
How many sell limit counts will it hit?
If each rung is $100, how much total shorts notional will it end up with? means $100 x sell limit counts.

---

**Cursor**

I'll look up your TON grid configuration and how rung spacing maps to a 20% move.


Checking how the grid is built and whether rungs extend beyond the initial band on a large move.


Simulating a straight 20% move with your TON grid parameters to count sell fills.


Your TON grid from `gridlimits.json` and `gridstrat.py`:

## TON grid geometry

| Parameter | Value |
|-----------|--------|
| Band | ±3% (`band_pct: 3.0`) |
| Rungs | 10 (`GRID_NUM`) → **5 sells above center**, 5 buys below |
| Mark (when written) | ~$1.776 |
| Rung spacing | ~**0.60%** of price (~$0.0107) |
| Resting sell limits | 5 levels from ~$1.787 to ~$1.829 |

Your chart’s ~20% move crossing **5** horizontal lines implies ~**4%** per line — wider than this bot grid (~0.6% per rung). The numbers below use your **actual TON config**, not that wider chart spacing.

---

## If price goes straight up +20%

### A) Only the initial 5 resting sells (no re-anchor past the band)

The sell ladder only spans about **+3%** above center. A one-way +20% move fills **all 5** sells quickly; the rest of the move is above the original band.

| | |
|---|---|
| **Sell limit fills** | **5** |
| **Short notional** (@ $100/rung) | **$500** |

(`gridlimits.json` has `per_rung_usd: 50` → **$250** at saved sizing.)

---

### B) “Every rung hit one after another” with breach re-anchor (default `GRID_REARM_ON_BREACH=reanchor`)

As price steps up rung-by-rung, each ~3% band breach recenters the ladder and places new sells above the new anchor. Simulating mark → +20% in small steps:

| | |
|---|---|
| **Sell limit fills** | **33** |
| **Grid resets** | 6 |
| **Short notional** (@ $100/rung) | **$3,300** |

That matches geometry: 20% ÷ ~0.60% spacing ≈ **33** sell rungs crossed from center.

---

## Chart vs bot

| Source | Rungs in ~20% | Sells @ $100/rung |
|--------|----------------|-------------------|
| Your chart (~4% spacing) | ~5 | ~$500 |
| TON bot config (~0.6% spacing, full path + reanchor) | **33** | **$3,300** |
| TON bot (initial book only) | **5** | **$500** |

---

## Summary

- **Initial book only:** **5** sells → **$500** shorts at $100/rung.  
- **Full one-way +20% with re-anchor and sequential rung hits:** **33** sells → **$3,300** at $100/rung.

Per-rung size: `(GRID_INVESTMENT_USD × GRID_LEVERAGE) / GRID_NUM`. For **$100/rung** with `GRID_NUM=10`, that’s **$1,000** deployed notional per ticker (e.g. $40 × 25). Your saved TON file still shows **$50/rung** until limits are regenerated.

---

**User**

Your saved TON file still shows $50/rung where?
I thought I git commit pushed a $100/ rung

---

**Cursor**

Tracing where per-rung sizing is stored and what your last commit actually changed.


You’re right to question that — the **$50/rung** figure came from a **stale local file**, not from what you pushed.

## Where the $50 was

In **`Varibot/gridlimits.json`**, TON (and every other ticker) still has:

```1093:1094:Varibot/gridlimits.json
      "per_rung_usd": 50.0,
      "per_rung_qty": "28.15",
```

Each limit row also has `"usd": 50.0`. That file was last written **`2026-05-23`** (`updated_at` at the bottom), before your sizing change.

## What you actually committed

Your push **`4269014`** (“Double default grid investment…”) only changed code defaults in `strategy/gridstrat.py`:

- `DEFAULT_GRID_INVESTMENT_USD`: **10 → 20**
- `DEFAULT_GRID_LEVERAGE`: **50** (unchanged)

Per-rung at runtime:

**20 × 50 / 10 = $100/rung**

That matches `Varibot/tests/test_portfolio_rebalance.py` (`20 × 50 / 10 = 100`).

**`Varibot/gridlimits.json` is not in git** (untracked locally). So the $100 change lives in **code/env**, not in a committed `gridlimits.json` snapshot.

## Why I said $50

I used the on-disk `gridlimits.json` from an older bot run when investment was still **$10** → **10 × 50 / 10 = $50/rung**. That file hadn’t been regenerated after your commit.

## What to expect live

After Varibot runs with the new defaults (and `sync_gridlimits_json` runs), `gridlimits.json` should show **`per_rung_usd": 100.0`** and `"usd": 100.0` on each limit. Until then, the JSON can still say 50 even though the strategy is already sizing at $100.

Sorry for the confusion — your **$100/rung** intent from the commit is correct; I should have cited `gridstrat.py` defaults, not the outdated cache file.

---

**User**

@TON_25-26May2026/BINANCE_TONUSDT_1m.csv @TON_25-26May2026/Realised PNL.csv @TON_25-26May2026/TON_12hours_25-26May2026.txt @TON_25-26May2026/Trades.csv 

I realised quite a bit of losses on TON, when it pumped 20% and dumped. Can you look into the history, and figure out why?
TON pumped gradually and dumped gradually, by your theory, I shouldn't have hit the $4,500 short notional limit to trigger the position reduction.

Was the bot not able to handle the re-arming of the buy sell limits?

---

**Cursor**

I'll read your TON trade logs, PnL, and price data and correlate them with how the grid re-arms limits during that pump-and-dump.


Here is what your files show for the TON pump/dump on 25–26 May.

## Bottom line

You lost money mainly because the bot **stacked a large short during a gradual pump**, then **hit the $4,500 notional cap near the top** and was forced to **market-buy half the position at ~$2.13** (realizing **−$153** in one shot). Re-arming was active, but **pinned grid bounds stayed stuck around $1.75–$1.86 while TON traded above $2.00**, so the limit ladder and reconcile logic were out of sync with price for hours.

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
| Binance move (your CSV) | $1.805 → high **$2.198 (+21.8%)** |

After **18:00 UTC**, realized PnL on TON turns **positive** (short working on the dump: +$0.16 up to +$3.60). Most damage was **during the pump and at the peak**, not on the way down.

---

## You *did* hit the $4,500 cap

From `TON_12hours_25-26May2026.txt`:

```
notional cap trim[TON]: short notional=$4563.60 > $4500 — buy 50% qty=1083.91
notional cap trim[TON]: short notional=$4715.75 > $4500 — buy 50% qty=1111.66
```

That matches `Trades.csv`: a **1,111 TON market buy** at 17:10 UTC @ $2.1295 — not a normal ~55 TON grid leg.

So the gradual pump **did** build enough short exposure to trigger trim. My earlier “33 rungs in one straight +20%” scenario was a **theoretical full-ladder walk**; your live session was **hours of repeated sells** (up to **170 consecutive sells** in the trade log), which is how you got to **~$4.5k+ short notional** without a single vertical candle.

---

## Timeline (UTC / SGT)

```mermaid
price
  2.20 |                    * peak ~01:31 SGT
  2.10 |               ****  | trim $4563→$4715 @ 01:09-01:10
  2.00 |          ****       |
  1.90 |     ****            |
  1.85 | ****  pinned upper bound stuck here
  1.75 |---- pinned lower ----
       +-----------------------------------> time
         22:37        23:59    01:09   dump
         log start    resets   TRIM
```

1. **~14:46 UTC (22:46 SGT)** – First TON sells; price ~$1.86. Grid already **buy=9, sell=1** (almost all upside rungs filled).
2. **14:48–01:11 SGT** – **12 GRID RESET** breaches as price steps up (anchors move to 1.856 → 1.91 → 1.96 → 2.02 → 2.07 → 2.13 → 2.18…).
3. **~16:00–17:00 UTC** – Many small **−$4 to −$6** realized losses (grid buys closing shorts **above** earlier sell prices during the pump).
4. **17:09–17:10 UTC (01:09–01:10 SGT)** – Cap trim fires twice; **−$153** on the 1,111 TON buy.
5. **17:17–17:56 UTC** – More losses as price wobbles near the top; then PnL flips positive on the dump.

---

## Re-arming: working, but fighting the wrong band

Re-arm was **not** idle:

- **390** `gridlimits[TON] re-arm` cycles  
- **160** sim `SELL filled` + **127** `BUY filled` events  
- Frequent `posting N missing limit(s)` after breaches  

Example at pump start (22:48 SGT):

- Top sell @ **1.856** fills → breach → reset → re-post **5 missing limits**

The failure mode is **pinned bounds**:

```14520:14521:TON_25-26May2026/TON_12hours_25-26May2026.txt
step: gridstrat[TON] (flat) rungs buy=0 sell=10 mark=2.147687638978418 band=±3.0%
step: gridstrat[TON] bounds ±3.0% (pinned) lower=1.7481276074845737 upper=1.8562592120712484
```

While **mark ≈ $2.15**, bounds stayed pinned at **upper = $1.856** for the whole log window (hundreds of cycles). That band was set when TON was ~$1.85 and **never re-pinned** as price trended up.

`resolve_grid_bounds` in `gridstrat.py` intentionally **pins** `lower`/`upper` so the bracket does not recentre every minute — but on a **one-way trend**, the sim **re-anchors** on breach while **bounds stay behind**. Then:

- Reconcile often reports: `no missing buy/sell limits to refill`  
- Sim shows absurd skew: **buy=0 sell=10** at $2.15 (all “sell” rungs logically below market inside a band capped at $1.856)  
- Or early pump: **buy=9 sell=1** (no sells left above price to lean against the rally)

So re-arm **ran**, but it was re-arming against a **stale template**, not a ladder centered on current price.

---

## Why a gradual pump still hurt

On a **mean-reverting grid**, a slow rally should:

- Fill sells → add small shorts  
- Fill buys on pullbacks → take profit  

What happened here:

1. **Trend, not chop** – Price grinded up for hours with limited buy-side relief (436 sells vs 89 buys in the full sample).
2. **Short inventory grew** – By the trim window you were **> $4.5k short** (~2,100+ TON notional at ~$2.10).
3. **Trim locked in loss** – Forced **50% cover at the top** (−$153) while price was still rising; you even **sold again at 17:11** right after the trim.
4. **Pinned band** – Could not rebuild a proper symmetric ladder around $2.00+; reconcile thought the book was fine while sim/venue were misaligned.

The **−$33 uPNL** at log start (22:37 SGT) with TON already at **$1.85** shows you were already underwater before the worst of the move in the log file.

---

## Direct answers to your questions

| Question | Answer |
|----------|--------|
| Why losses on pump + dump? | **Short built on pump**; **realized losses on covers** during rally; **large trim at peak**; dump actually **helped** after 18:00 UTC. |
| Should gradual move avoid $4.5k cap? | **No** — slow accumulation over ~3–4 hours still reached **$4,563–$4,716** short notional. |
| Was re-arm broken? | **Partially** — re-arm fired often, but **pinned bounds + breach re-anchor** left limits in the **wrong price zone**; reconcile didn’t refill what the sim needed above $1.86. |

---

## Most likely root cause (for a fix)

**Pinned `lower`/`upper` should update on breach re-anchor** (or when mark exits the band for N cycles), so `gridlimits` reconcile posts rungs around **current** price, not a band from when TON was $1.85.

Secondary: cap trim at **$4.5k** is doing its job, but **50% market buy into a live pump** is expensive; the bigger issue is letting short notional drift that high while the ladder is broken.

I can propose a concrete code change (e.g. re-pin bounds on `GRID RESET`, or unpin when `mark > pinned_upper`) if you want to patch this next.

---

**User**

save your response about TON, into a TON_loss_study.md
And save this agent chat?

---

**Cursor**

Saving the TON loss analysis and a structured transcript of this chat to `TON_25-26May2026/`.


Saved both files under your TON data folder:

| File | Contents |
|------|----------|
| [`TON_25-26May2026/TON_loss_study.md`](TON_25-26May2026/TON_loss_study.md) | Full loss forensic report (cap trim, pinned bounds, re-arm, timeline, PnL, fix direction) |
| [`TON_25-26May2026/TON_agent_chat.md`](TON_25-26May2026/TON_agent_chat.md) | Structured transcript of this chat (grid math, $50 vs $100/rung, loss analysis, file list) |

**About saving the agent chat:** Cursor doesn’t give me a way to export the raw chat UI thread as a file. `TON_agent_chat.md` is a written record of what we covered here. To keep the full interactive thread in Cursor, use the chat panel’s history/export for that session if you need the verbatim UI log.

Want these committed to git as well?

