PRAGMA foreign_keys=ON;

CREATE TABLE LegalEntity(
  entity_id TEXT PRIMARY KEY CHECK(entity_id GLOB 'ENT-[0-9][0-9][0-9][0-9][0-9][0-9]'),
  legal_name TEXT NOT NULL UNIQUE,
  country TEXT NOT NULL,
  base_currency TEXT NOT NULL CHECK(base_currency IN ('USD','EUR','GBP','INR','JPY')),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','DORMANT','CLOSED')),
  effective_from TEXT NOT NULL,
  effective_to TEXT);

CREATE TABLE BankAccount(
  account_id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES LegalEntity(entity_id),
  currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','GBP','INR','JPY')),
  account_type TEXT NOT NULL CHECK(account_type IN ('OPERATING','COLLECTION','PAYROLL','TAX','RESERVE')),
  bank_name TEXT NOT NULL,
  opening_date TEXT NOT NULL,
  opening_balance REAL NOT NULL,
  statement_date TEXT NOT NULL,
  statement_closing_balance REAL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','DORMANT','CLOSED')));

CREATE TABLE CurrencyRate(
  rate_id TEXT PRIMARY KEY,
  rate_date TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','GBP','INR','JPY')),
  usd_per_unit REAL NOT NULL CHECK(usd_per_unit>0),
  source TEXT NOT NULL,
  UNIQUE(rate_date,currency));

CREATE TABLE LedgerEntry(
  ledger_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES BankAccount(account_id),
  entry_date TEXT NOT NULL,
  amount REAL NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','GBP','INR','JPY')),
  entry_type TEXT NOT NULL CHECK(entry_type IN ('RECEIPT','PAYMENT','CARD_RECEIPT','OPEX','BANK_FEE','INTEREST','TRANSFER')),
  ref_type TEXT CHECK(ref_type IN ('RECEIVABLE','PAYABLE')),
  ref_id TEXT,
  description TEXT);
CREATE INDEX ix_led_acc ON LedgerEntry(account_id,entry_date);
CREATE INDEX ix_led_ref ON LedgerEntry(ref_id);

CREATE TABLE Receivable(
  receivable_id TEXT PRIMARY KEY CHECK(receivable_id GLOB 'REC-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
  entity_id TEXT NOT NULL REFERENCES LegalEntity(entity_id),
  account_id TEXT NOT NULL REFERENCES BankAccount(account_id),
  counterparty TEXT NOT NULL,
  counterparty_rating TEXT NOT NULL CHECK(counterparty_rating IN ('A','B','C')),
  currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','GBP','INR','JPY')),
  amount REAL NOT NULL CHECK(amount>0),
  created_date TEXT NOT NULL,
  expected_date TEXT NOT NULL,
  actual_date TEXT,
  delay_days INTEGER,
  status TEXT NOT NULL CHECK(status IN ('OPEN','OVERDUE','PAID')));

CREATE TABLE Payable(
  payable_id TEXT PRIMARY KEY CHECK(payable_id GLOB 'PAY-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
  entity_id TEXT NOT NULL REFERENCES LegalEntity(entity_id),
  account_id TEXT NOT NULL REFERENCES BankAccount(account_id),
  vendor TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN ('RENT','PAYROLL','SUPPLIER','UTILITIES','CAPEX','TAX')),
  is_one_off INTEGER NOT NULL CHECK(is_one_off IN (0,1)),
  currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','GBP','INR','JPY')),
  amount REAL NOT NULL CHECK(amount>0),
  created_date TEXT NOT NULL,
  expected_date TEXT NOT NULL,
  actual_date TEXT,
  delay_days INTEGER,
  status TEXT NOT NULL CHECK(status IN ('OPEN','OVERDUE','PAID')));

-- append-only lifecycle history (never updated, only inserted)
CREATE TABLE StatusEvent(
  event_id TEXT PRIMARY KEY,
  record_type TEXT NOT NULL CHECK(record_type IN ('RECEIVABLE','PAYABLE','FORECAST_RUN')),
  record_id TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  event_date TEXT NOT NULL);
CREATE INDEX ix_evt ON StatusEvent(record_id);

CREATE TABLE LiquidityScenario(
  scenario_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  receipt_delay_days INTEGER NOT NULL,
  receipt_haircut_pct REAL NOT NULL,
  payable_accel_days INTEGER NOT NULL,
  fx_shock_pct REAL NOT NULL);

CREATE TABLE ForecastRun(
  run_id TEXT PRIMARY KEY CHECK(run_id GLOB 'FCAST-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9]'),
  scenario_id TEXT NOT NULL REFERENCES LiquidityScenario(scenario_id),
  as_of_date TEXT NOT NULL,
  horizon_days INTEGER NOT NULL CHECK(horizon_days>0),
  version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUPERSEDED')),
  created_at TEXT NOT NULL,
  start_balance_usd REAL, ending_balance_usd REAL, min_balance_usd REAL,
  min_balance_date TEXT, n_items INTEGER,
  UNIQUE(as_of_date,scenario_id,version));

CREATE TABLE CashFlowForecast(
  cf_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES ForecastRun(run_id),
  account_id TEXT NOT NULL REFERENCES BankAccount(account_id),
  source_type TEXT NOT NULL CHECK(source_type IN ('RECEIVABLE','PAYABLE')),
  source_id TEXT NOT NULL,
  forecast_date TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','GBP','INR','JPY')),
  amount REAL NOT NULL,
  fx_rate_used REAL NOT NULL,
  fx_status TEXT NOT NULL CHECK(fx_status IN ('OK','FALLBACK')),
  amount_usd REAL NOT NULL,
  actual_date_used TEXT);
CREATE INDEX ix_cf_run ON CashFlowForecast(run_id);

-- bridge: forecast item <-> actual ledger entry (N:M over runs/scenarios)
CREATE TABLE ForecastActualMatch(
  match_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES ForecastRun(run_id),
  cf_id TEXT NOT NULL REFERENCES CashFlowForecast(cf_id),
  ledger_id TEXT REFERENCES LedgerEntry(ledger_id),
  forecast_amount_usd REAL NOT NULL,
  actual_amount_usd REAL NOT NULL,
  variance_usd REAL NOT NULL,
  variance_reason TEXT NOT NULL CHECK(variance_reason IN ('NOT_SETTLED_IN_WINDOW','HAIRCUT_ASSUMPTION','FX_MOVEMENT')));

-- derived (rebuildable) from LedgerEntry + CurrencyRate
CREATE TABLE DailyCashPosition(
  account_id TEXT NOT NULL REFERENCES BankAccount(account_id),
  position_date TEXT NOT NULL,
  opening_balance REAL NOT NULL,
  net_movement REAL NOT NULL,
  closing_balance REAL NOT NULL,
  closing_balance_usd REAL NOT NULL,
  PRIMARY KEY(account_id,position_date));

-- reviewer decisions on exceptions (append-only)
CREATE TABLE ReviewDecision(
  decision_id TEXT PRIMARY KEY,
  issue_type TEXT NOT NULL,
  record_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('ACCEPT_AS_IS','EXCLUDE_FROM_FORECAST','ESCALATE','CORRECTED_AT_SOURCE')),
  reviewer TEXT NOT NULL,
  note TEXT,
  decided_at TEXT NOT NULL);
