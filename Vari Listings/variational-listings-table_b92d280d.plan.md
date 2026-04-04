---
name: variational-listings-table
overview: Create a small tool to fetch Variational Omni market listings, enrich them with CoinGecko market cap and price change percentages, and maintain a table of metrics per ticker.
todos:
  - id: scaffold-script
    content: Create Python script file fetch_variational_listings.py with basic structure and configuration constants.
    status: pending
  - id: implement-vari-fetch
    content: Implement function to fetch and parse Variational Omni /metadata/stats listings.
    status: pending
  - id: implement-coingecko-fetch
    content: Implement function to fetch a universe of CoinGecko markets with market cap and price change percentages.
    status: pending
  - id: join-and-output
    content: Implement symbol-index join logic, build final table, and write CSV/JSON outputs.
    status: pending
  - id: cli-main
    content: Add a simple CLI main() to run the whole pipeline and print a short summary.
    status: pending
isProject: false
---

# Variational + CoinGecko Listings Tool

## Goal

Build a small, reproducible tool that:

- Pulls the full list of Variational Omni markets from `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats`.
- For each Variational ticker, looks up the corresponding asset on CoinGecko and fetches market cap and price-change percentages (1h, 24h, 7d).
- Produces and maintains a table with columns: `vari_ticker`, `coingecko_symbol`, `coingecko_id`, `market_cap`, `price_change_1h_pct`, `price_change_24h_pct`, `price_change_7d_pct`.
- Writes the table to a simple artifact (CSV and/or JSON) that you can re-generate on demand.

## Stack & Structure

- Use Python with `requests` for HTTP and `pandas` for tabular manipulation (if not installed, we will add it when executing the plan).
- Keep everything in a single script file initially, e.g. `[fetch_variational_listings.py](fetch_variational_listings.py)`.

## API Details & Mapping Strategy

### Variational Omni API

- Base: `https://omni-client-api.prod.ap-northeast-1.variational.io`.
- Endpoint: `GET /metadata/stats` (no auth, read-only).
- Response:
  - Top-level fields: `total_volume_24h`, `tvl`, `open_interest`, `num_markets`, `loss_refund`, etc.
  - `listings`: array of per-market objects, each with at least:
    - `ticker` (e.g., `BTC`, `ETH`)
    - `name`
    - `mark_price`, `volume_24h`, open interest, etc.
- Plan: parse JSON, extract `listings`, and keep at minimum `ticker` and `name` for mapping.

### CoinGecko API (Pro, but same structure as public for this endpoint)

- Endpoint: `GET /coins/markets`.
- Base used in docs: `https://pro-api.coingecko.com/api/v3`, but a free/public base is typically `https://api.coingecko.com/api/v3`.
- Key params we will use:
  - `vs_currency=usd`.
  - One of `ids`, `names`, or `symbols`. We will start with `symbols` because Variational tickers are mostly symbols.
  - `price_change_percentage=1h,24h,7d`.
- Response per coin (subset we care about):
  - `id`, `symbol`, `name`.
  - `market_cap`.
  - `price_change_percentage_1h_in_currency` (field name inferred from docs; we will verify during implementation), similarly for `24h` and `7d`.

### Symbol Mapping Strategy

Because:

- Variational tickers are short symbols (`BTC`, `ETH`, etc.).
- CoinGecko `symbols` query can return multiple matches and is limited in count per request.

We will:

1. Build the set of unique `ticker`s from Variational listings.
2. For an initial, robust implementation:
  - Do a `coins/markets` call for a **broad universe** (e.g. top N by market cap using no filters, or paginated) and build a local symbol→coin index.
  - Prefer "primary" matches:
    - Lowercase compare of symbol.
    - If multiple matches, keep the one with highest `market_cap`.
3. Join the Variational tickers to this local index on symbol.
4. For tickers that fail to map automatically, list them out in the output with null metrics so you can decide manual overrides later.

## Script Design

File: `[fetch_variational_listings.py](fetch_variational_listings.py)`

### 1. Configuration

