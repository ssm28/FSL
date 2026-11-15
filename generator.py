"""Synthetic data generator. Same code for starter and full scale (parameters only).
  python generator.py                       -> full dataset (30 ent / 150 acc / 100k ledger), seed 20261022
  python generator.py --entities 5 --accounts 25 --ledger 3000   -> starter dataset
  python generator.py --export              -> also CSV + SQL seed + manifest in data/export
  python generator.py --repro               -> generate twice, compare row counts + checksums
"""
import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

import engine as E

CCY = ["USD", "EUR", "GBP", "INR", "JPY"]
COUNTRY = {"USD": "US", "EUR": "DE", "GBP": "GB", "INR": "IN", "JPY": "JP"}
BASE_FX = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012, "JPY": 0.0067}  # USD per unit at start
VOL = {"EUR": 0.004, "GBP": 0.005, "INR": 0.002, "JPY": 0.006}
DRIFT = {"EUR": 0.0, "GBP": 0.0, "INR": -0.0003, "JPY": -0.0004}  # documented FX movement (depreciation)
TYPES = ["OPERATING", "COLLECTION", "PAYROLL", "TAX", "RESERVE"]
BANKS = ["Atlas Bank", "Meridian Bank", "Northwind Bank", "Sunrise Bank"]
START, CUTOFF, END = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-30"), pd.Timestamp("2026-08-29")
AS_OFS = [("2026-04-01", 30), ("2026-05-01", 30), ("2026-05-31", 30), ("2026-06-30", 60)]  # (as_of, horizon)
SCN = [("SCN-BASE", "Base", "Contractual dates, no adjustments", 0, 0.0, 0, 0.0),
       ("SCN-DELAYED", "Delayed receipts", "All receipts arrive 14 days later than expected", 14, 0.0, 0, 0.0),
       ("SCN-STRESS", "Stress", "Receipts +21 days and 10% haircut, payables 7 days earlier, non-USD -10% vs USD", 21, 10.0, 7, -10.0)]
DECISION = {"BALANCE_MISMATCH": "INVESTIGATE_AND_RECONCILE", "FX_RATE_MISSING": "USE_FALLBACK_RATE_AND_FLAG",
            "DUPLICATE_FORECAST": "EXCLUDE_DUPLICATE", "FORECAST_USES_ACTUAL": "REJECT_ROW_FUTURE_INFORMATION",
            "CURRENCY_MIXED": "RECONVERT_AT_AS_OF_RATE", "DELAYED_RECEIVABLE": "MONITOR_COLLECTION",
            "LARGE_ONE_OFF_PAYMENT": "RESERVE_LIQUIDITY_BUFFER"}


def sched(kind, n, pool, rng):
    """Receivables ('R') or payables ('P'). Delay outcomes follow documented rules, not free randomness."""
    sub = pool.iloc[rng.integers(0, len(pool), n)].reset_index(drop=True)
    exp = START + pd.to_timedelta(rng.integers(0, (END - START).days + 1, n), unit="D")
    cre = exp - pd.to_timedelta(rng.integers(15, 46, n), unit="D")
    usd = rng.lognormal(np.log(8000), 0.8, n)
    d = pd.DataFrame({"entity_id": sub.entity_id, "account_id": sub.account_id, "currency": sub.currency,
                      "created_date": cre, "expected_date": exp})
    if kind == "R":
        rating = rng.choice(["A", "B", "C"], n, p=[.5, .35, .15])
        delay = np.where(rating == "A", rng.integers(0, 3, n), np.where(rating == "B", rng.integers(0, 13, n), rng.integers(8, 46, n)))
        delay = delay + np.where(usd > np.quantile(usd, .9), 5, 0)  # rule: top-10% invoices pay 5 days later
        d["counterparty"] = [f"CUST-{k:03d}" for k in rng.integers(1, 201, n)]
        d["counterparty_rating"] = rating
    else:
        delay = np.where(rng.random(n) < .15, rng.integers(1, 6, n), 0)  # rule: 15% of vendors paid 1-5 days late
        one = rng.random(n) < .015  # rule: 1.5% are large one-off payments (5-12x typical)
        usd = usd * np.where(one, rng.uniform(5, 12, n), 1)
        d["vendor"] = [f"VEND-{k:03d}" for k in rng.integers(1, 151, n)]
        d["category"] = np.where(one, rng.choice(["CAPEX", "TAX"], n), rng.choice(["RENT", "PAYROLL", "SUPPLIER", "UTILITIES"], n))
        d["is_one_off"] = one.astype(int)
    d["amount"] = np.round(usd / d.currency.map(BASE_FX).values, 2)
    act = d.expected_date + pd.to_timedelta(delay, unit="D")
    ok = act <= CUTOFF  # actuals only exist up to the cutoff
    d["actual_date"] = act.where(ok)
    d["delay_days"] = pd.Series(delay, index=d.index).where(ok)
    d["status"] = np.select([ok, d.expected_date < CUTOFF], ["PAID", "OVERDUE"], "OPEN")
    d = d.sort_values("expected_date", kind="stable").reset_index(drop=True)
    pre, pk = ("REC", "receivable_id") if kind == "R" else ("PAY", "payable_id")
    d.insert(0, pk, [f"{pre}-2026-{i + 1:06d}" for i in range(n)])
    return d


