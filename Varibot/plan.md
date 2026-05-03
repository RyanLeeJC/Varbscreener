## Variational 24/7 Trading Bot Plan (Workflow + Visual Map)

create a runner_cont_median.py that will 
validate vr-token, 
run listingtable.py to update the listingtabledata.json, 
filter the .json with the criteria I have

Criteria:
Calculate the median 24hChg% of top 100 coins by OI, excluding BTC & ETH.
Split 100 coins in two groups, 
(50) Median Outperformers
(50) Median Underperformers

get the tickers, then feed into multimarketorder.py
to long the outperformers, and short the underperformers. $5 each ticker, slippage cap 0.1%.
Then check positions.py and output in terminal.

### Goal

Run a **24/7 trading bot loop** that:

- Authenticates via **`vr-token` + wallet address**
- Polls **portfolio + positions** (and optionally orders) on a **5-minute cadence**
- Classifies market regime (**Sideways** vs **Directional**) using BTC/ETH 24h change + “Now” indicators
- Selects long/short tickers using **Filter Set A (Sideways)** or **Filter Set B (Directional)**
- Computes **total Initial Margin (IM) target** given number of tickers × per-position size
- Executes trades with **preset slippage limit** and **preset leverage**, then returns to monitoring

### Key constraints / non-negotiables

- **Safety first**: never place orders if auth fails, portfolio data is missing, or risk checks fail (e.g., IM/MM usage too high).
- **Deterministic loop**: each cycle produces a clear “decision + actions” record (even if it chooses to do nothing).
- **Idempotent behavior**: repeated cycles should not accidentally duplicate exposure (e.g., re-open same position repeatedly without intent).

### Proposed components (aligned to existing scripts)

- **Your original node list + decision tree (verbatim, for future prompting)**
  - auth vr-token & wallet address
  - check portfolio stats
  - check existing positions stats -> If no positions, proceeds to check market. If have existing positions,
  - check market (BTC 24hChg%, ETH 24hChg%, Sideways Now?, Directional Now?)
  - Sideways Now regime ->will fetch tickers based on a set A of filters, tickers to long and tickers to short.
  - Directional Now regime ->will fetch tickers based on a set B of filters, tickers to long and tickers to short.
  - Calculates the amount of IM to achieve with the amount of tickers x position size.
  - Executes the trades with the preset slippage limit and preset position leverage. Then goes to check portfolio stats and check existing positions every 5minutes.

- **Auth**
  - Validate `VR_TOKEN` + `VR_WALLET_ADDRESS` by calling an authenticated endpoint (e.g. `GET /api/positions`).
  - Reference: `validate_vr_token.py` and `lucius.md`.
- **Portfolio stats**
  - Fetch `GET /api/portfolio` and parse into normalized snapshot (portfolio value, IM/MM usage).
  - Reference: `check_portfolio_stats.py`.
- **Positions stats**
  - Fetch `GET /api/positions` and normalize (count, per-position details).
  - Reference: `positions.py`.
- **Market regime detection**
  - Inputs: BTC 24hChg%, ETH 24hChg%, SidewaysNow?, DirectionalNow?
  - Output: `SidewaysNow` or `DirectionalNow` (+ confidence if available).
- **Ticker selection**
  - Sideways regime: apply **Filter Set A** -> `tickers_to_long[]`, `tickers_to_short[]`
  - Directional regime: apply **Filter Set B** -> `tickers_to_long[]`, `tickers_to_short[]`
- **Sizing**
  - Compute total intended exposure and the **IM target**:
    - `n = len(longs) + len(shorts)`
    - `target_notional = n * position_size_usd`
    - `target_im = target_notional / leverage` (conceptual; exact API/UI definitions may differ)
  - Gate by portfolio/risk constraints (e.g., max IM usage threshold).
- **Execution**
  - Set leverage (if required per-asset) then place market orders with:
    - **preset `max_slippage`**
    - **preset leverage**
  - After execution, return to monitoring loop.

### Visual workflow map (Mermaid)

