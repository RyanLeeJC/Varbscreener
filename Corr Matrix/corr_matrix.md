# Correlations Matrix — quick run

1. From the repo root (or this folder):

   ```bash
   python3 "Corr Matrix/server.py"
   ```

2. Open **http://127.0.0.1:8787/** in your browser.

**Optional**

- Different port: `CORR_MATRIX_PORT=9000 python3 "Corr Matrix/server.py"`
- Different DB file: `VARI_CORR_DB="/path/to/vari_railway_db.sqlite" python3 "Corr Matrix/server.py"`

Default DB path is `Vari Listings/vari_railway_db/vari_railway_db.sqlite` (relative to the repo root).
