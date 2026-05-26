# ETH/BTC 5m chg% check — conversation log

---

## User

@Varibot/HighOIbacktest/ETHBTC15-25May_5m.sqlite 

how many number of timestamps are there

---

## Assistant

Checking the SQLite schema and counting timestamps.

There are **2,668** timestamps in `ETHBTC15-25May_5m.sqlite`.

They live in the `bars_5m` table (`time` is the primary key, so one row per timestamp). Range:

- **First:** `2026-05-15T10:35:00+08:00`
- **Last:** `2026-05-24T17:25:00+08:00`

That matches the schema note: 5m bars inner-joined across BTC/USDT, ETH/USDT, and ETH/BTC, trimmed to timestamps present in all three sources.

---

## User

query for me,
What is the number of occurrences, 
where eth 5m chg% - btc 5m chg% >= 0.10%
eth 5m chg% - btc 5m chg% <= -0.10%
Combined sum of occurrences where eth exceeds btc by 0.10%

where eth 5m chg% - btc 5m chg% >= 0.05%
eth 5m chg% - btc 5m chg% <= -0.05%
Combined sum of occurrences where eth exceeds btc by 0.05%

---

## Assistant

Here are the counts from `bars_5m` (`eth_chg_pct - btc_chg_pct`, **2,668** bars total):

### ≥ 0.10% spread (ETH − BTC)

| Condition | Count | % of bars |
|-----------|------:|----------:|
| `eth_chg_pct - btc_chg_pct >= 0.10` | **71** | 2.66% |
| `eth_chg_pct - btc_chg_pct <= -0.10` | **80** | 3.00% |
| **Combined** (either tail) | **151** | 5.66% |

### ≥ 0.05% spread (ETH − BTC)

| Condition | Count | % of bars |
|-----------|------:|----------:|
| `eth_chg_pct - btc_chg_pct >= 0.05` | **343** | 12.86% |
| `eth_chg_pct - btc_chg_pct <= -0.05` | **329** | 12.33% |
| **Combined** (either tail) | **672** | 25.19% |

“Combined” is the sum of the two one-sided counts (ETH ahead by at least the threshold **or** BTC ahead by at least the threshold). The tails don’t overlap, so combined = 71 + 80 = 151 and 343 + 329 = 672.

If you only want bars where **ETH beats BTC** (positive spread only), use **71** at 0.10% and **343** at 0.05%.

---

## User

git commit push branch

---

## Assistant

Committed and pushed on branch **`HighOI`**.

**Commit:** `db12ad9` — *Layer Monk BTC/ETH pair rules on HighOI live strategy and backtests.*

**Included (15 files):**
- `strategy/HighOIstrat.py` — Monk-style entry/exit, Sim A half-size defaults
- `Varibot/HighOI.md` — Monk strategy notes + live bot mapping
- `Varibot/tests/test_highoi_venue_flat.py` — $20k/leg expectations
- Backtest DB (`ETHBTC15-25May_5m.sqlite`), schema, bars export, sim HTMLs, optimizer scripts

**Pushed:** `origin/HighOI` (`ead8a58` → `db12ad9`)

**Left untracked** (not part of this HighOI work):
- `Varibot/gridlimits.json`
- `strategy/strategy_output.txt`

Say if you want those added in a follow-up commit.

---

## User

save this convo verbatim into ethbtc5mchgpctcheck.md

---

## Assistant

Saved to [Varibot/HighOIbacktest/ethbtc5mchgpctcheck.md](Varibot/HighOIbacktest/ethbtc5mchgpctcheck.md).

---
