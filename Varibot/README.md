## variationalbot (Variational bot skeleton)

### What this does
- Authenticates to Variational using `vr-token` + `vr-connected-address` cookies/headers
- Polls every `BOT_POLL_INTERVAL_S` seconds (default 300)
- Fetches:
  - `GET /api/portfolio`
  - `GET /api/positions`
  - `GET /api/orders/v2`
- Writes last snapshots to SQLite (`BOT_STATE_DB`, default `./state.db`)
- Optionally runs an external signal script (`BOT_SIGNAL_SCRIPT`) and generates **paper** order intents

### Setup (local)
1. Install deps:

```bash
pip install -r requirements.txt
```

2. Create `.env` next to this README (do not commit it):

```env
VR_TOKEN=...
VR_WALLET_ADDRESS=0x...
BOT_MODE=paper
```

3. Run:

```bash
python -m variationalbot.main
```

### Signals contract
If `BOT_SIGNAL_SCRIPT` is set, the bot will run it each cycle with JSON on stdin:

- `portfolio`: raw `/api/portfolio` JSON
- `positions`: raw `/api/positions` JSON
- `orders`: raw `/api/orders/v2` JSON

The script should print a single JSON object to stdout, e.g.:

```json
{
  "long": [{"asset":"SOL","usd_size":50.0,"score":0.7,"leverage":20}],
  "short": [],
  "meta": {"note":"optional"}
}
```

### Railway
- Use the `Procfile` (`worker: python -m variationalbot.main`)
- Set env vars in Railway instead of `.env`