```mermaid
flowchart TB
  start([Start]) --> loadEnv[Load env: VR_TOKEN + VR_WALLET_ADDRESS]
  loadEnv --> auth[Auth: validate vr-token + wallet]
  auth --> authOk{Auth valid?}
  authOk -->|yes| portfolio[Check portfolio stats]
  authOk -->|no| halt[Halt + alert/log]

  portfolio --> positions[Check existing positions stats]

  positions --> hasPos{Any open positions?}
  hasPos -->|yes| managePos[Manage existing positions\n(PnL, risk, reduce-only decisions)]
  hasPos -->|no| market[Check market regime inputs]
  managePos --> market

  market --> metrics[Compute:\nBTC 24hChg%\nETH 24hChg%\nSidewaysNow?\nDirectionalNow?]
  metrics --> regime{Regime now?}

  regime -->|SidewaysNow| filtersA[Fetch tickers using Filter Set A]
  regime -->|DirectionalNow| filtersB[Fetch tickers using Filter Set B]

  filtersA --> lsA[Tickers to LONG + SHORT]
  filtersB --> lsB[Tickers to LONG + SHORT]
  lsA --> sizing[Compute IM target\n(n tickers x position size)]
  lsB --> sizing

  sizing --> riskGate{Risk checks pass?\n(IM/MM usage, limits)}
  riskGate -->|no| skip[Skip trades; log decision]
  riskGate -->|yes| exec[Execute trades\n(set leverage, place market orders)\n(max slippage)]

  exec --> post[Post-trade: refresh portfolio + positions]
  skip --> wait[Wait 5 minutes]
  post --> wait
  wait --> portfolio
```

### Decision tree details (what each box decides)

- **Auth**
  - If token is rejected/expired (401) or Cloudflare HTML is returned, stop trading actions and keep retrying safely (or halt with alert).
- **Has positions?**
  - If **no positions**: proceed to market regime detection + ticker selection.
  - If **has positions**: still do regime + signals, but include logic to avoid duplicating exposure and to optionally prioritize management (reduce-only, de-risking).
- **Regime now?**
  - Sideways -> Filter Set A
  - Directional -> Filter Set B
