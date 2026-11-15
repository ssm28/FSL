# Short-term Liquidity Forecast MVP

3-layer app: **Streamlit** (frontend) → **Python** (`engine.py`, `generator.py`) → **SQLite** (`data/liquidity.db`).
Consolidates bank balances and scheduled receivables/payables across 5 currencies, forecasts liquidity under
Base / Delayed-receipt / Stress scenarios, compares forecast with actuals, and flags labelled data exceptions.

## Quick start
```bash
pip install -r requirements.txt

python generator.py                                    # full: 30 entities, 150 accounts, 100k ledger, ~20k forecast items (~1 min)
python generator.py --entities 5 --accounts 25 --ledger 4000   # starter dataset (seconds), same code

streamlit run app.py                                   # open the UI (it also offers to generate data if none exists)
```
Optional:
```bash
python generator.py --export     # CSV per table + seed_data.sql + manifest.json -> data/export/
python generator.py --repro      # generate twice with the same seed, compare row counts + SHA-256 checksums
```

## What you get
| Output | Where |
|---|---|
| SQLite database | `data/liquidity.db` |
| Ground truth (anomalies + labels, kept separate) | `data/ground_truth.csv` |
| Validation + detection metrics | `data/validation_summary.json` |
| CSV / SQL seed / manifest (checksums, command, seed) | `data/export/` (only with `--export`) |

Fixed seed `20261022`; every number is derived from source records. Data is 100% synthetic (no external sources).

## UI tabs (pick a role in the sidebar)
Forecast (scenario chart, shortfalls, run new version) · Actual vs Forecast · Entity view · Exceptions (+ reviewer decisions) · Data quality (validation + detection precision/recall) · Data admin · Explorer.

See **developer.md** for the full logic, tables and file map.
