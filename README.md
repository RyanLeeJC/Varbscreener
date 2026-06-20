# Equity Funding Rate Screener

Cross-venue funding rate screener for Ondo Perps, Hyperliquid xyz, Lighter, and Variational.

**Live site:** `https://ryanleejc.github.io/Varbscreener/`

## Open locally (no server)

Double-click `index.html` (needs sibling `funding_screener.data.js`).

## Refresh embedded data

```bash
pip install -r requirements.txt
cp env.example .env   # fill VR_TOKEN + VR_WALLET_ADDRESS
python3 fundingratecheck.py --write-screener-data funding_screener.data.js
```

## GitHub Pages deploy

Push to `main`. The workflow rebuilds `funding_screener.data.js` every 15 minutes and deploys `index.html` + the data file.

Vari funding rates use the public `/metadata/stats` API (no auth required for the screener snapshot).

Optional repository secrets (only if CI refresh fails without them):

- `VR_TOKEN`
- `VR_WALLET_ADDRESS`
- `HTTPS_PROXY` — if Omni blocks GitHub Actions IPs
