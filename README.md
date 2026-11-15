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

## What this project does (in plain words)
A company with several legal entities holds many bank accounts in 5 currencies. Treasury needs to answer:
**"If we keep paying and collecting as scheduled, will any account run out of cash in the next 30-60 days, and what if customers pay late or currencies drop?"**
The system (1) builds realistic synthetic books, (2) computes today's cash per account, (3) projects cash forward under three scenarios, (4) later compares the projection with what really happened, and (5) flags bad data so nobody trusts a wrong number.

## How the forecast works
1. **Start point:** closing cash of every account on the as-of date (opening balance + all ledger entries), converted to USD.
2. **Inputs:** receivables (money coming in) and payables (money going out) that were already known at the as-of date and are not yet settled.
3. **Dates:** each item lands on its expected date; overdue items land the next day.
4. **Scenario adjustments** (the only thing that differs between scenarios):

| Scenario | Receipts | Payables | FX (non-USD vs USD) |
|---|---|---|---|
| BASE | on expected date | on expected date | rate on as-of date |
| DELAYED | +14 days | unchanged | unchanged |
| STRESS | +21 days and -10% amount | 7 days earlier | -10% |

5. **Roll forward:** daily balance = start + cumulative net flows. Reported: ending balance, lowest balance and its date, and accounts projected below zero.
6. **Versioning:** a re-run creates a new version; the old one is kept as SUPERSEDED (nothing is overwritten).
7. **Accuracy check:** once real dates pass, every forecast item is matched to its ledger entry and each variance gets a reason: not settled in window, haircut assumption, or FX movement.

## Who benefits
| User | What they get |
|---|---|
| Operations analyst | Scenario chart, accounts about to go negative (move cash before it happens), run new forecast versions, see how wrong past forecasts were and why |
| Risk / compliance reviewer | Exception list (5 types), approve/exclude/escalate decisions stored as an audit trail, validation checks and detection precision/recall |
| Customer / account holder | Own entity's accounts, balances in USD and native currency, daily balance chart |
| System administrator | Regenerate data at any size, export CSV/JSON/SQL, reproducibility check, raw table explorer |

## Database tables (13)
Row counts are for the `full` dataset.
| Table | Rows | Purpose |
|---|---|---|
| LegalEntity | 30 | Companies that own accounts (parent of everything) |
| BankAccount | 150 | Accounts per entity, with currency, opening balance, bank statement balance |
| CurrencyRate | 899 | Daily USD-per-unit rates for 5 currencies (6 gaps on purpose) |
| LedgerEntry | 100,000 | Actual money movements; source of truth for balances |
| Receivable | 7,500 | Money owed to us: expected vs actual date, rating, status |
| Payable | 7,500 | Money we owe: expected vs actual date, one-off flag, status |
| StatusEvent | 31,544 | Append-only history of status changes (OPEN, OVERDUE, PAID; runs ACTIVE, SUPERSEDED) |
| LiquidityScenario | 3 | The documented assumptions of Base / Delayed / Stress |
| ForecastRun | 12 | One forecast version: as-of date, scenario, horizon, summary results |
| CashFlowForecast | ~20.8k | Forecast line items inside a run (incl. injected bad rows) |
| ForecastActualMatch | ~14.4k | Bridge: forecast item to actual ledger entry, with variance and reason |
| DailyCashPosition | 27,150 | Derived daily balance per account (rebuildable from ledger) |
| ReviewDecision | 0 | Reviewer decisions entered in the UI (starts empty) |

Plus `ground_truth` (a file, not a table): the answer key for injected anomalies and labels. Full field-by-field dictionary is in **developer.md**.

## Edge cases built in (labelled in ground truth)
Statement balance does not match opening + ledger · FX rate missing · forecast counted twice · forecast uses an actual from after the cutoff · non-USD amount treated as USD.

## UI tabs (pick a role in the sidebar)
Forecast (scenario chart, shortfalls, run new version) · Actual vs Forecast · Entity view · Exceptions (+ reviewer decisions) · Data quality (validation + detection precision/recall) · Data admin · Explorer.

See **developer.md** for the full logic, tables and file map.
