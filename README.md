# Equity Funding Rate Screener

Cross-venue funding rate screener for Ondo Perps, Hyperliquid xyz, Lighter, and Variational.

**Live site:** configure GitHub Pages on branch `Varbscreener` → `https://ryanleejc.github.io/varibot/`

## Open locally (no server)

Double-click `index.html` (needs sibling `funding_screener.data.js`).

## Refresh embedded data

```bash
pip install -r requirements.txt
cp env.example .env   # fill VR_TOKEN + VR_WALLET_ADDRESS
python3 fundingratecheck.py --write-screener-data funding_screener.data.js
```

## GitHub Pages deploy

Push to `Varbscreener`. The workflow rebuilds `funding_screener.data.js` and deploys `index.html` + the data file.

Set repository secrets for Vari funding rates:

- `VR_TOKEN`
- `VR_WALLET_ADDRESS`

Optional: `HTTPS_PROXY` if Omni blocks GitHub Actions IPs.