def events(df, pk, rt):
    """OPEN on creation; OVERDUE the day after expected_date if late; PAID on actual_date."""
    paid = df.actual_date.notna()
    late = (paid & (df.actual_date > df.expected_date)) | (~paid & (df.expected_date < CUTOFF))
    mk = lambda ids, frm, to, dt, o: pd.DataFrame({"record_type": rt, "record_id": ids.values, "from_status": frm,
                                                   "to_status": to, "event_date": dt.values, "o": o})
    return pd.concat([mk(df[pk], None, "OPEN", df.created_date, 0),
                      mk(df[pk][late], "OPEN", "OVERDUE", df.expected_date[late] + pd.Timedelta(days=1), 1),
                      mk(df[pk][paid], np.where(late[paid], "OVERDUE", "OPEN"), "PAID", df.actual_date[paid], 2)])


def generate(db=E.DB, entities=30, accounts=150, ledger=100000, seed=20261022, log=print):
    accounts = max(accounts, entities)
    rng = np.random.default_rng(seed)
    conn = E.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        conn.execute(f"DROP TABLE {t}")
    conn.executescript(E.SCHEMA.read_text())
    conn.execute("PRAGMA foreign_keys=ON")
    gt = []  # ground truth rows: (table, record_id, issue_type, description)

    # 1) master data -------------------------------------------------------------------
    log("master data ...")
    ids = [f"ENT-{i:06d}" for i in range(1, entities + 1)]
    base = rng.choice(CCY, entities, p=[.35, .2, .15, .2, .1])
    E.write(conn, "LegalEntity", pd.DataFrame({"entity_id": ids, "legal_name": [f"Entity {i:02d} Holdings" for i in range(1, entities + 1)],
                                               "country": [COUNTRY[c] for c in base], "base_currency": base, "status": "ACTIVE",
                                               "effective_from": START, "effective_to": None}))
    acc = []
    for i, eid in enumerate(ids):
        for j in range(accounts // entities + (1 if i < accounts % entities else 0)):
            ccy = str(base[i]) if j == 0 else str(rng.choice(CCY))
            typ = TYPES[j % 5]
            usd = rng.lognormal(np.log(1_000_000 * (2 if typ == "PAYROLL" else 1)), 0.5)
            acc.append({"account_id": f"BANKACC-{eid}-{j + 1:03d}", "entity_id": eid, "currency": ccy, "account_type": typ,
                        "bank_name": str(rng.choice(BANKS)), "opening_date": START, "opening_balance": round(usd / BASE_FX[ccy], 2),
                        "statement_date": CUTOFF, "statement_closing_balance": None, "status": "ACTIVE"})
    acc = pd.DataFrame(acc)
    E.write(conn, "BankAccount", acc)

    days = pd.date_range(START, CUTOFF)
    rates = []
    for c in CCY:
        path = np.ones(len(days)) if c == "USD" else BASE_FX[c] * np.exp(np.cumsum(rng.normal(DRIFT[c], VOL[c], len(days))))
        rates.append(pd.DataFrame({"rate_id": [f"FX-{d:%Y-%m-%d}-{c}" for d in days], "rate_date": days, "currency": c,
                                   "usd_per_unit": np.round(path, 8), "source": "SYNTHETIC"}))
    rates = pd.concat(rates, ignore_index=True)
    miss = {("EUR", "2026-05-01"), ("JPY", "2026-05-31"), ("INR", "2026-06-30")}  # edge case: FX rate missing (on forecast as-of dates)
    while len(miss) < 6:
        miss.add((str(rng.choice(["EUR", "GBP", "INR", "JPY"])), days[rng.integers(1, len(days) - 1)].strftime("%Y-%m-%d")))
    for c, d in sorted(miss):
        rid = f"FX-{d}-{c}"
        rates = rates[rates.rate_id != rid]
        gt.append(("CurrencyRate", rid, "FX_RATE_MISSING", f"{c} rate for {d} deliberately removed"))
    E.write(conn, "CurrencyRate", rates)
    E.write(conn, "LiquidityScenario", pd.DataFrame(SCN, columns=["scenario_id", "name", "description", "receipt_delay_days",
                                                                  "receipt_haircut_pct", "payable_accel_days", "fx_shock_pct"]))

    # 2) receivables / payables ---------------------------------------------------------
    log("receivables / payables ...")
    n = int(ledger * 0.075)
    pool = lambda T: acc[acc.account_type.isin(T)] if acc.account_type.isin(T).any() else acc
    rec, pay = sched("R", n, pool(["OPERATING", "COLLECTION"]), rng), sched("P", n, pool(["OPERATING", "PAYROLL"]), rng)
    E.write(conn, "Receivable", rec)
    E.write(conn, "Payable", pay)
    ev = pd.concat([events(rec, "receivable_id", "RECEIVABLE"), events(pay, "payable_id", "PAYABLE")])
    ev = ev.sort_values(["event_date", "record_id", "o"], kind="stable").reset_index(drop=True)
    ev.insert(0, "event_id", [f"EVT-{i + 1:06d}" for i in range(len(ev))])
    E.write(conn, "StatusEvent", ev.drop(columns="o"))
    gt += [("Receivable", r, "DELAYED_RECEIVABLE", "settled more than 14 days late") for r in rec[rec.delay_days > 14].receivable_id]
    gt += [("Payable", r, "LARGE_ONE_OFF_PAYMENT", "one-off payment 5-12x typical size") for r in pay[pay.is_one_off == 1].payable_id]

    # 3) ledger: settled items + background activity ------------------------------------
    log("ledger ...")
    s, p = rec[rec.actual_date.notna()], pay[pay.actual_date.notna()]
    parts = [pd.DataFrame({"account_id": s.account_id.values, "entry_date": s.actual_date.values, "amount": s.amount.values,
                           "currency": s.currency.values, "entry_type": "RECEIPT", "ref_type": "RECEIVABLE", "ref_id": s.receivable_id.values}),
             pd.DataFrame({"account_id": p.account_id.values, "entry_date": p.actual_date.values, "amount": -p.amount.values,
                           "currency": p.currency.values, "entry_type": "PAYMENT", "ref_type": "PAYABLE", "ref_id": p.payable_id.values})]
    nm = max(0, ledger - len(s) - len(p))
    t = rng.choice(["OPEX", "CARD_RECEIPT", "BANK_FEE", "INTEREST", "TRANSFER"], nm, p=[.45, .35, .10, .05, .05])
    usd = np.where(t == "BANK_FEE", rng.uniform(5, 60, nm), np.where(t == "INTEREST", rng.uniform(10, 400, nm), rng.lognormal(np.log(1500), .8, nm)))
    sign = np.where(np.isin(t, ["OPEX", "BANK_FEE"]), -1, np.where(t == "TRANSFER", rng.choice([-1, 1], nm), 1))
    a = acc.iloc[rng.integers(0, len(acc), nm)]
    parts.append(pd.DataFrame({"account_id": a.account_id.values,
                               "entry_date": (START + pd.to_timedelta(rng.integers(0, (CUTOFF - START).days + 1, nm), unit="D")).values,
                               "amount": np.round(sign * usd / a.currency.map(BASE_FX).values, 2), "currency": a.currency.values,
                               "entry_type": t, "ref_type": None, "ref_id": None}))
    led = pd.concat(parts, ignore_index=True).sort_values(["entry_date", "account_id", "entry_type", "amount"], kind="stable").reset_index(drop=True)
    led.insert(0, "ledger_id", [f"LED-{d.year}-{i + 1:08d}" for i, d in enumerate(led.entry_date)])
    led["description"] = led.entry_type + np.where(led.ref_id.notna(), " " + led.ref_id.fillna(""), "")
    E.write(conn, "LedgerEntry", led)
    conn.commit()

    # 4) derived: daily cash positions + statement balance (edge case: balance mismatch)
    log("daily positions ...")
    E.build_positions(conn)
    pos = E.q(conn, "SELECT account_id,closing_balance FROM DailyCashPosition WHERE position_date=?", (CUTOFF.strftime("%Y-%m-%d"),))
    bad = set(rng.choice(pos.account_id.values, min(3, len(pos)), replace=False))
    off = dict(zip(sorted(bad), rng.choice([-1, 1], len(bad)) * rng.uniform(500, 5000, len(bad))))
    for aid, cb in zip(pos.account_id, pos.closing_balance):
        conn.execute("UPDATE BankAccount SET statement_closing_balance=? WHERE account_id=?", (round(cb + off.get(aid, 0.0), 2), aid))
        if aid in off:
            gt.append(("BankAccount", aid, "BALANCE_MISMATCH", f"statement balance differs from opening+ledger by {off[aid]:.2f}"))
    conn.commit()

    # 5) forecast runs: 4 as-of dates x 3 scenarios, then actual-vs-forecast ---------------
    log("forecast runs ...")
    runs = [E.run_forecast(conn, d, sc[0], h, created_at=f"{d}T08:00:00") for d, h in AS_OFS for sc in SCN]
    for r in runs:
        E.compare_run(conn, r)

    # 6) inject labelled forecast anomalies (disjoint record sets) -------------------------
    log("injecting edge cases ...")
    F = E.q(conn, "SELECT f.*,r.as_of_date,r.horizon_days FROM CashFlowForecast f JOIN ForecastRun r USING(run_id) ORDER BY cf_id")
    actual = E.q(conn, "SELECT receivable_id id,actual_date FROM Receivable UNION ALL SELECT payable_id,actual_date FROM Payable")
    F = F.merge(actual, left_on="source_id", right_on="id", how="left")
    k = max(2, int(len(F) * 0.002))
    ad, ao = pd.to_datetime(F.actual_date), pd.to_datetime(F.as_of_date)
    cand = F[ad.notna() & (ad > ao) & (ad <= ao + pd.to_timedelta(F.horizon_days, unit="D"))]
    leak = rng.choice(cand.cf_id.values, min(k, len(cand)), replace=False)
    for cid, d in zip(leak, F.set_index("cf_id").loc[leak, "actual_date"]):
        conn.execute("UPDATE CashFlowForecast SET forecast_date=?,actual_date_used=? WHERE cf_id=?", (d, d, cid))
        gt.append(("CashFlowForecast", cid, "FORECAST_USES_ACTUAL", "forecast_date taken from an actual settlement after the as-of date"))
    rest = F[~F.cf_id.isin(leak)]
    dup = rng.choice(rest.cf_id.values, min(k, len(rest)), replace=False)
    c = rest[rest.cf_id.isin(dup)][E.q(conn, "SELECT * FROM CashFlowForecast LIMIT 0").columns].copy()
    n0 = E.next_n(conn, "CashFlowForecast", "cf_id", "CF-2026-")
    c["cf_id"] = [f"CF-2026-{n0 + i:06d}" for i in range(len(c))]
    E.write(conn, "CashFlowForecast", c)
    gt += [("CashFlowForecast", cid, "DUPLICATE_FORECAST", "same source item forecast twice in one run") for cid in c.cf_id]
    rest = rest[(rest.currency != "USD") & ~rest.cf_id.isin(dup)]
    mix = rng.choice(rest.cf_id.values, min(k, len(rest)), replace=False)
    for cid in mix:
        conn.execute("UPDATE CashFlowForecast SET fx_rate_used=1.0,amount_usd=amount WHERE cf_id=?", (cid,))
        gt.append(("CashFlowForecast", cid, "CURRENCY_MIXED", "non-USD amount treated as USD (no conversion)"))
    conn.commit()
    for r in runs:
        E.refresh_summary(conn, r)

    # 7) ground truth (separate file), validation, detection metrics -----------------------
    out = Path(db).parent
    g = pd.DataFrame(gt, columns=["table_name", "record_id", "issue_type", "description"])
    g.insert(0, "gt_id", [f"GT-{i + 1:06d}" for i in range(len(g))])
    g["expected_decision"] = g.issue_type.map(DECISION)
    g["is_anomaly"] = g.issue_type.isin(E.ANOM).astype(int)
    g.to_csv(out / "ground_truth.csv", index=False)
    val, ev_ = E.validate(conn), E.evaluate(E.detect(conn), g)
    summary = {"seed": seed, "validation": dict(zip(val.check, val.violations)), "detection": ev_.to_dict("records"),
               "rows": {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in
                        conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1").fetchall()}}
    (out / "validation_summary.json").write_text(json.dumps(summary, indent=2))
    conn.close()
    log(f"done. rows: {summary['rows']}")
    log("validation failures: " + (str(val[val.status == 'FAIL'].check.tolist()) or "none"))
    log("detection vs ground truth:\n" + ev_.to_string(index=False))
    return summary


def table_hashes(conn):
    res = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1").fetchall():
        df = E.q(conn, f"SELECT * FROM {t} ORDER BY 1,2" if t == "DailyCashPosition" else f"SELECT * FROM {t} ORDER BY 1")
        res[t] = {"rows": len(df), "sha256": hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()}
    return res


def export(db=E.DB, out="data/export", params=None):
    params = params or {}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    conn = E.connect(db)
    for t in table_hashes(conn):
        E.q(conn, f"SELECT * FROM {t}").to_csv(out / f"{t}.csv", index=False)
    (out / "seed_data.sql").write_text("\n".join(conn.iterdump()))
    cmd = "python generator.py " + " ".join(f"--{k} {v}" for k, v in params.items())
    (out / "manifest.json").write_text(json.dumps({"generator_command": cmd, "params": params, "sources": "none - fully synthetic, no external data",
                                                   "tables": table_hashes(conn)}, indent=2))
    for f in ("ground_truth.csv", "validation_summary.json"):
        shutil.copy(Path(db).parent / f, out / f)
    conn.close()
    return out


def repro(**kw):
    hs = []
    for i in range(2):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            generate(f"{t}/r{i}.db", log=lambda *a: None, **kw)
            c = E.connect(f"{t}/r{i}.db")
            hs.append(table_hashes(c))
            c.close()
    same = hs[0] == hs[1]
    print("REPRODUCIBLE: identical row counts and checksums on both runs" if same else "NOT reproducible!")
    return same, hs[0]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=E.DB)
    ap.add_argument("--entities", type=int, default=30)
    ap.add_argument("--accounts", type=int, default=150)
    ap.add_argument("--ledger", type=int, default=100000)
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--repro", action="store_true")
    a = ap.parse_args()
    kw = dict(entities=a.entities, accounts=a.accounts, ledger=a.ledger, seed=a.seed)
    if a.repro:
        repro(**kw)
    else:
        generate(a.db, **kw)
        if a.export:
            print("exported to", export(a.db, params=kw))
