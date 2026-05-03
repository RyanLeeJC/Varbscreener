# Branch plan (feature/bot-function-redesign)

prompts for live/ dry test

Dry-run (no real orders):
cd /Users/ryanlee/Documents/Dev/Vari/Varibot && python3 varibot.py

Live trading:
cd /Users/ryanlee/Documents/Dev/Vari/Varibot && python3 varibot.py --live

One cycle then exit (handy for testing):
cd /Users/ryanlee/Documents/Dev/Vari/Varibot && python3 varibot.py --once

## Goal

In this branch, the bot becomes **more involved and active in closing pairs of tickers** during the interval portfolio check (aligned to `CHECK_INTERVAL_MIN`), instead of relying on a scheduled session close-all at `STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN` (and instead of only using a whole-portfolio PnL trigger to flatten everything).

## Strategy for this branch build: `near_median`

- **Strategy module**: [`strategy/near_median.py`](strategy/near_median.py)
- **Ticker selection**: **20 tickers** chosen “near the median” (see `DEFAULT_MAX_TICKER_ENTRIES = 20`).
- **Direction split**: **Long outperformers**, **short underperformers** (as described in the strategy’s thesis).
- **Entry behavior**: the bot still starts by opening \(X\) tickers according to the selected strategy output (then routes into the portfolio manager loop).

## Remove session close-all trigger (branch delta)

Current `main`-style / current code behavior (today) for `near_median` in [`Varibot/varibot.py`](Varibot/varibot.py):

- `near_median` is included in `STRATEGY_SESSION_CLOSEALL_KEYS`.
- `_child_main()` runs a “session mode”:
  - enter immediately
  - while holding, run periodic checks every `CHECK_INTERVAL_MIN`
  - close all at the next wall multiple of `STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN`

**Branch target:** remove the scheduled close-all at `STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN` for the `near_median` build. Exits become PM-driven (pair closes) at the `CHECK_INTERVAL_MIN` cadence.

## Portfolio Manager (PM) — interval behavior

At every interval check (`CHECK_INTERVAL_MIN`), PM checks existing positions and builds a table:

**Ticker | L/S | Value | uPnL**

- **Value**: position value / notional in USD.
- **uPnL**: unrealized PnL in USD (signed).

### Pair close rule (take-profit by pairing winners)

PM searches for **pairs of opposite-side positions** (one long + one short) such that:

- both legs have **positive uPnL in USD**, and
- the **combined uPnL** exceeds a threshold percentage of their **combined trade value**.

Definitions:

- `combined_uPnL = uPnL_a + uPnL_b`
- `combined_value = Value_a + Value_b`
- Pair qualifies when:

\[
\frac{combined\_uPnL}{combined\_value} \times 100 \ge PAIR\_TP\_THRESHOLD\_PCT
\]

Selection logic (greedy, repeatable):

- Rank candidates by uPnL within each side (longs, shorts), considering only `uPnL > 0`.
- Start from the **best uPnL** ticker on one side.
- Try to match it with the **worst positive-uPnL** ticker on the opposite side that still meets the combined threshold.
  - “nth worse” means: scan the opposite-side positive-uPnL list from lowest to higher until a match is found; the first match is used.
- If a match is found:
  - close **both** tickers
  - remove both from eligibility
  - continue to the next best remaining ticker.
- Stop when no more eligible pairs can be formed.

### Computation approach

Implement the above via an **array / grid** method for speed and clarity:

- Build arrays for eligible longs and shorts: `uPnL_long[]`, `value_long[]`, `uPnL_short[]`, `value_short[]`.
- Compute a grid of `combined_uPnL` and `combined_value` (broadcasting) and a boolean mask for threshold satisfaction.
- Apply the greedy pairing rule on top of the mask to choose non-overlapping pairs.

### Order execution

When closing an eligible pair:

- close both legs (reduce-only) using the same **stepped-up slippage** retry strategy used elsewhere in the bot (increase `max_slippage` on retry if the venue rejects for slippage).

## After PM closes all eligible pairs: refill closed slots

Once all eligible pairs have been closed in this cycle:

1. Refresh listings:
   - run [`Vari Listings/listingtable_pro.py`](Vari%20Listings/listingtable_pro.py) to update [`Vari Listings/listingtabledata.json`](Vari%20Listings/listingtabledata.json).
2. Re-run the **same `near_median` strategy logic** to select replacement tickers.
3. Replacement constraints:
   - **must not** be tickers **just closed** by PM in this cycle
   - **must not** be tickers already in **existing live positions**
4. Replacement count:
   - if PM closed \(N\) pairs (i.e., \(N\) longs + \(N\) shorts), PM fills back with \(N\) new longs and \(N\) new shorts.

## Code touchpoints (where this will be wired)

- Portfolio snapshot + TP check helpers: [`Varibot/check_portfolio_stats.py`](Varibot/check_portfolio_stats.py)
- Main loop / session logic: [`Varibot/varibot.py`](Varibot/varibot.py)
- Strategy selection: [`strategy/near_median.py`](strategy/near_median.py)
- Order placement / slippage stepping patterns: [`Varibot/multimarketorder.py`](Varibot/multimarketorder.py) and existing close logic in `Varibot/varibot.py` (e.g. `funding_pairs` manager)

