"""Same property? Human labelling of candidate sale pairs.

Reads the frozen sample `silver.label_sample` (pinned by LABEL_SAMPLE_VERSION) and appends
labels to `silver.link_labels` as the signed-in reviewer. Prices and Eircodes are never
shown, so labels stay independent of the index and of the Eircode weak labels.
"""

from __future__ import annotations

import html
import os
import re
from datetime import UTC, datetime

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config

APP_VERSION = "labelling-1"
CATALOG = os.getenv("RSI_CATALOG", "ireland_rsi")
SAMPLE_VERSION = os.environ["LABEL_SAMPLE_VERSION"]
WAREHOUSE_ID = os.environ["DATABRICKS_WAREHOUSE_ID"]
LABELS = {"match": "Same property", "non_match": "Different property",
          "uncertain": "Can't tell"}

st.set_page_config(page_title="Same property?", page_icon="🏠", layout="wide")
cfg = Config()


@st.cache_resource
def connection():
    return sql.connect(server_hostname=cfg.host,
                       http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
                       credentials_provider=lambda: cfg.authenticate)


def query(statement: str, params: dict | None = None) -> pd.DataFrame:
    with connection().cursor() as cursor:
        cursor.execute(statement, params or {})
        return cursor.fetchall_arrow().to_pandas()


def execute(statement: str, params: dict) -> None:
    with connection().cursor() as cursor:
        cursor.execute(statement, params)


@st.cache_data(ttl=3600)
def load_sample(version: str) -> pd.DataFrame:
    return query(
        "SELECT pair_id, sample_order, double_review, county, date_a, date_b, address_a, "
        "address_b, status_a, status_b, size_a, size_b "
        f"FROM {CATALOG}.silver.label_sample WHERE sample_version = :v ORDER BY sample_order",
        {"v": version})


def load_my_labels(version: str, reviewer: str) -> dict[str, str]:
    df = query(
        f"SELECT pair_id, label, labelled_at FROM {CATALOG}.silver.link_labels "
        "WHERE sample_version = :v AND reviewer = :r", {"v": version, "r": reviewer})
    if df.empty:
        return {}
    latest = df.sort_values("labelled_at").groupby("pair_id").tail(1)
    return dict(zip(latest.pair_id, latest.label, strict=True))


def save_label(pair_id: str, reviewer: str, label: str, rationale: str) -> None:
    execute(
        f"INSERT INTO {CATALOG}.silver.link_labels "
        "(sample_version, pair_id, reviewer, labelled_at, label, rationale, app_version) "
        "VALUES (:v, :p, :r, :t, :l, :why, :app)",
        {"v": SAMPLE_VERSION, "p": pair_id, "r": reviewer, "t": datetime.now(UTC),
         "l": label, "why": rationale or None, "app": APP_VERSION})


def tokens(text: str) -> list[str]:
    return re.findall(r"[0-9A-Za-zÀ-ÿ]+|[^0-9A-Za-zÀ-ÿ\s]", text or "")


def highlight(text: str, other: str) -> str:
    """Escape the address and mark words that do not appear in the other address."""
    theirs = {t.casefold() for t in tokens(other)}
    out = []
    for word in re.split(r"(\s+)", text or ""):
        parts = []
        for t in tokens(word) or [word]:
            safe = html.escape(t)
            if t.strip() and t.casefold() not in theirs and t.isalnum():
                safe = f"<mark>{safe}</mark>"
            parts.append(safe)
        out.append("".join(parts) if word.strip() else html.escape(word))
    return "".join(out)


def sale_card(title: str, d, address: str, other: str, status: str, size: str | None) -> None:
    status_text = {"new": "New dwelling", "second_hand": "Second-hand dwelling"}.get(
        status, status)
    size_text = f" · size band {html.escape(size)}" if isinstance(size, str) else ""
    st.markdown(
        f"<div class='card'><div class='label'>{title} · {d:%d %b %Y}</div>"
        f"<div class='addr'>{highlight(address, other)}</div>"
        f"<div class='meta'>{status_text}{size_text}</div></div>",
        unsafe_allow_html=True)


