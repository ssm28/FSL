"""Backend logic shared by the generator and the Streamlit app (no UI here)."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

DB = "data/liquidity.db"
SCHEMA = Path(__file__).with_name("schema.sql")
ANOM = ["BALANCE_MISMATCH", "FX_RATE_MISSING", "DUPLICATE_FORECAST", "FORECAST_USES_ACTUAL", "CURRENCY_MIXED"]


# ---------- helpers ----------
def connect(db=DB):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def q(conn, sql, params=()):
    return pd.read_sql_query(sql, conn, params=params)


def write(conn, table, df):
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.strftime("%Y-%m-%d")
    df.to_sql(table, conn, if_exists="append", index=False)


def next_n(conn, table, col, prefix):
    r = conn.execute(f"SELECT MAX({col}) FROM {table} WHERE {col} LIKE ?", (prefix + "%",)).fetchone()[0]
    return int(r[len(prefix):]) + 1 if r else 1


def rate_grid(conn):
    """Wide table date x currency of USD-per-unit, gaps forward-filled (missing rates fall back to last known)."""
    r = q(conn, "SELECT currency,rate_date,usd_per_unit FROM CurrencyRate")
    g = r.pivot(index="rate_date", columns="currency", values="usd_per_unit")
    g.index = pd.to_datetime(g.index)
    return g.reindex(pd.date_range(g.index.min(), g.index.max())).ffill().bfill()


def asof_rates(conn, d):
    r = q(conn, "SELECT currency,rate_date,usd_per_unit FROM CurrencyRate WHERE rate_date<=? ORDER BY rate_date", (d,))
    r = r.groupby("currency").tail(1).set_index("currency")
    return r.usd_per_unit.to_dict(), {c: ("OK" if dt == d else "FALLBACK") for c, dt in r.rate_date.items()}


# ---------- daily cash positions (derived from ledger) ----------
def build_positions(conn):
    acc = q(conn, "SELECT account_id,currency,opening_balance,opening_date,statement_date FROM BankAccount")
    days = pd.date_range(acc.opening_date.min(), acc.statement_date.max())
    led = q(conn, "SELECT account_id,entry_date,SUM(amount) net FROM LedgerEntry GROUP BY 1,2")
    net = led.pivot(index="entry_date", columns="account_id", values="net")
    net.index = pd.to_datetime(net.index)
    net = net.reindex(index=days, columns=acc.account_id.values).fillna(0.0)
    opening = pd.Series(acc.opening_balance.values, index=acc.account_id.values)
    close = net.cumsum() + opening
    fx = rate_grid(conn).reindex(days).ffill().bfill()[acc.currency.values]
    fx.columns = acc.account_id.values
    out = pd.DataFrame({"opening_balance": (close - net).stack(), "net_movement": net.stack(),
                        "closing_balance": close.stack(), "closing_balance_usd": (close * fx).stack()}).reset_index()
    out.columns = ["position_date", "account_id", "opening_balance", "net_movement", "closing_balance", "closing_balance_usd"]
    num = ["opening_balance", "net_movement", "closing_balance", "closing_balance_usd"]
    out[num] = out[num].round(2)
    out = out[["account_id", "position_date"] + num]
    conn.execute("DELETE FROM DailyCashPosition")
    write(conn, "DailyCashPosition", out)
    conn.commit()


def start_balance(conn, a, shock_pct):
    d = q(conn, "SELECT b.currency,SUM(p.closing_balance_usd) usd FROM DailyCashPosition p "
                "JOIN BankAccount b USING(account_id) WHERE p.position_date=? GROUP BY 1", (a,))
    return float((d.usd * np.where(d.currency == "USD", 1.0, 1 + shock_pct / 100)).sum())


# ---------- forecasting ----------
def _event(conn, rtype, rid, frm, to, day):
    n = next_n(conn, "StatusEvent", "event_id", "EVT-")
    conn.execute("INSERT INTO StatusEvent VALUES(?,?,?,?,?,?)", (f"EVT-{n:06d}", rtype, rid, frm, to, day))


def run_forecast(conn, as_of, scenario_id, horizon=30, created_at=None):
    """Create a NEW forecast version (old ones stay, marked SUPERSEDED). Deterministic given the data."""
    as_of = pd.Timestamp(as_of)
    a = as_of.strftime("%Y-%m-%d")
    end = as_of + pd.Timedelta(days=int(horizon))
    delay, hair, accel, shock = conn.execute(
        "SELECT receipt_delay_days,receipt_haircut_pct,payable_accel_days,fx_shock_pct FROM LiquidityScenario WHERE scenario_id=?",
        (scenario_id,)).fetchone()
    created_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    run_id = f"FCAST-{a}-{next_n(conn, 'ForecastRun', 'run_id', f'FCAST-{a}-'):03d}"
    ver = 1 + conn.execute("SELECT COUNT(*) FROM ForecastRun WHERE as_of_date=? AND scenario_id=?", (a, scenario_id)).fetchone()[0]
    for (old,) in conn.execute("SELECT run_id FROM ForecastRun WHERE as_of_date=? AND scenario_id=? AND status='ACTIVE'",
                               (a, scenario_id)).fetchall():
        conn.execute("UPDATE ForecastRun SET status='SUPERSEDED' WHERE run_id=?", (old,))
        _event(conn, "FORECAST_RUN", old, "ACTIVE", "SUPERSEDED", created_at[:10])
    conn.execute("INSERT INTO ForecastRun(run_id,scenario_id,as_of_date,horizon_days,version,status,created_at) VALUES(?,?,?,?,?,'ACTIVE',?)",
                 (run_id, scenario_id, a, int(horizon), ver, created_at))
    _event(conn, "FORECAST_RUN", run_id, None, "ACTIVE", created_at[:10])

    # only items KNOWN and still unsettled at as_of are used (no future information)
    e1 = end.strftime("%Y-%m-%d")
    e2 = (end + pd.Timedelta(days=int(accel))).strftime("%Y-%m-%d")
    rec = q(conn, "SELECT receivable_id source_id,account_id,currency,amount,expected_date FROM Receivable "
                  "WHERE created_date<=? AND (actual_date IS NULL OR actual_date>?) AND expected_date<=?", (a, a, e1))
    rec["source_type"], rec["sign"], rec["shift"], rec["factor"] = "RECEIVABLE", 1.0, int(delay), 1 - hair / 100
    pay = q(conn, "SELECT payable_id source_id,account_id,currency,amount,expected_date FROM Payable "
                  "WHERE created_date<=? AND (actual_date IS NULL OR actual_date>?) AND expected_date<=?", (a, a, e2))
    pay["source_type"], pay["sign"], pay["shift"], pay["factor"] = "PAYABLE", -1.0, -int(accel), 1.0
    f = pd.concat([rec, pay], ignore_index=True)
    f["forecast_date"] = (pd.to_datetime(f.expected_date) + pd.to_timedelta(f["shift"].astype(int), unit="D")).clip(lower=as_of + pd.Timedelta(days=1))
    f = f[f.forecast_date <= end].sort_values(["forecast_date", "source_id"]).reset_index(drop=True)
    rates, st = asof_rates(conn, a)
    f["fx_status"] = f.currency.map(st)
    f["fx_rate_used"] = f.currency.map(rates) * np.where(f.currency == "USD", 1.0, 1 + shock / 100)
    f["amount"] = (f.sign * f.amount * f.factor).round(2)
    f["amount_usd"] = (f.amount * f.fx_rate_used).round(2)
    n0 = next_n(conn, "CashFlowForecast", "cf_id", "CF-2026-")
    f["cf_id"] = [f"CF-2026-{n0 + i:06d}" for i in range(len(f))]
    f["run_id"], f["actual_date_used"] = run_id, None
    write(conn, "CashFlowForecast", f[["cf_id", "run_id", "account_id", "source_type", "source_id", "forecast_date",
                                       "currency", "amount", "fx_rate_used", "fx_status", "amount_usd", "actual_date_used"]])
    refresh_summary(conn, run_id)
    return run_id


def _run(conn, run_id):
    return q(conn, "SELECT r.as_of_date a,r.horizon_days h,s.fx_shock_pct shock FROM ForecastRun r "
                   "JOIN LiquidityScenario s USING(scenario_id) WHERE r.run_id=?", (run_id,)).iloc[0]


def daily_path(conn, run_id):
    r = _run(conn, run_id)
    days = pd.date_range(pd.Timestamp(r.a) + pd.Timedelta(days=1), periods=int(r.h))
    f = q(conn, "SELECT forecast_date,SUM(amount_usd) net FROM CashFlowForecast WHERE run_id=? GROUP BY 1", (run_id,))
    net = pd.Series(f.net.values, index=pd.to_datetime(f.forecast_date)).reindex(days, fill_value=0.0)
    bal = start_balance(conn, r.a, r.shock) + net.cumsum()
    return pd.DataFrame({"date": days, "net_usd": net.values, "balance_usd": bal.values})


def refresh_summary(conn, run_id):
    r = _run(conn, run_id)
    p = daily_path(conn, run_id)
    i = int(p.balance_usd.values.argmin())
    n = conn.execute("SELECT COUNT(*) FROM CashFlowForecast WHERE run_id=?", (run_id,)).fetchone()[0]
    conn.execute("UPDATE ForecastRun SET start_balance_usd=?,ending_balance_usd=?,min_balance_usd=?,min_balance_date=?,n_items=? WHERE run_id=?",
                 (round(start_balance(conn, r.a, r.shock), 2), round(float(p.balance_usd.iloc[-1]), 2),
                  round(float(p.balance_usd.iloc[i]), 2), p.date.iloc[i].strftime("%Y-%m-%d"), n, run_id))
    conn.commit()


def account_shortfalls(conn, run_id):
    """Per account: lowest projected balance (native currency) over the horizon. Negative = projected overdraft."""
    r = _run(conn, run_id)
    s = q(conn, "SELECT p.account_id,b.currency,p.closing_balance start_balance FROM DailyCashPosition p "
                "JOIN BankAccount b USING(account_id) WHERE p.position_date=?", (r.a,)).set_index("account_id")
    f = q(conn, "SELECT account_id,forecast_date,SUM(amount) net FROM CashFlowForecast WHERE run_id=? GROUP BY 1,2 ORDER BY 2", (run_id,))
    f["cum"] = f.groupby("account_id").net.cumsum()
    s = s.join(f.groupby("account_id").cum.min().rename("min_cum_flow")).fillna({"min_cum_flow": 0.0})
    s["min_projected_balance"] = (s.start_balance + s.min_cum_flow.clip(upper=0)).round(2)
    return s.reset_index().sort_values("min_projected_balance")


def compare_run(conn, run_id):
    """Store forecast-vs-actual per item, with a reason for every variance. Only if actuals cover the horizon."""
    r = _run(conn, run_id)
    a, e = r.a, (pd.Timestamp(r.a) + pd.Timedelta(days=int(r.h))).strftime("%Y-%m-%d")
    last = conn.execute("SELECT MAX(entry_date) FROM LedgerEntry").fetchone()[0]
    if last < e or conn.execute("SELECT 1 FROM ForecastActualMatch WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
        return 0
    f = q(conn, "SELECT cf_id,source_id,currency,amount,amount_usd FROM CashFlowForecast WHERE run_id=?", (run_id,))
    l = q(conn, "SELECT ledger_id,ref_id source_id,entry_date,amount actual_amount FROM LedgerEntry "
                "WHERE ref_id IS NOT NULL AND entry_date>? AND entry_date<=?", (a, e))
    m = f.merge(l, on="source_id", how="left")
    gl = rate_grid(conn).stack().rename("rate").reset_index()
    gl.columns = ["d", "currency", "rate"]
    m["d"] = pd.to_datetime(m.entry_date)
    m = m.merge(gl, on=["d", "currency"], how="left")
    m["actual_usd"] = (m.actual_amount * m.rate).fillna(0.0).round(2)
    m["reason"] = np.where(m.actual_amount.isna(), "NOT_SETTLED_IN_WINDOW",
                           np.where((m.actual_amount - m.amount).abs() > 0.005, "HAIRCUT_ASSUMPTION", "FX_MOVEMENT"))
    n0 = next_n(conn, "ForecastActualMatch", "match_id", "MATCH-")
    out = pd.DataFrame({"match_id": [f"MATCH-{n0 + i:06d}" for i in range(len(m))], "run_id": run_id, "cf_id": m.cf_id.values,
                        "ledger_id": m.ledger_id.values, "forecast_amount_usd": m.amount_usd.values,
                        "actual_amount_usd": m.actual_usd.values, "variance_usd": (m.actual_usd - m.amount_usd).round(2).values,
                        "variance_reason": m.reason.values})
    write(conn, "ForecastActualMatch", out)
    conn.commit()
    return len(out)


# ---------- exception detection (rules only; never reads ground_truth) ----------
def detect(conn):
    out = []

    def add(t, tab, vals):
        out.append(pd.DataFrame({"issue_type": t, "table_name": tab, "record_id": list(vals)}))

    add("BALANCE_MISMATCH", "BankAccount", q(conn,
        "SELECT a.account_id FROM BankAccount a WHERE a.statement_closing_balance IS NOT NULL AND "
        "ABS(a.statement_closing_balance-(a.opening_balance+(SELECT COALESCE(SUM(amount),0) FROM LedgerEntry l "
        "WHERE l.account_id=a.account_id AND l.entry_date<=a.statement_date)))>0.05").account_id)
    r = q(conn, "SELECT currency,rate_date FROM CurrencyRate")
    sp = q(conn, "SELECT MIN(opening_date) s,MAX(statement_date) e FROM BankAccount").iloc[0]
    exp = {(c, d.strftime("%Y-%m-%d")) for c in r.currency.unique() for d in pd.date_range(sp.s, sp.e)}
    add("FX_RATE_MISSING", "CurrencyRate", [f"FX-{d}-{c}" for c, d in sorted(exp - set(zip(r.currency, r.rate_date)))])
    add("DUPLICATE_FORECAST", "CashFlowForecast", q(conn,
        "SELECT cf_id FROM CashFlowForecast WHERE cf_id NOT IN "
        "(SELECT MIN(cf_id) FROM CashFlowForecast GROUP BY run_id,source_type,source_id)").cf_id)
    add("FORECAST_USES_ACTUAL", "CashFlowForecast", q(conn,
        "SELECT f.cf_id FROM CashFlowForecast f JOIN ForecastRun r USING(run_id) "
        "WHERE f.actual_date_used IS NOT NULL AND f.actual_date_used>r.as_of_date").cf_id)
    add("CURRENCY_MIXED", "CashFlowForecast", q(conn,
        "SELECT cf_id FROM CashFlowForecast WHERE currency<>'USD' AND fx_rate_used=1.0").cf_id)
    return pd.concat(out, ignore_index=True)


def evaluate(det, gt):
    rows = []
    for t in ANOM:
        D, G = set(det[det.issue_type == t].record_id), set(gt[gt.issue_type == t].record_id)
        tp = len(D & G)
        rows.append({"issue_type": t, "ground_truth": len(G), "detected": len(D), "true_pos": tp,
                     "precision": round(tp / len(D), 3) if D else None, "recall": round(tp / len(G), 3) if G else None})
    return pd.DataFrame(rows)


# ---------- validation (each query must return 0 violations) ----------
CHECKS = {
    "Entity ID format": "SELECT COUNT(*) FROM LegalEntity WHERE entity_id NOT GLOB 'ENT-[0-9][0-9][0-9][0-9][0-9][0-9]'",
    "Account ID = BANKACC-<entity>-NNN": "SELECT COUNT(*) FROM BankAccount WHERE account_id NOT LIKE 'BANKACC-'||entity_id||'-___'",
    "Ledger/CF/Run ID formats": "SELECT (SELECT COUNT(*) FROM LedgerEntry WHERE ledger_id NOT GLOB 'LED-2026-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]')"
        "+(SELECT COUNT(*) FROM CashFlowForecast WHERE cf_id NOT GLOB 'CF-2026-[0-9][0-9][0-9][0-9][0-9][0-9]')",
    "Ledger currency = account currency": "SELECT COUNT(*) FROM LedgerEntry l JOIN BankAccount a USING(account_id) WHERE l.currency<>a.currency",
    "Ledger dates inside account window": "SELECT COUNT(*) FROM LedgerEntry l JOIN BankAccount a USING(account_id) WHERE l.entry_date<a.opening_date OR l.entry_date>a.statement_date",
    "Receivable/Payable date order": "SELECT (SELECT COUNT(*) FROM Receivable WHERE expected_date<created_date OR actual_date<created_date)"
        "+(SELECT COUNT(*) FROM Payable WHERE expected_date<created_date OR actual_date<created_date)",
    "Forecast date after as-of date": "SELECT COUNT(*) FROM CashFlowForecast f JOIN ForecastRun r USING(run_id) WHERE f.forecast_date<=r.as_of_date",
    "Daily position: opening+net=closing": "SELECT COUNT(*) FROM DailyCashPosition WHERE ABS(opening_balance+net_movement-closing_balance)>0.02",
    "Cutoff position = opening + ledger": "SELECT COUNT(*) FROM BankAccount a JOIN DailyCashPosition p ON p.account_id=a.account_id AND p.position_date=a.statement_date "
        "WHERE ABS(p.closing_balance-(a.opening_balance+(SELECT COALESCE(SUM(amount),0) FROM LedgerEntry l WHERE l.account_id=a.account_id)))>0.05",
    "PAID items have ledger entry": "SELECT (SELECT COUNT(*) FROM Receivable r WHERE status='PAID' AND NOT EXISTS(SELECT 1 FROM LedgerEntry WHERE ref_id=r.receivable_id))"
        "+(SELECT COUNT(*) FROM Payable p WHERE status='PAID' AND NOT EXISTS(SELECT 1 FROM LedgerEntry WHERE ref_id=p.payable_id))",
    "PAID items have PAID event": "SELECT (SELECT COUNT(*) FROM Receivable r WHERE status='PAID' AND NOT EXISTS(SELECT 1 FROM StatusEvent WHERE record_id=r.receivable_id AND to_status='PAID'))"
        "+(SELECT COUNT(*) FROM Payable p WHERE status='PAID' AND NOT EXISTS(SELECT 1 FROM StatusEvent WHERE record_id=p.payable_id AND to_status='PAID'))",
    "Duplicate ledger postings": "SELECT COUNT(*) FROM (SELECT 1 FROM LedgerEntry WHERE ref_id IS NOT NULL GROUP BY ref_id HAVING COUNT(*)>1)",
    "BASE scenario has no adjustments": "SELECT COUNT(*) FROM LiquidityScenario WHERE scenario_id='SCN-BASE' AND (receipt_delay_days<>0 OR receipt_haircut_pct<>0 OR payable_accel_days<>0 OR fx_shock_pct<>0)",
    "Variance explainable (arithmetic + reason)": "SELECT COUNT(*) FROM ForecastActualMatch WHERE ABS(actual_amount_usd-forecast_amount_usd-variance_usd)>0.02 OR variance_reason IS NULL",
}


def validate(conn):
    rows = [{"check": "Foreign key integrity", "violations": len(conn.execute("PRAGMA foreign_key_check").fetchall())}]
    rows += [{"check": k, "violations": conn.execute(v).fetchone()[0]} for k, v in CHECKS.items()]
    d = pd.DataFrame(rows)
    d["status"] = np.where(d.violations == 0, "PASS", "FAIL")
    return d
