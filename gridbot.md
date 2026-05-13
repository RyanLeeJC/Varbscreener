# GridBot

Slim branch of **Vari** focused on the **Varibot** orchestrator: auth, listing refresh, **gridstrat** (invert-extreme style) ticker selection, multimarket orders, and portfolio-manager pair logic. No backtests, correlation UI, or extra strategy modules in tree.

## Layout

| Path | Role |
|------|------|
| `Varibot/varibot.py` | Main loop: listingtable → marketstate → strategy → multimarketorder |
| `Varibot/multimarketorder.py` | Places orders (dry-run or `--live`) |
| `Varibot/closeallpositions.py` | Reduce-only closes when invoked |
| `Varibot/portfolio_manager_pairs.py` | Pair / leg TP–SL style management helpers |
| `Varibot/variationalbot/` | Config, Vari HTTP client, execution helpers |
| `Vari Listings/listingtable.py` | Fetches listings + CoinGecko fields → `listingtabledata.json` |
| `Vari Listings/marketstate.py` | Reads listing JSON → `marketstate.json` (BTC/ETH regime) |
| `strategy/gridstrat.py` | Strategy logic + `run_strategy()` loader; writes `strategy/strategy_output.txt` when run |
| `requirements.txt` | All third-party deps; `Varibot/requirements.txt` and `Vari Listings/requirements.txt` include it via `-r` |

Runtime JSON (not committed): `Vari Listings/listingtabledata.json`, `Vari Listings/marketstate.json`.

## Prerequisites

- **Python**: 3.12 recommended (`runtime.txt`, `Dockerfile`). 3.9+ often works if dependencies install.
- **API keys / env**: copy `Varibot/env.example` → `Varibot/.env` and set at least `VR_TOKEN` and `VR_WALLET_ADDRESS`. Optional: `COINGECKO_API_KEY` for CoinGecko Pro from `listingtable.py`.
- **Network**: Vari endpoints may need `HTTPS_PROXY` on some hosts (see comments in `env.example`).

## Install

From repo root:

```bash
python3 -m pip install -r requirements.txt
```

## Refresh market data (before each cycle or when flat)

From repo root:

```bash
python3 "Vari Listings/listingtable.py"
python3 "Vari Listings/marketstate.py"
```

## Run Varibot

From `Varibot/` (recommended; script adjusts `sys.path`):

```bash
cd Varibot
python3 varibot.py              # dry-run (no live orders)
python3 varibot.py --live       # live trading
python3 varibot.py --once       # single cycle then exit
python3 varibot.py --help
```

Default strategy env: `VARIBOT_STRATEGY` (default `invert_extreme.py`). On this branch, selection is implemented in **`strategy/gridstrat.py`**; keys **`invert_extreme`** and **`gridstrat`** both resolve to that module.

## Docker / Railway

- **Docker**: `docker build -t gridbot .` then run with the same env as local; image default command is `python3 Varibot/varibot.py --live`.
- **Railway**: `railway.toml` points at the Dockerfile; set secrets for `VR_TOKEN`, wallet, and any proxy vars in the platform UI.

## Dependencies (pip)

Declared in **`requirements.txt`**: `curl_cffi`, `python-dotenv`, `requests`, `urllib3<2`. Subfolder requirement files re-export the root file.

## Safety

Use **`--live`** only when you intend real orders. Start with dry-run and small sizing flags (`--usd`, `--im-target-pct`, etc.) as documented in `varibot.py --help`.
