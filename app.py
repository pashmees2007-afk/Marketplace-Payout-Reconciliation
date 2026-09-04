"""
Streamlit dashboard for the AI Finance Controller.

Reads the outputs already produced by reconcile.py / report.py / agent.py
-- it never re-implements or re-decides any financial logic itself. The
dashboard's whole job is to make the separation between the deterministic
engine and the AI explanation layer visible:

    ORDER DATA -> DETERMINISTIC MATCHING -> FINANCIAL RESULT -> AI EXPLANATION

Run with:
    streamlit run app.py
"""

import json

import pandas as pd
import streamlit as st

import agent
import config
import report
from money import format_inr
from validation import DataValidationError, load_orders, load_payouts

st.set_page_config(page_title="AI Finance Controller", page_icon="💰", layout="wide")

STATUS_COLORS = {
    "MATCHED": "#1e7e34",       # green
    "CLOSE_MATCH": "#b8860b",   # amber
    "UNRESOLVED": "#c0392b",    # red
}
STATUS_BG = {
    "MATCHED": "#eaf7ec",
    "CLOSE_MATCH": "#fdf4e3",
    "UNRESOLVED": "#fbeaea",
}


# ---------------------------------------------------------------------------
# Data access (cached; the dashboard is a read-only view over pipeline output)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _load_pipeline_output(_cache_key: float):
    with open(config.RECONCILIATION_RESULTS_JSON) as f:
        outcome = json.load(f)
    with open(config.METRICS_JSON) as f:
        metrics = json.load(f)
    orders = load_orders(config.ORDERS_CSV)
    payouts = load_payouts(config.PAYOUTS_CSV)
    return outcome, metrics, orders, payouts


def pipeline_outputs_exist() -> bool:
    return (
        config.RECONCILIATION_RESULTS_JSON.exists()
        and config.METRICS_JSON.exists()
        and config.ORDERS_CSV.exists()
        and config.PAYOUTS_CSV.exists()
    )


def run_pipeline_now():
    import generate_data

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    orders, payouts, ground_truth = generate_data.generate()
    generate_data.write_orders_csv(orders, config.ORDERS_CSV)
    generate_data.write_payouts_csv(payouts, config.PAYOUTS_CSV)
    generate_data.write_ground_truth_json(ground_truth, config.GROUND_TRUTH_JSON)
    report.generate_report()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("💰 AI Finance Controller")
st.subheader("Marketplace Payout Reconciliation")
st.markdown(
    "A marketplace payout usually represents **many orders bundled together**, "
    "after commissions and refunds. This system answers *\"which combination of "
    "seller orders explains this payout?\"* using a **deterministic subset-sum "
    "matching engine** -- the AI layer never decides a match, it only explains "
    "the payouts the engine could not fully resolve."
)

with st.sidebar:
    st.header("Pipeline")
    st.caption(f"LLM_MODE = `{config.LLM_MODE}`")
    st.caption(f"Tolerance = {format_inr(config.TOLERANCE_PAISE)}")
    st.caption(f"Eligibility window = {config.ELIGIBILITY_WINDOW_DAYS} days")
    if st.button("🔄 Regenerate data & re-run pipeline", use_container_width=True):
        with st.spinner("Generating data and reconciling..."):
            run_pipeline_now()
        st.cache_data.clear()
        st.rerun()

if not pipeline_outputs_exist():
    st.warning(
        "No pipeline output found yet. Run `python run.py` from the terminal, "
        "or click **Regenerate data & re-run pipeline** in the sidebar."
    )
    st.stop()

try:
    outcome, metrics, orders, payouts = _load_pipeline_output(
        config.RECONCILIATION_RESULTS_JSON.stat().st_mtime
    )
except DataValidationError as exc:
    st.error(f"Data validation failed: {exc}")
    st.stop()
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load pipeline output: {exc}")
    st.stop()

results = outcome["results"]
results_by_id = {r["payout_id"]: r for r in results}
orders_by_id = {o["order_id"]: o for o in orders}

# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------

st.markdown("### Key metrics")
kpis = metrics["kpis"]
totals = metrics["totals"]
by_status = metrics["by_status"]

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Total payout volume", format_inr(int(round(totals["total_payout_value"] * 100))))
c2.metric("Match rate by value", f"{kpis['match_rate_by_value']:.1%}" if kpis["match_rate_by_value"] is not None else "n/a")
c3.metric("Exact matches", by_status["MATCHED"]["count"])
c4.metric("Close matches", by_status["CLOSE_MATCH"]["count"])
c5.metric("Unresolved", by_status["UNRESOLVED"]["count"])
c6.metric("Orphaned orders", totals["orphaned_order_count"])

if "ground_truth_validation" in metrics:
    gtv = metrics["ground_truth_validation"]
    st.caption(
        f"Ground-truth validation (measured against the synthetic generator's known answer, "
        f"not self-reported): **{gtv['overall_accuracy']:.1%}** payout classification accuracy "
        f"({gtv['payouts_correct']}/{gtv['payouts_evaluated']}), orphaned-order recall "
        f"**{gtv['orphaned_order_recall']:.1%}**, precision **{gtv['orphaned_order_precision']:.1%}**."
    )

st.divider()

# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------

