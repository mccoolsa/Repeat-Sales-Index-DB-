"""Property lookup: public sale history and an index-adjusted estimate.

The session pins one release when it starts, so the history, estimate and chart always
come from the same release even if a newer one is published meanwhile. Reads only gold
tables, through the app's own service principal, with bound parameters.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import altair as alt
import pandas as pd
import service
import streamlit as st

# Local mode: RSI_LOCAL_DUCKDB=data/warehouse/donegal.duckdb serves the same release from the
# local warehouse (offline demos, screenshots). Otherwise the app reads the SQL warehouse.
LOCAL_DUCKDB = os.getenv("RSI_LOCAL_DUCKDB")
CATALOG = (Path(LOCAL_DUCKDB).stem if LOCAL_DUCKDB
           else os.getenv("RSI_CATALOG", "ireland_rsi_donegal"))
COUNTY = os.getenv("RSI_COUNTY") or None
GEO_LABELS = {"scope": os.getenv("RSI_SCOPE_LABEL", "Donegal")}

st.set_page_config(page_title="Property price lookup", page_icon="🏠", layout="wide")


@st.cache_resource
def connection():
    if LOCAL_DUCKDB:
        import duckdb
        return duckdb.connect(LOCAL_DUCKDB, read_only=True)
    from databricks import sql
    from databricks.sdk.core import Config
    cfg = Config()
    return sql.connect(server_hostname=cfg.host,
                       http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_WAREHOUSE_ID']}",
                       credentials_provider=lambda: cfg.authenticate)


class Warehouse:
    def query(self, statement: str, params: dict) -> pd.DataFrame:
        if LOCAL_DUCKDB:
            # DuckDB binds $name where the Databricks connector binds :name.
            return connection().execute(re.sub(r"(?<![:\w]):(\w+)", r"$\1", statement),
                                        params).df()
        with connection().cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchall_arrow().to_pandas()


DB = Warehouse()


def euro(x: float | None) -> str:
    return "-" if x is None or pd.isna(x) else f"€{x:,.0f}"


def geo_label(geo: str) -> str:
    return GEO_LABELS.get(geo, geo.split(":", 1)[-1])


# Release-aware caches: every key includes the release ID.
@st.cache_data(ttl=600, show_spinner=False)
def cached_search(release_id: str, text: str):
    return service.search_properties(DB, CATALOG, text, COUNTY, release_id)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_history(release_id: str, property_id: str):
    return service.get_history(DB, CATALOG, property_id, release_id)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_series(release_id: str, geo: str, freq: str):
    return service.index_series(DB, CATALOG, release_id, geo, freq)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_published(release_id: str):
    return service.published_series(DB, CATALOG, release_id)


def unavailable(exc: Exception) -> None:
    st.error(service.REASONS["SERVICE_UNAVAILABLE"])
    with st.expander("Details"):
        st.code(f"{type(exc).__name__}: {exc}"[:800])
    st.stop()


# --- Release (pinned for the session) --------------------------------------------------
if "release" not in st.session_state:
    try:
        st.session_state.release = service.current_release(DB, CATALOG)
    except Exception as exc:
        unavailable(exc)
release: service.Release | None = st.session_state.release
if release is None:
    st.warning("No release has been published yet.")
    st.stop()

st.title("Property price lookup")
st.caption(
    f"Release `{release.release_id}` · {release.state} · published "
    f"{release.created_at:%d %b %Y} · source snapshot `{release.snapshot_id}`. "
    "Sale facts come from the public Property Price Register; no owner, buyer or seller is "
    "identified.")
if release.state == "research":
    st.info("Research release: the property-matching threshold is still provisional, so "
            "linked sale histories and estimates may change when it is finalised.")
if release.stale():
    st.warning(service.REASONS["SOURCE_STALE"])

# --- Search --------------------------------------------------------------------------
# ?address=... pre-fills the search, so a lookup can be shared as a link.
text = st.text_input("Address", value=st.query_params.get("address", ""),
                     placeholder="e.g. 19 Ard Colmcille, Letterkenny")
if not text:
    st.stop()
t0 = time.perf_counter()
try:
    hits = cached_search(release.release_id, text)
except Exception as exc:
    unavailable(exc)
if hits.empty:
    st.warning(service.REASONS["NO_MATCH"])
    st.stop()
if len(hits) > 1:
    st.caption(f"{len(hits)} candidate properties. Search similarity is not identity: "
               "choose the right one.")
labels = [f"{r.address} · {r.locality_name or 'no town assigned'} · {r.sales} sale"
          f"{'s' if r.sales > 1 else ''} · last {str(r.last_sale)[:10]}"
          for r in hits.itertuples()]
choice = st.selectbox("Property", range(len(hits)), format_func=lambda i: labels[i])
property_id = hits.property_id.iloc[choice]

# --- History -------------------------------------------------------------------------
history = cached_history(release.release_id, property_id)
st.subheader("Sale history")
link = history.link_strength.iloc[0]
st.caption({
    "single_sale": "One recorded sale.",
    "strong": "Sales linked with a strong match.",
    "moderate": "Sales linked with a moderate match; review the addresses.",
    "weak": "Sales linked with a weak match (provisional): they may not all be one home.",
    "linked": "Sales linked by the property-matching model.",
}.get(link, ""))
show = history.assign(
    reported=history.reported_price_eur.map(euro),
    analysis=[euro(a) if b != "reported" else "same" for a, b in
              zip(history.analysis_price_eur, history.price_basis, strict=True)],
    usable=[("✅ can anchor" if ok else f"✗ {why}") for ok, why in
            zip(history.anchor_eligible, history.anchor_reason, strict=True)])
st.dataframe(
    show[["sale_date", "address", "reported", "analysis", "dwelling_status", "usable"]]
    .rename(columns={"sale_date": "Date", "address": "Address as filed",
                     "reported": "Reported price", "analysis": "VAT-adjusted price",
                     "dwelling_status": "Status", "usable": "Anchor"}),
    hide_index=True, use_container_width=True)

# --- Estimate ------------------------------------------------------------------------
eligible = history[history.anchor_eligible]
c1, c2 = st.columns(2)
anchor_options = list(history.sale_id)
default_anchor = eligible.sale_id.iloc[-1] if len(eligible) else anchor_options[-1]
anchor_id = c1.selectbox(
    "Anchor sale", anchor_options, index=anchor_options.index(default_anchor),
    format_func=lambda s: f"{history.set_index('sale_id').sale_date[s]} · "
                          f"{euro(history.set_index('sale_id').reported_price_eur[s])}")
chain = release.policy.get("estimate_chain", [])
pub = cached_published(release.release_id)
first = next((o for o in chain if ((pub.geography_id == o["geography"])
                                   & (pub.frequency == o["frequency"])
                                   & (pub.supported == 1)).any()), None)
periods = []
if first:
    s = cached_series(release.release_id, first["geography"], first["frequency"])
    periods = list(s[s.period_state != "suppressed"].period)[::-1]
target = c2.selectbox("Target period", periods or ["(none)"])
result = service.estimate(DB, CATALOG, property_id, release, anchor_id,
                          None if target == "(none)" else target)
elapsed = time.perf_counter() - t0

if result.status == "ok":
    st.subheader(result.label)
    m1, m2, m3 = st.columns(3)
    m1.metric("Estimate", euro(result.amount))
    m2.metric(f"{result.interval_level:.0%} prediction interval",
              f"{euro(result.interval_lo)} to {euro(result.interval_hi)}")
    m3.metric("Index change", f"{(result.index_ratio - 1):+.1%}",
              help=f"{result.anchor_period} → {result.target_period}")
    basis = ("reported price" if result.anchor_price_basis == "reported"
             else "VAT-adjusted analytical price")
    notes = [f"Anchor: sale on {result.anchor_date} at {euro(result.anchor_price)} ({basis}).",
             f"Index: {geo_label(result.geography_id)}, {result.frequency}."]
    if result.fallback_note:
        notes.append(result.fallback_note)
    if result.provisional:
        notes.append("The target or anchor period is provisional and may be revised.")
    notes.append("This is an index adjustment of a past sale, not a valuation. The interval "
                 f"comes from backtested errors for holding periods of {result.horizon_band}; "
                 "it is wider than the index band on the chart below.")
    st.markdown("\n".join(f"- {n}" for n in notes))
else:
    st.warning(f"**No estimate** ({result.reason_code}). {result.message}")
    if result.tried:
        with st.expander("Series considered"):
            st.write(result.tried)

# --- Index chart ---------------------------------------------------------------------
geo = result.geography_id or (first or {}).get("geography")
freq = result.frequency or (first or {}).get("frequency")
if geo and freq:
    s = cached_series(release.release_id, geo, freq)
    s = s[s.period_state != "suppressed"]
    st.subheader(f"{geo_label(geo)} index ({freq}, 2015 = 100)")
    st.caption(f"Shaded band: {release.policy.get('index_interval', 0.95):.0%} confidence "
               "interval of the index itself (not the property's prediction interval). "
               "Suppressed periods are not shown.")
    base = alt.Chart(s).encode(x=alt.X("period:O", title=None))
    band = base.mark_area(opacity=0.25).encode(y=alt.Y("level_lo:Q", title="Index"),
                                               y2="level_hi:Q")
    line = base.mark_line(point=True).encode(
        y="index_value:Q",
        tooltip=["period", alt.Tooltip("index_value:Q", format=".1f"), "endpoint_pairs",
                 "period_state"])
    st.altair_chart(band + line, use_container_width=True)

st.caption(f"Answered in {elapsed:.2f} s.")