st.markdown("""<style>
.card {border:1px solid rgba(128,128,128,.35); border-radius:10px; padding:14px 16px;}
.card .label {font-size:.85rem; opacity:.7; margin-bottom:6px;}
.card .addr {font-size:1.3rem; line-height:1.4;}
.card .meta {font-size:.85rem; opacity:.7; margin-top:8px;}
mark {background:#fde68a; color:inherit; padding:0 2px; border-radius:3px;}
</style>""", unsafe_allow_html=True)

reviewer = (st.context.headers.get("X-Forwarded-Email")
            or st.context.headers.get("X-Forwarded-Preferred-Username"))
if not reviewer:
    st.error("Could not identify the signed-in reviewer. Open this app from Databricks.")
    st.stop()

sample = load_sample(SAMPLE_VERSION)
if "mine" not in st.session_state:
    st.session_state.mine = load_my_labels(SAMPLE_VERSION, reviewer)
    st.session_state.history = []
mine: dict[str, str] = st.session_state.mine

with st.sidebar:
    st.header("Same property?")
    queue = st.radio("Queue", ["All pairs", "Second-review subset"],
                     help="A second reviewer labels only the flagged subset, to measure "
                          "agreement between reviewers.")
    pool = sample if queue == "All pairs" else sample[sample.double_review]
    done = int(pool.pair_id.isin(mine).sum())
    st.progress(done / max(len(pool), 1), text=f"{done} of {len(pool)} labelled")
    counts = pd.Series([mine[p] for p in pool.pair_id if p in mine]).value_counts()
    for key, name in LABELS.items():
        st.caption(f"{name}: {int(counts.get(key, 0))}")
    st.caption(f"Reviewer: {reviewer}")
    st.caption(f"Sample: {SAMPLE_VERSION}")
    with st.expander("How to decide"):
        st.markdown(
            "- **Same property**: the same dwelling sold twice. Spelling, case, "
            "abbreviations, a missing town or county, or Irish/English place names can differ.\n"
            "- **Different property**: different dwellings, including different apartments "
            "in the same building, different house numbers, or the same estate name in "
            "different towns.\n"
            "- **Can't tell**: the text does not settle it. This is a valid answer; "
            "don't guess.\n\n"
            "Use only what is shown. Prices and Eircodes are hidden on purpose. A new "
            "dwelling is normally sold before any second-hand sale of the same home.")

remaining = pool[~pool.pair_id.isin(mine)]
if st.session_state.get("revisit"):
    current = pool[pool.pair_id == st.session_state.revisit].iloc[0]
elif remaining.empty:
    st.success(f"All {len(pool)} pairs in this queue are labelled. Thank you!")
    st.stop()
else:
    current = remaining.iloc[0]

st.subheader("Are these the same property?")
st.caption(f"County {current.county} · pair {int(current.sample_order) + 1}"
           + (" · revisiting, previous answer: " + LABELS[mine[current.pair_id]]
              if current.pair_id in mine else ""))
left, right = st.columns(2)
with left:
    sale_card("Earlier sale", current.date_a, current.address_a, current.address_b,
              current.status_a, current.size_a)
with right:
    sale_card("Later sale", current.date_b, current.address_b, current.address_a,
              current.status_b, current.size_b)

st.text_input("Note (optional)", key=f"why_{current.pair_id}",
                          placeholder="e.g. apartment numbers differ")


def answer(label: str) -> None:
    save_label(current.pair_id, reviewer, label, st.session_state.get(f"why_{current.pair_id}"))
    mine[current.pair_id] = label
    st.session_state.history.append(current.pair_id)
    st.session_state.revisit = None


b1, b2, b3, b4 = st.columns([2, 2, 2, 1])
b1.button("✅ Same property", on_click=answer, args=("match",), use_container_width=True,
          type="primary")
b2.button("❌ Different property", on_click=answer, args=("non_match",),
          use_container_width=True)
b3.button("❓ Can't tell", on_click=answer, args=("uncertain",), use_container_width=True)
if st.session_state.history:
    b4.button("↩ Back", use_container_width=True, on_click=lambda: st.session_state.update(
        revisit=st.session_state.history.pop()))
