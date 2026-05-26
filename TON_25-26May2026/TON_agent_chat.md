# Agent chat transcript — TON grid / loss study

Saved from Cursor agent session (May 2026).  
Full forensic write-up: **[TON_loss_study.md](./TON_loss_study.md)**

---

## 1. Grid rung math (+20% move)

**User:** If TON went straight up 20% hitting all sell limit rungs, how many sells and what total short notional at $100/rung?

**Agent (summary):**

TON config: ±3% band, 10 rungs (`GRID_NUM`), ~0.6% spacing per rung, 5 sells above center.

| Scenario | Sell fills | Short notional @ $100/rung |
|----------|------------|----------------------------|
| Initial book only (5 resting sells) | 5 | $500 |
| Full +20% with breach re-anchor, every rung hit | ~33 | ~$3,300 |

User’s chart (~5 rungs in ~20%) implied ~4% spacing — wider than bot’s ~0.6% spacing.

---

## 2. $50 vs $100 per rung

**User:** Saved TON file shows $50/rung — thought they pushed $100/rung in git.

**Agent (summary):**

- **$50** was in local **`Varibot/gridlimits.json`** (runtime snapshot, `updated_at` 2026-05-23), **not committed** (untracked).
- **$100/rung** comes from code: `DEFAULT_GRID_INVESTMENT_USD=20`, `GRID_LEVERAGE=50`, `GRID_NUM=10` → `20 × 50 / 10 = $100` (commit `4269014` doubled investment from 10 → 20).
- Live trades in the loss period used ~**55 TON** ≈ **~$110** per leg at ~$2.

---

## 3. Why losses on TON pump/dump?

**User:** Realized losses when TON pumped ~20% and dumped. Thought gradual move wouldn’t hit $4.5k cap. Was re-arm broken?

**Agent (summary):**

**No — gradual move still hit the cap** via hundreds of sells over hours (~$4,563–$4,716 short notional at trim).

**Main causes:**

1. Short stacked during multi-hour pump (525 TON trades; 436 sells).
2. **$4,500 trim** at peak — two 50% market buys (~1,084 + ~1,112 TON); **−$153** single realized event.
3. **Pinned bounds** stuck at lower≈1.748, upper≈1.856 while mark went to **2.15+** — sim re-anchored on breach but bounds didn’t re-pin; reconcile often said “no missing limits” while ladder was wrong (`buy=0 sell=10` at $2.15).
4. Re-arm **ran** (~390 TON re-arm cycles) but refilled against **stale band**.

**PnL:** Total TON realized **−$307.56**; worst hour **17:00 UTC (−$242)**; after **18:00 UTC** mostly positive on dump.

**Not the main issue:** One-shot theoretical 33-rung / $3,300 model — live path was slow accumulation + broken pinned band + trim at top.

---

## 4. Save artifacts

**User:** Save TON response into `TON_loss_study.md` and save this agent chat.

**Files created:**

- `TON_25-26May2026/TON_loss_study.md` — full forensic report
- `TON_25-26May2026/TON_agent_chat.md` — this transcript

---

## Note on saving chats in Cursor

This markdown is a **summary transcript** of the conversation, not a binary export of the IDE chat pane. To keep the full interactive thread in Cursor, use the chat UI’s export or history features for that session.