- Constants for:
  - `VARI_BASE_URL` (`https://omni-client-api.prod.ap-northeast-1.variational.io`).
  - `COINGECKO_BASE_URL` (start with `https://api.coingecko.com/api/v3`).
  - `VS_CURRENCY = "usd"`.
  - `PRICE_CHANGE_WINDOWS = "1h,24h,7d"`.
- Optional environment variable for a CoinGecko Pro API key (if you decide to use Pro):
  - e.g. read `COINGECKO_API_KEY` and, if set, add header/query param.

### 2. Variational Fetch Function

- `fetch_variational_listings() -> list[dict]`:
  - GET `VARI_BASE_URL + "/metadata/stats"`.
  - Raise on non-200, parse JSON.
  - Return `data["listings"]` as Python list.

### 3. CoinGecko Fetch Function

Two possible approaches; we implement the more robust one:

- `fetch_coingecko_markets_universe(pages: int = 4, per_page: int = 250) -> list[dict]`:
  - Loop `page=1..pages`, call `GET /coins/markets` with:
    - `vs_currency=usd`.
    - `order=market_cap_desc`.
    - `per_page=250`, `page=page`.
    - `price_change_percentage=1h,24h,7d`.
  - Aggregate results into a list.
  - Stop early if a page returns empty.

This should give up to ~1000 top coins, which is likely to cover almost all Variational listings.

### 4. Build Symbol Index

- `build_symbol_index(coins: list[dict]) -> dict[str, dict]`:
  - For each `coin` in `coins`:
    - `sym = coin["symbol"].lower()`.
    - If `sym` not in index, set it.
    - If already exists, keep the entry with **higher `market_cap`** to prefer the main asset.
  - Return dict mapping `symbol` → best-matching coin object.

### 5. Join Variational Listings to CoinGecko Data

- Represent Variational listings as a list of dicts with at least:
  - `vari_ticker` (original `ticker`).
  - `vari_name` (original `name`).
- `enrich_listings_with_coingecko(listings, symbol_index) -> list[dict]`:
  - For each listing:
    - `sym = listing["ticker"].lower()`.
    - If `sym` in `symbol_index`:
      - Access coin:
        - `coin_id`, `coin_symbol`, `coin_name`.
        - `market_cap`.
        - `price_change_percentage_1h_in_currency`.
        - `price_change_percentage_24h_in_currency`.
        - `price_change_percentage_7d_in_currency`.
      - Create output row with desired fields.
    - Else:
      - Create row with `coingecko_*` fields as `None`.

### 6. Output Table Format

We will:

- Use `pandas.DataFrame` for tabular operations.
- Columns:
  - `vari_ticker`
  - `vari_name`
  - `coingecko_id`
  - `coingecko_symbol`
  - `coingecko_name`
  - `market_cap_usd`
  - `price_change_1h_pct`
  - `price_change_24h_pct`
  - `price_change_7d_pct`

Outputs:

- Write to CSV: `vari_listings_enriched.csv`.
- Optionally write to JSON: `vari_listings_enriched.json` (records oriented).

### 7. CLI Entrypoint

- Implement a `main()` that:
  1. Fetches Variational listings.
  2. Fetches CoinGecko markets universe.
  3. Builds symbol index.
  4. Enriches listings.
  5. Writes CSV and JSON to the current directory.
  6. Prints a short summary:
    - Number of Variational listings.
    - Number successfully mapped to CoinGecko.
    - Sample of a few rows.

## Error Handling & Edge Cases

- Handle HTTP errors with clear messages and non-zero exit code.
- If CoinGecko rate-limits (429), backoff and retry a few times.
- If no CoinGecko match is found for a symbol, keep the row with null metrics so you can see what is missing.
- Use decimal-friendly types where possible (but market cap and percent changes are numeric floats as provided by CoinGecko).

## Possible Future Enhancements (not in initial implementation)

- Add a small config file (YAML/JSON) for manual symbol overrides (e.g., if a Variational ticker uses a non-standard symbol that requires mapping to a specific CoinGecko `id`).
- Wrap this in a minimal web UI or notebook for interactive exploration.
- Schedule periodic refresh and persistence in a SQLite DB.

