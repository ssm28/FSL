# Short-term Liquidity Forecast MVP

3-layer app: **Streamlit** (frontend) → **Python** (`engine.py`, `generator.py`) → **SQLite** (`out/<dataset>/liquidity.db`).
Consolidates bank balances and scheduled receivables/payables across 5 currencies, forecasts liquidity under
Base / Delayed-receipt / Stress scenarios, compares forecast with actuals, and flags labelled data exceptions.

## Quick start
```bash
pip install -r requirements.txt
python generator.py                  # builds BOTH datasets: out/starter (seconds) and out/full (~1-2 min)
python generator.py --only starter   # just one of them
python generator.py --repro          # generate twice with the same seed, compare row counts + SHA-256 checksums
streamlit run app.py                 # pick "starter" or "full" in the sidebar
```

## Output layout (created by `python generator.py`)
```
out/
  starter/   5 entities, 25 accounts, 4,000 ledger entries
  full/      30 entities, 150 accounts, 100,000 ledger entries, ~20k forecast items
    liquidity.db            SQLite database used by the app
    csv/<Table>.csv         every table, plus ground_truth.csv
    json/<Table>.json       every table, plus ground_truth.json
    ground_truth.csv        anomalies + labels (separate from inputs)
    validation_summary.json validation results + detection precision/recall
    manifest.json           seed, command, row counts, SHA-256 per table
    seed_data.sql           SQL dump
```
Fixed seed `20261022`; every number is derived from source records. Data is 100% synthetic (no external sources).

## UI tabs (pick a role in the sidebar)
Forecast (scenario chart, shortfalls, run new version) · Actual vs Forecast · Entity view · Exceptions (+ reviewer decisions) · Data quality (validation + detection precision/recall) · Data admin · Explorer.

See **developer.md** for the full logic, tables and file map.