- **Risk checks**
  - Guardrails based on portfolio snapshot (IM/MM usage thresholds, max #positions, max notional, cooldowns).
- **Execute**
  - Apply leverage + slippage constraints, place orders for tickers-to-long/short.
  - After placing orders, re-poll stats to confirm.

### Inputs you’ll need to define (config)

- **Filter Set A**: exact criteria for sideways regime (liquidity, spread, volume, funding, vol, etc.)
- **Filter Set B**: exact criteria for directional regime
- **position_size_usd**: per-position notional size
- **leverage**: target leverage per market/asset
- **max_slippage**: per-trade max slippage
- **risk limits**: max IM usage %, max MM usage %, max total positions, cooldowns
- **poll interval**: default 300s (5 minutes)

### Output artifacts (recommended)

- Per-cycle JSON log:
  - timestamp, auth status, regime, selected tickers, sizing summary, risk gate result, orders attempted/placed
- Optional persistent state:
  - last cycle timestamp, last signals, last execution summary

### Regime while in positions (design intent)

**Cadence:** Use a poll interval of **at least 5 minutes** (do not run faster than 5 minutes between cycles).

**Two ways to fill `marketstate.json`:**

- **Flat (no open positions):** Keep the usual pipeline: run `listingtable.py`, then `marketstate.py` that derives BTC/ETH stats from **`listingtabledata.json`** (same listing snapshot used for median/ticker selection). No separate CoinGecko pass required for regime in that path beyond what listing enrichment already did.

- **Live positions:** On **every** interval while positions are open, invoke **`marketstate.py` in a mode that fetches BTC/ETH from CoinGecko directly** (not by reading mapped rows from `listingtabledata.json`), and write **`Vari Listings/marketstate.json`**.

**Why:** While exposed, you want a **fresh regime read** that is independent of the last full listing refresh, so you can see whether **Sideways vs Directional** has **changed** since the regime snapshot at/around entry (or since the last flat-path cycle).

**Implementation status:** Not wired in `varibot.py` yet; today `marketstate.py` is listing-only. Follow-up: add a CoinGecko-direct mode to `marketstate.py` and call it from the **has-positions** branch each cycle.

**Time-in-position caveat:** Each `marketstate.json` write currently sets **`fetched_at` / `fetched_at_unix`**. If you refresh `marketstate.json` every interval while holding, that may **interfere** with **`--time-exit-source marketstate`** (the anchor time for “how long have I been in this trade”). Before implementing, decide one of: keep a **separate** timestamp for “live regime poll” vs “anchor at orders/entry”; use **`orders` / `latch` / `auto`** for time-in-position while holding; or **compare** regime without overwriting the anchor field.

### Single market order live command (PowerShell)

```powershell
python singlemarketorder.py --asset SOL --side buy --usd 100 --max-slippage 0.001 --live
```

### Single market order (PowerShell, optional JSON output)

```powershell
python singlemarketorder.py --asset SOL --side buy --usd 100 --max-slippage 0.001 --live --print-json
```

### Multi market order live command (bash)

```bash
cd /Users/ryanlee/Documents/Dev/Vari/Varibot && python3 multimarketorder.py --long BTC,ETH --usd 100 --max-slippage 0.001 --live
```

### Close all positions live command (bash)

```bash
cd /Users/ryanlee/Documents/Dev/Vari/Varibot && python3 closeallpositions.py --slippage-percent 0.001 --live
```

---
Running varibot.py, revert_near_median.py with 5min cadence live for testing.
---
python3 Varibot/varibot.py --live --period-min 5 --no-align
--period-min 5: 5‑minute cadence
--no-align: sleeps a fixed 5 minutes between cycles (instead of waiting for the next wall-clock boundary)
If you ARE using --strategy revert_near_median
Right now that strategy is hard-wired into “hourly session” mode in _child_main().

The easiest wiring change is to replace the period_minutes=60 in that block with period_minutes=5 (or better, use args.period_min there). If you tell me which behavior you want (keep “enter then close every interval” vs just “run cycle every interval”), I can patch it in 1–2 lines.

How strategy/revert_near_median.py picks longs/shorts
It does this, in order:

Build a ranked universe
Loads listingtabledata.json
Parses each row into a ListingRow with:
ticker, market_cap, chg_24h_pct, plus optional vol_24h, oi, oi_skew
Ranks by DEFAULT_RANK_BY which is "market_cap".
Take top 20 by market cap
Uses DEFAULT_TOP_SPEC = "20" (or top_n if varibot passes one).
So the starting universe is Top 20 by market cap (within the listing payload).
Exclude tickers
Excludes DEFAULT_EXCLUDE_CSV = "BTC,ETH"
Excludes everything in TICKER_BLACKLIST (your extended list: PI/NIGHT/SKY/… plus others)
Pick “near-median” tickers
Computes the median of chg_24h_pct across the filtered universe.
Sorts by absolute distance to that median (closest first).
Takes DEFAULT_MAX_TICKER_ENTRIES = 10 tickers.
That’s _near_median_subset(...).

Mean-reversion split into longs/shorts
Within those 10 tickers:
sort by chg_24h_pct ascending
long the bottom 5 (underperformers)
short the top 5 (outperformers)
That’s _revert_split_by_24h_change(...).

Net effect: “low-volatility around the median movers” + “mean reversion (long slightly weak, short slightly strong)” within a Top-20 market cap, BTC/ETH excluded universe.

Yes. That “TP/PnL check” is just a point in Varibot/varibot.py where it’s about to sleep and then call one_cycle() again. You can wire a Telegram (or n8n webhook) notification in two common ways:

Option 1 (no code change): ship Railway logs to n8n and trigger on the line
Railway: keep running python3 Varibot/varibot.py --live as-is.
n8n: ingest logs (or a log drain) and trigger when a log line matches:
session: next TP/PnL check in
Then n8n sends Telegram.
This is “tap the logs”, not “bot actively sends”.

Option 2 (code change): call an n8n webhook right when the check is scheduled (recommended)
At the exact place it logs:

session: next TP/PnL check in 15m0s

add a small HTTP POST to your n8n webhook (or Telegram) with a JSON payload, e.g.:

event: "tp_check_scheduled"
next_check_in_s: ...
ts: ...
optionally include the latest portfolio snapshot fields (uPnL, pos notional, TP verdict) right after the one_cycle() call.
Wiring
n8n: create a Webhook Trigger node (POST), then Telegram node.
Railway: add env var like N8N_WEBHOOK_URL to your service.
What you’d send
On “scheduled”: “Next TP/PnL check in 15m”
On “check ran”: include the numbers (PV/uPnL/IM/MM/TP)
Practical recommendation
If your goal is “portfolio numbers periodic update”, don’t trigger on the scheduled message—trigger after the check runs, because you’ll have fresh numbers to send.

If you want, paste the snippet of the session loop around where it logs “next TP/PnL check in …”, and I’ll point to the exact line to insert the webhook call and what payload shape to use (still in Ask mode, so I’ll describe it precisely rather than editing).