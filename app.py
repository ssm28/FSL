"""Streamlit frontend. Run: streamlit run app.py   (all data logic lives in engine.py / generator.py)"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

import engine as E
import generator as G

st.set_page_config(page_title="Liquidity Forecast MVP", layout="wide")
DS = st.sidebar.selectbox("Dataset", list(G.PROFILES), index=1)  # out/<dataset>/liquidity.db
DB = f"{G.OUT}/{DS}/liquidity.db"
GT = Path(DB).parent / "ground_truth.csv"


def call(fn, *a):
    c = E.connect(DB)
    try:
        return fn(c, *a)
    finally:
        c.close()


def Q(sql, p=()):
    return call(E.q, sql, p)


def mt():  # (dataset, DB modification time) -> cache key, so caches refresh after any write
    return (DB, Path(DB).stat().st_mtime if Path(DB).exists() else 0)


@st.cache_data(show_spinner=False)
def path_of(run_id, stamp):
    return call(E.daily_path, run_id)


@st.cache_data(show_spinner=False)
def detect_cached(stamp):
    return call(E.detect)


@st.cache_data(show_spinner=False)
def validate_cached(stamp):
    return call(E.validate)


def scale_form(k):
    p, c = G.PROFILES[DS], st.columns(4)
    return (int(c[0].number_input("Entities", 1, 100, p["entities"], key=k + DS + "1")), int(c[1].number_input("Bank accounts", 1, 500, p["accounts"], key=k + DS + "2")),
            int(c[2].number_input("Ledger entries", 500, 500000, p["ledger"], step=500, key=k + DS + "3")), int(c[3].number_input("Seed", value=20261022, key=k + DS + "4")))


def regenerate(e, a, l, s):
    with st.spinner("Generating (full scale takes ~1 min)..."):
        G.build(DS, s, log=lambda *x: None, entities=e, accounts=a, ledger=l)
    st.cache_data.clear()
    st.rerun()


st.title("Short-term Liquidity Forecast")
if not Path(DB).exists():
    st.warning(f"No {DS} dataset yet. Run `python generator.py` (builds both), or generate this one here.")
    e, a, l, s = scale_form("first")
    if st.button("Generate data", type="primary"):
        regenerate(e, a, l, s)
    st.stop()

role = st.sidebar.selectbox("Role", ["Operations Analyst", "Risk / Compliance Reviewer", "System Administrator", "Account Holder"])
st.sidebar.caption("Role only changes which tabs are shown (MVP, no login).")
cnt = Q("SELECT (SELECT COUNT(*) FROM LegalEntity) e,(SELECT COUNT(*) FROM BankAccount) a,(SELECT COUNT(*) FROM LedgerEntry) l,"
        "(SELECT COUNT(*) FROM CashFlowForecast) f,(SELECT COUNT(*) FROM ForecastRun) r").iloc[0]
for col, (lab, v) in zip(st.columns(5), zip(["Entities", "Bank accounts", "Ledger entries", "Forecast items", "Forecast runs"], cnt)):
    col.metric(lab, f"{int(v):,}")


def forecast_tab():
    runs = Q("SELECT * FROM ForecastRun ORDER BY as_of_date,scenario_id,version")
    dates = sorted(runs.as_of_date.unique())
    a = st.selectbox("As-of date", dates, index=len(dates) - 1)
    act = runs[(runs.as_of_date == a) & (runs.status == "ACTIVE")]
    st.subheader("Consolidated projected cash (USD) by scenario")
    st.line_chart(pd.DataFrame({r.scenario_id: path_of(r.run_id, mt()).set_index("date").balance_usd for r in act.itertuples()}))
    st.dataframe(act.drop(columns=["status"]), hide_index=True)
    with st.expander("Scenario assumptions"):
        st.dataframe(Q("SELECT * FROM LiquidityScenario"), hide_index=True)
    rid = st.selectbox("Account-level shortfalls for run", act.run_id.tolist())
    s = call(E.account_shortfalls, rid)
    s = s[s.min_projected_balance < 0]
    st.write(f"**{len(s)}** accounts projected below zero (native currency) in this run.")
    st.dataframe(s.head(50), hide_index=True)
    with st.expander("Run a new forecast version"):
        lo, hi = Q("SELECT MIN(opening_date) lo,MAX(statement_date) hi FROM BankAccount").iloc[0]
        c = st.columns(3)
        d = c[0].date_input("As-of date", pd.Timestamp(hi).date(), (pd.Timestamp(lo) + pd.Timedelta(days=1)).date(), pd.Timestamp(hi).date())
        sc = c[1].selectbox("Scenario", Q("SELECT scenario_id FROM LiquidityScenario").scenario_id.tolist())
        h = c[2].slider("Horizon (days)", 7, 90, 30)
        if st.button("Run forecast"):
            con = E.connect(DB)
            new = E.run_forecast(con, str(d), sc, h)
            n = E.compare_run(con, new)
            con.close()
            st.cache_data.clear()
            st.success(f"Created {new} (older versions kept, marked SUPERSEDED). Actual-vs-forecast rows stored: {n}")


def avf_tab():
    s = Q("SELECT r.run_id,r.as_of_date,r.scenario_id,r.version,COUNT(*) items,ROUND(SUM(m.forecast_amount_usd)) forecast_usd,"
          "ROUND(SUM(m.actual_amount_usd)) actual_usd,ROUND(SUM(m.variance_usd)) variance_usd FROM ForecastActualMatch m "
          "JOIN ForecastRun r USING(run_id) GROUP BY r.run_id ORDER BY 2,3")
    if s.empty:
        st.info("No run has actuals covering its horizon yet.")
        return
    s["net_error_pct"] = (100 * s.variance_usd / s.forecast_usd.abs()).round(1)
    st.subheader("Forecast vs actual by run")
    st.dataframe(s, hide_index=True)
    rid = st.selectbox("Explain variance for run", s.run_id)
    st.subheader("Variance explained by reason")
    st.dataframe(Q("SELECT variance_reason,COUNT(*) items,ROUND(SUM(forecast_amount_usd)) forecast_usd,ROUND(SUM(actual_amount_usd)) actual_usd,"
                   "ROUND(SUM(variance_usd)) variance_usd FROM ForecastActualMatch WHERE run_id=? GROUP BY 1", (rid,)), hide_index=True)
    st.subheader("Largest item variances")
    st.dataframe(Q("SELECT m.match_id,f.source_id,f.currency,m.forecast_amount_usd,m.actual_amount_usd,m.variance_usd,m.variance_reason "
                   "FROM ForecastActualMatch m JOIN CashFlowForecast f USING(cf_id) WHERE m.run_id=? ORDER BY ABS(m.variance_usd) DESC LIMIT 100", (rid,)), hide_index=True)


def entity_tab():
    ents = Q("SELECT entity_id,legal_name FROM LegalEntity")
    names = dict(zip(ents.entity_id, ents.legal_name))
    e = st.selectbox("Legal entity", ents.entity_id, format_func=lambda x: f"{x} - {names[x]}")
    acc = Q("SELECT b.account_id,b.account_type,b.currency,b.opening_balance,p.closing_balance,p.closing_balance_usd,b.statement_closing_balance "
            "FROM BankAccount b JOIN DailyCashPosition p ON p.account_id=b.account_id AND p.position_date=b.statement_date WHERE b.entity_id=?", (e,))
    st.metric("Consolidated closing balance (USD, at cutoff)", f"{acc.closing_balance_usd.sum():,.0f}")
    st.dataframe(acc, hide_index=True)
    a = st.selectbox("Account", acc.account_id)
    st.line_chart(Q("SELECT position_date,closing_balance FROM DailyCashPosition WHERE account_id=? ORDER BY 1", (a,)).set_index("position_date"))


def exceptions_tab():
    det = detect_cached(mt())
    st.subheader("Detected exceptions (rule-based, no ground truth used)")
    st.bar_chart(det.groupby("issue_type").size())
    t = st.selectbox("Issue type", E.ANOM)
    ids = det[det.issue_type == t].record_id.tolist()
    st.dataframe(det[det.issue_type == t].head(300), hide_index=True)
    if ids:
        with st.form("review"):
            c = st.columns(3)
            rec = c[0].selectbox("Record", ids[:1000])
            dec = c[1].selectbox("Decision", ["ACCEPT_AS_IS", "EXCLUDE_FROM_FORECAST", "ESCALATE", "CORRECTED_AT_SOURCE"])
            who = c[2].text_input("Reviewer", "reviewer1")
            note = st.text_input("Note")
            if st.form_submit_button("Record decision"):
                con = E.connect(DB)
                n = E.next_n(con, "ReviewDecision", "decision_id", "REV-")
                con.execute("INSERT INTO ReviewDecision VALUES(?,?,?,?,?,?,?)", (f"REV-{n:06d}", t, rec, dec, who, note,
                                                                                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")))
                con.commit()
                con.close()
                st.success("Decision stored (append-only).")
    st.subheader("Review decisions")
    st.dataframe(Q("SELECT * FROM ReviewDecision ORDER BY decision_id DESC LIMIT 50"), hide_index=True)


def quality_tab():
    st.subheader("Validation checks (0 violations = pass)")
    st.dataframe(validate_cached(mt()), hide_index=True)
    st.subheader("Detection quality vs ground truth")
    if GT.exists():
        st.dataframe(E.evaluate(detect_cached(mt()), pd.read_csv(GT)), hide_index=True)
        st.caption("ground_truth.csv is a separate file, used only here for scoring.")
    else:
        st.info("ground_truth.csv not found.")


def admin_tab():
    st.subheader("Regenerate dataset (replaces all data, including review decisions)")
    e, a, l, s = scale_form("adm")
    if st.button("Regenerate", type="primary"):
        regenerate(e, a, l, s)
    c = st.columns(2)
    if c[0].button("Export CSV / SQL / manifest"):
        st.success(f"Exported to {G.export(DB, Path(DB).parent, dict(entities=e, accounts=a, ledger=l, seed=s))}")
    if c[1].button("Reproducibility check (2 runs)"):
        with st.spinner("Generating twice..."):
            same, h = G.repro(entities=e, accounts=a, ledger=l, seed=s)
        st.success("Identical checksums on both runs") if same else st.error("Not reproducible")
        st.json(h)


def explorer_tab():
    t = st.selectbox("Table", Q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1").name)
    st.caption(f"{Q(f'SELECT COUNT(*) n FROM {t}').n[0]:,} rows (first 500 shown)")
    st.dataframe(Q(f"SELECT * FROM {t} LIMIT 500"), hide_index=True)


TABS = {"Forecast": forecast_tab, "Actual vs Forecast": avf_tab, "Entity view": entity_tab, "Exceptions": exceptions_tab,
        "Data quality": quality_tab, "Data admin": admin_tab, "Explorer": explorer_tab}
ROLES = {"Operations Analyst": ["Forecast", "Actual vs Forecast", "Entity view"],
         "Risk / Compliance Reviewer": ["Exceptions", "Data quality", "Forecast"],
         "System Administrator": ["Data admin", "Data quality", "Explorer"],
         "Account Holder": ["Entity view"]}
names = ROLES[role]
for tab, n in zip(st.tabs(names), names):
    with tab:
        TABS[n]()
