# Binance kline fetch spec

Reference for pulling OHLCV candlesticks from the Binance **Spot** REST API. The companion script [`binancefetch.py`](binancefetch.py) implements this spec.

Official docs:

- [Market Data — Kline/Candlestick data](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints#klinecandlestick-data)
- [REST API limits](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits)

---

## Endpoint

| Host | Use |
|------|-----|
| `https://api.binance.com` | Default production API |
| `https://data-api.binance.vision` | Market-data only (no trading); same kline path |

```
GET /api/v3/klines
```

No API key required for public market data.

**Request weight:** `2` per call (counts toward IP request-weight budget).

---

## Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | yes | Trading pair, e.g. `ETHBTC`, `BTCUSDT` (uppercase, no separator) |
| `interval` | enum | yes | Candle resolution — see table below (case-sensitive) |
| `startTime` | long | no | Range start, **UTC milliseconds** (inclusive) |
| `endTime` | long | no | Range end, **UTC milliseconds** (inclusive) |
| `limit` | int | no | Bars per response; default `500`, max `1000` |
| `timeZone` | string | no | Shifts how *interval boundaries* are computed; `startTime`/`endTime` stay UTC. Default `0` (UTC). Range `[-12:00, +14:00]` |

### Behavior without / with time bounds

- **Neither** `startTime` nor `endTime`: returns the **most recent** bars up to `limit`.
- **`startTime` only**: oldest bars from `startTime` forward, up to `limit`.
- **`endTime` only**: most recent bars up to `endTime`, up to `limit`.
- **Both**: same as `startTime` only, but results do not exceed `endTime`.

Unlike some authenticated history endpoints (e.g. user trades), **klines have no documented 24-hour window cap**. You can walk backward arbitrarily by paginating; depth is limited only by how long the symbol has traded on Binance.

---

## Supported intervals (resolutions)

Values are **case-sensitive** (`1M` = month, not minute).

| Bucket | `interval` values |
|--------|-------------------|
| Seconds | `1s` |
| Minutes | `1m`, `3m`, `5m`, `15m`, `30m` |
| Hours | `1h`, `2h`, `4h`, `6h`, `8h`, `12h` |
| Days | `1d`, `3d` |
| Weeks | `1w` |
| Months | `1M` |

### Bars per request vs calendar span

Each request returns at most **1000** bars. Maximum calendar coverage per call:

| Interval | ~Max span @ limit=1000 |
|----------|-------------------------|
| `1s` | ~16.7 min |
| `1m` | ~16.7 h |
| `5m` | ~3.5 d |
| `15m` | ~10.4 d |
| `1h` | ~41.7 d |
| `4h` | ~166 d |
| `1d` | ~2.7 y |
| `1w` | ~19 y |

For longer history, paginate (see below).

---

## Response format

JSON array of arrays (chronological). Each inner array has 12 fields:

| Index | Field | Type | Notes |
|-------|-------|------|-------|
| 0 | Open time | ms | Unique bar id |
| 1 | Open | string decimal | |
| 2 | High | string decimal | |
| 3 | Low | string decimal | |
| 4 | Close | string decimal | |
| 5 | Volume | string decimal | Base asset |
| 6 | Close time | ms | |
| 7 | Quote volume | string decimal | |
| 8 | Number of trades | int | |
| 9 | Taker buy base volume | string decimal | |
| 10 | Taker buy quote volume | string decimal | |
| 11 | Unused | | Ignore |

---

## Pagination (multi-request fetch)

1. Set `startTime` / `endTime` for the desired UTC window.
2. Request with `limit=1000`.
3. Append results; set next `startTime = last_bar_open_time + 1`.
4. Stop when: empty batch, batch size &lt; 1000, or `startTime >= endTime`.

Example: 90 days of `15m` bars ≈ 8,640 bars → **9** requests (weight **18**).

`binancefetch.py` sleeps **100 ms** between pages by default (`--sleep`) to stay under rate limits.

---

## Rate limits

Limits are **per IP**, not per API key. Current hard limits (verify via `GET /api/v3/exchangeInfo` → `rateLimits`):

| Limiter | Typical cap | Applies to klines? |
|---------|-------------|-------------------|
| `REQUEST_WEIGHT` | **6,000 / minute** | Yes — each kline call = weight **2** → ~**3,000 kline requests/min** theoretical max |
| `RAW_REQUESTS` | Varies | Counts raw HTTP requests |
| Order limits | 100 / 10s, 200k / 24h | No (trading only) |

### Response headers (monitor usage)

- `X-MBX-USED-WEIGHT-1M` — weight consumed in the current 1-minute window
- On **429** (rate limit): back off; read `Retry-After` (seconds)
- Repeated 429s → **418** IP ban (2 min → up to 3 days for repeat offenders)

### Practical guidance

| Scenario | Suggested pacing |
|----------|------------------|
| Incremental update (1 symbol, 1 interval) | 1 request; no sleep needed |
| Full 14d @ 15m (1 symbol) | 2 requests; 100 ms gap |
| Full 1y @ 1h (1 symbol) | ~9 requests; 100 ms gap |
| Many symbols / fine intervals | Keep `X-MBX-USED-WEIGHT-1M` &lt; ~5,400; increase `--sleep` if needed |

---

## Time range strategies

### Rolling window (default in `binancefetch.py`)

```
end   = now (UTC)
start = end - days * 24h
```

After fetch, drop bars with `open_time_ms < start` so the JSON keeps a fixed rolling window.

### Explicit range

Pass `--start` and/or `--end` as ISO-8601 UTC (e.g. `2025-01-01T00:00:00Z`). Overrides `--days` for the fetch window (trim still uses `--days` unless you set a matching window).

### Incremental update

1. Load existing JSON.
2. If missing → full fetch for the window.
3. If present → fetch only `(last_open_time + 1) … now`, merge by `open_time_ms`, trim old bars.
4. Use `--full` to force a complete re-download.

---

## Output JSON schema (`binancefetch.py`)

```json
{
  "symbol": "ETHBTC",
  "interval": "15m",
  "days": 14,
  "source": "https://api.binance.com/api/v3/klines",
  "fetched_at": "2026-06-06T12:00:00+00:00",
  "bar_count": 1344,
  "bars": [
    {
      "time": "2026-05-23T12:00:00+00:00",
      "open_time_ms": 1748001600000,
      "open": 0.05234,
      "high": 0.05240,
      "low": 0.05230,
      "close": 0.05238,
      "volume": 12.5,
      "close_time_ms": 1748002499999,
      "quote_volume": 0.654,
      "trades": 42
    }
  ]
}
```

Optional `--data-js` writes a sibling `*.data.js` with `window.__BINANCE_KLINE_DATA__` for standalone HTML charts.

---

## Funding rate history (USDT-M futures)

```
GET /fapi/v1/fundingRate
```

Futures only. Shares **500 requests / 5 min / IP** with `GET /fapi/v1/fundingInfo`.

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | no | e.g. `CLUSDT`, `BTCUSDT` |
| `startTime` | long | no | UTC ms, inclusive |
| `endTime` | long | no | UTC ms, inclusive |
| `limit` | int | no | Default `100`, max `1000` |

- No `startTime`/`endTime` → most recent **200** records.
- With both bounds and more than `limit` rows → returns oldest `limit` rows from `startTime`; paginate with `startTime = last fundingTime + 1`.
- Results are **ascending** by `fundingTime`.

Response fields per record: `symbol`, `fundingRate`, `fundingTime`, `markPrice`.

`binancefetch.py --funding` writes `{symbol}_funding_last{days}d.json`:

```json
{
  "symbol": "CLUSDT",
  "days": 30,
  "source": "https://fapi.binance.com/fapi/v1/fundingRate",
  "fetched_at": "2026-06-06T12:00:00+00:00",
  "record_count": 180,
  "records": [
    {
      "time": "2026-05-07T08:00:00.001000+00:00",
      "funding_time_ms": 1778112000002,
      "funding_rate": -0.00127,
      "mark_price": 92.67
    }
  ]
}
```

---

## CLI quick reference

```bash
# From repo root — default: ETHBTC 15m, 14-day rolling window, incremental
python3 binancefetch/binancefetch.py

# Full refresh, custom symbol/interval/window
python3 binancefetch/binancefetch.py --symbol BTCUSDT --interval 1h --days 30 --full

# Explicit UTC range
python3 binancefetch/binancefetch.py --symbol ETHUSDT --interval 5m \
  --start 2025-05-01T00:00:00Z --end 2025-05-15T00:00:00Z --full

# Futures funding rate history (30d rolling window)
python3 binancefetch/binancefetch.py --symbol CLUSDT --funding --days 30 --data-host futures --full

# Custom output + chart embed (writes into binancefetch/ by default)
python3 binancefetch/binancefetch.py --symbol ETHBTC --interval 15m --data-js
```

---

## Related

[`ETHBTCfetch.py`](../ETHBTCfetch.py) is a project-specific wrapper that fetches ETHBTC / ETHUSDT / BTCUSDT @ 15m and feeds `ETHBTC_sim.html`. Use `binancefetch.py` for ad-hoc symbols and intervals.
