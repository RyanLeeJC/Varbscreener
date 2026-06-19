# Funding screener

## Initialise (live)

From `Varibot/` (needs `.env` for Vari funding rates):

```bash
cd Varibot
python3 funding_screener_server.py
```

Then open:

http://127.0.0.1:8765/funding_screener.html

Auto-refresh hits `GET /api/screener` every 60s. Use ↻ to refresh manually.

Optional port:

```bash
python3 funding_screener_server.py --port 8765
```

## Regenerate embedded snapshot (optional)

For offline / double-click use, write `funding_screener.data.js` first:

```bash
cd Varibot
python3 fundingratecheck.py --write-screener-data funding_screener.data.js
```

Then open `funding_screener.html` from the same folder (no server required; live refresh falls back to the snapshot).
