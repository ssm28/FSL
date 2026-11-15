# Developer guide

## 1. Files and what each does
| File | Layer | Purpose |
|---|---|---|
| `app.py` | Frontend | Streamlit UI only. Reads/writes through `engine.py`/`generator.py`. Role selector shows different tabs. |
| `engine.py` | Backend | Business logic: daily positions, forecast runs, scenario maths, forecast-vs-actual, anomaly **detection**, **validation**, scoring. No UI code. |
| `generator.py` | Backend | Synthetic data generator (+ CLI): master data → transactions → ledger → positions → forecasts → injected edge cases → ground truth. Also `export` and `repro`. |
| `schema.sql` | Database | All tables, PK/FK, enums (CHECK), ID format checks, indexes. |
| `requirements.txt`, `README.md`, `developer.md` | Docs | Setup and explanation. |

Created at run time (not shipped): `data/liquidity.db`, `data/ground_truth.csv`, `data/validation_summary.json`, `data/export/*`.

## 2. How the whole thing flows
```
generator.py  -- generate() -->  SQLite  <-- engine.py <-- app.py (Streamlit)
  1 entities, accounts, FX rates, scenarios      (master data, parents first)
  2 receivables + payables (+ StatusEvent)       (children, valid parent IDs)
  3 ledger = settled items + background activity
  4 DailyCashPosition (derived) + statement balance
  5 ForecastRun x CashFlowForecast (4 as-of dates x 3 scenarios) + actual-vs-forecast
  6 inject labelled anomalies  -> ground_truth.csv
  7 validate + detect + score
```
Timeline: history/actuals `2026-01-01 .. 2026-06-30` (the **cutoff**). Scheduled items are expected from Jan 1 to Aug 29.
Forecast as-of dates: Apr 1, May 1, May 31 (30-day horizon, fully covered by actuals so they can be compared) and Jun 30 (60-day horizon, pure future).

## 3. Tables, relationships, rules
Cardinality: LegalEntity 1:N BankAccount · BankAccount 1:N LedgerEntry / Receivable / Payable / DailyCashPosition / CashFlowForecast ·
LiquidityScenario 1:N ForecastRun · ForecastRun 1:N CashFlowForecast · Receivable/Payable 1:N CashFlowForecast (one per run) ·
**CashFlowForecast N:M LedgerEntry** via bridge `ForecastActualMatch` (a ledger entry is matched by forecasts in many runs) ·
Receivable/Payable 1:1 LedgerEntry when settled (via `ref_id`) · Receivable/Payable 1:N StatusEvent (polymorphic `record_id`, optional).

### Data dictionary (type · R=required/O=optional · rule). Dates are ISO text `YYYY-MM-DD`; money is REAL rounded to 2 dp.
**LegalEntity** `entity_id` PK `ENT-000001` · `legal_name` unique · `country` · `base_currency` ∈ USD/EUR/GBP/INR/JPY · `status` ACTIVE/DORMANT/CLOSED · `effective_from` R · `effective_to` O (open-ended = NULL).