st.markdown("### Ask about a payout")
st.caption("e.g. \"Why doesn't payout PYT-00042 fully match?\" -- the AI only narrates the "
           "reconciliation engine's evidence, it cannot invent orders or amounts.")
qa_col1, qa_col2 = st.columns([5, 1])
with qa_col1:
    question = st.text_input("Question", label_visibility="collapsed",
                              placeholder="Why doesn't payout PYT-00042 fully match?")
with qa_col2:
    ask = st.button("Ask", use_container_width=True)

if ask and question:
    answer = agent.answer_question(question, results_by_id)
    if not answer["ok"]:
        st.info(answer["message"])
    else:
        st.markdown(f"**FACT:** {answer['fact']}")
        if answer["explanation"]:
            st.markdown(f"**AI-generated explanation** _(hypothesis, not financial truth)_ "
                        f"({answer['mode']}): {answer['explanation']}")
        else:
            st.success("This payout is fully matched -- no exception to explain.")

st.divider()

# ---------------------------------------------------------------------------
# Payout table
# ---------------------------------------------------------------------------

st.markdown("### Payouts")

filter_col1, filter_col2 = st.columns([1, 1])
with filter_col1:
    status_filter = st.multiselect(
        "Status", ["MATCHED", "CLOSE_MATCH", "UNRESOLVED"],
        default=["MATCHED", "CLOSE_MATCH", "UNRESOLVED"],
    )
with filter_col2:
    seller_options = sorted({r["seller_id"] for r in results})
    seller_filter = st.multiselect("Seller", seller_options, default=[])

filtered = [
    r for r in results
    if r["status"] in status_filter
    and (not seller_filter or r["seller_id"] in seller_filter)
]

table_df = pd.DataFrame([
    {
        "Payout ID": r["payout_id"],
        "Seller": r["seller_id"],
        "Date": r["payout_date"],
        "Amount": r["payout_amount"],
        "Status": r["status"],
        "Matched Amount": r["matched_total"],
        "Delta": r["delta"],
    }
    for r in filtered
])

if table_df.empty:
    st.info("No payouts match the current filters.")
else:
    def _style_status(row):
        color = STATUS_BG.get(row["Status"], "#ffffff")
        return [f"background-color: {color}"] * len(row)

    styled = table_df.style.apply(_style_status, axis=1).format({
        "Amount": "₹{:.2f}", "Matched Amount": "₹{:.2f}", "Delta": "₹{:.2f}",
    })
    st.dataframe(styled, use_container_width=True, height=380)

st.divider()

# ---------------------------------------------------------------------------
# Payout detail drill-down
# ---------------------------------------------------------------------------

st.markdown("### Payout details")
payout_ids = [r["payout_id"] for r in filtered] or [r["payout_id"] for r in results]
selected_id = st.selectbox("Select a payout", payout_ids)

if selected_id:
    r = results_by_id[selected_id]
    status = r["status"]

    badge = f"<span style='background-color:{STATUS_COLORS[status]};color:white;padding:3px 10px;border-radius:12px;font-weight:600;'>{status}</span>"
    st.markdown(f"**{r['payout_id']}** &nbsp; {badge} &nbsp; Seller: `{r['seller_id']}` &nbsp; "
                f"Date: {r['payout_date']} &nbsp; UTR: `{r['utr']}`", unsafe_allow_html=True)

    d1, d2, d3 = st.columns(3)
    d1.metric("Payout amount", f"₹{r['payout_amount']:.2f}")
    d2.metric("Matched order total", f"₹{r['matched_total']:.2f}")
    d3.metric("Delta", f"₹{r['delta']:.2f}" if r["delta"] is not None else "n/a")

    st.markdown("**Orders involved**")
    if r["matched_orders"]:
        orders_df = pd.DataFrame(r["matched_orders"])[
            ["order_id", "order_date", "order_amount", "commission_amount", "refund_amount", "net_payable"]
        ]
        orders_df.columns = ["Order ID", "Order Date", "Order Amount", "Commission", "Refund", "Net Payable"]
        st.dataframe(orders_df, use_container_width=True, hide_index=True)
    else:
        st.info("No orders were matched to this payout.")

    if status != "MATCHED":
        with st.expander(f"Eligible orders considered ({len(r['eligible_order_ids'])})"):
            st.write(", ".join(r["eligible_order_ids"]) or "(none eligible)")

        st.markdown("#### AI-generated explanation")
        st.caption("Labeled explicitly: this is a hypothesis from the AI layer, not financial "
                   "truth. The match/no-match decision above was made entirely by the "
                   "deterministic reconciliation engine, before the AI ever saw this payout.")
        explanation = agent.explain_payout(r)
        st.markdown(f"**FACT:** {explanation['fact']}")
        st.info(f"**{explanation['explanation']}**\n\n_(mode: {explanation['mode']}"
                f"{', model: ' + explanation['model'] if explanation['model'] else ''})_")
    else:
        st.success("Exact match -- fully explained by the deterministic engine. No AI call was made.")

st.divider()
st.caption(
    "AI Finance Controller — Marketplace Payout Reconciliation. "
    "The reconciliation engine (reconcile.py) is 100% deterministic and never calls an LLM. "
    "The AI layer (agent.py) only narrates payouts the engine could not fully explain."
)