**BankAccount** `account_id` PK `BANKACC-<entity_id>-001` · `entity_id` FK · `currency` enum · `account_type` OPERATING/COLLECTION/PAYROLL/TAX/RESERVE · `bank_name` · `opening_date` · `opening_balance` (account currency) · `statement_date` (= cutoff) · `statement_closing_balance` O (bank's reported balance; used for reconciliation) · `status`.

**CurrencyRate** `rate_id` PK `FX-2026-05-01-EUR` · `rate_date` · `currency` · `usd_per_unit` >0 (USD per 1 unit; USD=1) · `source`=SYNTHETIC · unique(rate_date,currency). Random walk with documented drift (INR, JPY weaken).

**LedgerEntry** `ledger_id` PK `LED-2026-00000001` · `account_id` FK · `entry_date` · `amount` signed, **account currency** (+in/−out) · `currency` (= account currency) · `entry_type` RECEIPT/PAYMENT/CARD_RECEIPT/OPEX/BANK_FEE/INTEREST/TRANSFER · `ref_type`,`ref_id` O (set for RECEIPT/PAYMENT = the receivable/payable settled) · `description`.

**Receivable** `receivable_id` PK `REC-2026-000001` · `entity_id`,`account_id` FK · `counterparty`, `counterparty_rating` A/B/C · `currency` (= account) · `amount`>0 · `created_date` ≤ `expected_date` · `actual_date` O (only if ≤ cutoff) · `delay_days` O (= actual − expected) · `status` OPEN/OVERDUE/PAID.
**Payable** `payable_id` PK `PAY-2026-000001` · same shape · `vendor`, `category`, `is_one_off` 0/1.

**StatusEvent** (append-only history) `event_id` PK `EVT-000001` · `record_type` RECEIVABLE/PAYABLE/FORECAST_RUN · `record_id` · `from_status` O · `to_status` · `event_date`. Lifecycle: `OPEN → (OVERDUE) → PAID`; runs: `ACTIVE → SUPERSEDED`.

**LiquidityScenario** `scenario_id` PK · `receipt_delay_days`, `receipt_haircut_pct`, `payable_accel_days`, `fx_shock_pct` (negative = non-USD currencies lose value vs USD).

**ForecastRun** `run_id` PK `FCAST-2026-05-01-001` (date + sequence per date) · `scenario_id` FK · `as_of_date` · `horizon_days` · `version` (per as_of+scenario) · `status` ACTIVE/SUPERSEDED · `created_at` · summary: `start_balance_usd`, `ending_balance_usd`, `min_balance_usd`, `min_balance_date`, `n_items`.

**CashFlowForecast** `cf_id` PK `CF-2026-000001` · `run_id`,`account_id` FK · `source_type`/`source_id` (receivable or payable) · `forecast_date` · `currency` · `amount` signed native · `fx_rate_used` · `fx_status` OK/FALLBACK · `amount_usd` · `actual_date_used` O (must be NULL; non-NULL = leakage anomaly). *No UNIQUE on (run, source)* so the duplicate anomaly can exist; detection finds it.

**ForecastActualMatch** (bridge) `match_id` PK `MATCH-000001` · `run_id`,`cf_id`,`ledger_id` FK (ledger O) · `forecast_amount_usd`, `actual_amount_usd`, `variance_usd` = actual − forecast · `variance_reason` NOT_SETTLED_IN_WINDOW / HAIRCUT_ASSUMPTION / FX_MOVEMENT.

**DailyCashPosition** PK(`account_id`,`position_date`) · `opening_balance` + `net_movement` = `closing_balance` (native) · `closing_balance_usd` (that day's rate, gaps forward-filled). Derived from ledger; safe to rebuild.

**ReviewDecision** (append-only, filled by the Risk reviewer in the UI) `decision_id` `REV-000001` · `issue_type`,`record_id` · `decision` · `reviewer` · `note` · `decided_at`.

## 4. Business logic (where to look)
- **Outcomes come from rules, not dice** (`generator.sched`): receivable delay depends on counterparty rating (A 0-2d, B 0-12d, C 8-45d) +5d for the top-10% invoices; 15% of payables pay 1-5d late; 1.5% of payables are large one-offs (5-12× size). Status/`actual_date` follow from delay vs the cutoff.
- **Ledger** = one entry per settled receivable/payable + background activity (opex, card receipts, fees, interest, transfers) up to the requested size.
- **Daily positions** (`engine.build_positions`): opening + cumulative ledger, converted to USD at each day's rate.
- **Forecast** (`engine.run_forecast`): uses only items known (`created_date ≤ as_of`) and still unsettled at the as-of date. `forecast_date = max(expected + shift, as_of+1)` (overdue items are assumed to land the next day); receipts shifted by `receipt_delay_days` and cut by the haircut; payables pulled forward by `payable_accel_days`; USD conversion at the **as-of day rate** × (1 + shock). Starting balance = consolidated positions at as-of. Scenarios differ **only** by the 4 documented parameters. Re-running creates a new version; the old run stays (SUPERSEDED) and both transitions are logged in `StatusEvent`.
- **Actual vs forecast** (`engine.compare_run`): matches each forecast item to the ledger entry settled inside the horizon; every variance gets a reason (not settled in window / haircut assumption / FX movement-or-shock).
- **Exceptions path**: missing FX rate → fallback to last known rate + `fx_status=FALLBACK`; projected overdrafts per account (`account_shortfalls`); anomaly detection (below); reviewer decisions stored in `ReviewDecision`.

## 5. Edge cases (injected AFTER valid baseline data) and how they are found
| issue_type | Injection | Detection rule (`engine.detect`) |
|---|---|---|
| BALANCE_MISMATCH | 3 accounts get a statement balance ±500-5000 off | statement ≠ opening + Σledger |
| FX_RATE_MISSING | 6 (currency, date) rates deleted, 3 on forecast as-of dates | gaps in daily rate calendar |
| DUPLICATE_FORECAST | ~0.2% rows copied with new `cf_id` | same (run, source) appears more than once |
| FORECAST_USES_ACTUAL | forecast_date set from a settlement after as-of | `actual_date_used > as_of_date` |
| CURRENCY_MIXED | non-USD rows with `fx_rate_used=1.0` | currency ≠ USD and rate = 1 |

`data/ground_truth.csv` (`gt_id, table_name, record_id, issue_type, description, expected_decision, is_anomaly`) also labels `DELAYED_RECEIVABLE` (>14 days) and `LARGE_ONE_OFF_PAYMENT`. The app/detector never read it, except the scoring table (precision/recall) on the *Data quality* tab.

## 6. Validation (`engine.CHECKS`, each must return 0)
FK integrity · ID formats · ledger currency = account currency · dates in range and ordered · forecast after as-of · daily position arithmetic · cutoff position = opening + ledger · PAID ⇒ ledger entry and PAID event · no double-posted ledger refs · BASE has zero adjustments · variance arithmetic + reason present.

## 7. Reproducibility
One `numpy.default_rng(seed)` drives everything, fixed seed `20261022`, fixed `created_at` for generated runs, deterministic sort orders. `--repro` generates twice and compares per-table row counts and SHA-256 of the sorted table contents. `--export` writes them in `manifest.json` with the exact command.

## 8. Common changes
- Scale: CLI flags `--entities --accounts --ledger --seed` (receivables = payables = 7.5% of ledger rows; forecast ≈ 20k at full scale).
- New scenario: add a tuple to `SCN` in `generator.py`, or insert a row in `LiquidityScenario`; it appears in the app automatically.
- New check: add a `name: SQL returning a violation count` entry in `engine.CHECKS`.
- New anomaly: inject in `generator.generate` step 6, add a `detect` rule, add the name to `engine.ANOM` and `DECISION`.
