"""Read-only lookup service: search, sale history and index-adjusted estimates.

Every function takes an explicit `release_id`, so a session that pinned a release keeps
using it even if a newer release is published mid-session. All user input reaches SQL as
bound parameters, never as SQL text.

    estimate = anchor price * index[target period] / index[anchor period]

An estimate that cannot be supported returns a reason code and no amount; it never
returns zero or silently changes the user's chosen anchor.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

import pandas as pd

REASONS = {
    "NO_MATCH": "No property in this release matches that address.",
    "AMBIGUOUS_PROPERTY": "Several properties match; choose one.",
    "NO_ELIGIBLE_ANCHOR": "This property has no sale that can anchor an estimate.",
    "UNRESOLVED_VAT": "The chosen sale's price basis (VAT) is unresolved, so it cannot anchor "
                      "an estimate.",
    "UNSUPPORTED_PERIOD": "No published index series covers both the sale period and the "
                          "target period.",
    "DISCONNECTED_SERIES": "The sale period is not connected to the target period in any "
                           "published series.",
    "SPARSE_GEOGRAPHY": "There is not enough repeat-sales evidence here to publish an index.",
    "UNCALIBRATED_INTERVAL": "Estimates for this holding period are withheld: their "
                             "prediction interval failed its backtest.",
    "TARGET_BEFORE_ANCHOR": "The target period is before the chosen sale.",
    "SOURCE_STALE": "This release is older than the refresh policy allows; newer sales may "
                    "be missing.",
    "SERVICE_UNAVAILABLE": "The data service is unavailable. Try again shortly.",
}


class Backend(Protocol):
    def query(self, sql: str, params: dict) -> pd.DataFrame: ...


@dataclass
class Release:
    release_id: str
    state: str
    created_at: datetime
    snapshot_id: str
    policy: dict

    def stale(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        created = self.created_at if self.created_at.tzinfo else self.created_at.replace(
            tzinfo=UTC)
        return (now - created).days > self.policy.get("stale_after_days", 45)


@dataclass
class EstimateResult:
    status: str                         # "ok" or "unavailable"
    reason_code: str | None = None
    message: str | None = None
    amount: float | None = None
    interval_lo: float | None = None
    interval_hi: float | None = None
    interval_level: float | None = None
    label: str | None = None
    anchor_sale_id: str | None = None
    anchor_date: date | None = None
    anchor_price: float | None = None
    anchor_price_basis: str | None = None
    anchor_period: str | None = None
    target_period: str | None = None
    geography_id: str | None = None
    frequency: str | None = None
    fallback_level: int | None = None
    fallback_note: str | None = None
    index_ratio: float | None = None
    horizon_band: str | None = None
    provisional: bool = False
    release_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _fail(code: str, release_id: str, message: str | None = None, **extra) -> EstimateResult:
    return EstimateResult("unavailable", code, message or REASONS[code], release_id=release_id,
                          **extra)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def current_release(backend: Backend, catalog: str) -> Release | None:
    """The release the pointer selects now (latest publish or rollback event)."""
    df = backend.query(
        f"SELECT e.release_id, m.release_state, m.created_at, m.snapshot_id, "
        f"m.lookup_policy_json FROM {catalog}.gold.release_events e "
        f"JOIN {catalog}.gold.release_manifest m ON m.release_id = e.release_id "
        "WHERE e.action IN ('publish', 'rollback') "
        "ORDER BY e.event_at DESC, e.event_id DESC LIMIT 1", {})
    if df.empty:
        return None
    r = df.iloc[0]
    return Release(r.release_id, r.release_state, pd.Timestamp(r.created_at).to_pydatetime(),
                   r.snapshot_id, json.loads(r.lookup_policy_json or "{}"))


# ---------------------------------------------------------------------------
# Search and history
# ---------------------------------------------------------------------------

ABBREVIATIONS = {"co": "county", "apt": "apartment", "apts": "apartments", "rd": "road",
                 "st": "street", "ave": "avenue", "no": "", "the": ""}


def search_terms(query: str, limit: int = 6) -> list[str]:
    words = re.findall(r"[0-9a-z]+", (query or "").lower())
    out = []
    for w in words:
        w = ABBREVIATIONS.get(w, w)
        if w and w not in out:
            out.append(w)
    return out[:limit]


def search_properties(backend: Backend, catalog: str, query: str, county: str | None,
                      release_id: str, limit: int = 20) -> pd.DataFrame:
    """Candidate properties for free-text address input, best first.

    Search relevance is not identity confidence: results are choices for the user, and a
    match never merges properties or picks an anchor.
    """
    terms = search_terms(query)
    if not terms:
        return pd.DataFrame(columns=["property_id", "address", "locality_name", "sales",
                                     "last_sale", "score"])
    params: dict = {"rid": release_id}
    where = ["release_id = :rid"]
    for i, t in enumerate(terms):
        params[f"t{i}"] = f"%{t}%"
        where.append(f"address_search LIKE :t{i}")
    if county:
        params["county"] = county
        where.append("county = :county")
    df = backend.query(
        "SELECT property_id, address, address_search, locality_name, sale_date "
        f"FROM {catalog}.gold.lookup_history WHERE {' AND '.join(where)} LIMIT 500", params)
    if df.empty:
        return df.assign(sales=[], last_sale=[], score=[])
    target = " ".join(terms)
    df["score"] = [difflib.SequenceMatcher(None, target, a).ratio() for a in df.address_search]
    df = df.sort_values("sale_date")
    g = df.groupby("property_id")
    out = pd.DataFrame({
        "address": g.address.last(), "locality_name": g.locality_name.last(),
        "sales": g.size(), "last_sale": g.sale_date.max(), "score": g.score.max(),
    }).reset_index().sort_values(["score", "sales"], ascending=[False, False])
    return out.head(limit).reset_index(drop=True)


def get_history(backend: Backend, catalog: str, property_id: str, release_id: str
                ) -> pd.DataFrame:
    df = backend.query(
        "SELECT sale_id, sale_date, address, locality_name, locality_confidence, "
        "reported_price_eur, analysis_price_eur, price_basis, dwelling_status, "
        "not_full_market_price, anchor_eligible, anchor_reason, link_strength, property_sales "
        f"FROM {catalog}.gold.lookup_history "
        "WHERE release_id = :rid AND property_id = :pid ORDER BY sale_date, sale_id",
        {"rid": release_id, "pid": property_id})
    if not df.empty:
        df["sale_date"] = pd.to_datetime(df.sale_date).dt.date
    return df


def index_series(backend: Backend, catalog: str, release_id: str, geography_id: str,
                 frequency: str) -> pd.DataFrame:
    return backend.query(
        "SELECT period, index_value, level_lo, level_hi, endpoint_pairs, period_state, "
        "state_reason, series_supported, series_properties "
        f"FROM {catalog}.gold.index_vintages WHERE release_id = :rid AND geography_id = :g "
        "AND frequency = :f ORDER BY period",
        {"rid": release_id, "g": geography_id, "f": frequency})


def published_series(backend: Backend, catalog: str, release_id: str) -> pd.DataFrame:
    return backend.query(
        "SELECT geography_id, frequency, max(CAST(series_supported AS INT)) AS supported, "
        "max(series_properties) AS properties "
        f"FROM {catalog}.gold.index_vintages WHERE release_id = :rid "
        "GROUP BY geography_id, frequency ORDER BY geography_id, frequency",
        {"rid": release_id})


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------


def period_of(d: date, frequency: str) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}" if frequency == "quarterly" else str(d.year)


def period_midpoint(period: str) -> date:
    if "Q" in period:
        q = int(period[5:])
        return date(int(period[:4]), 3 * q - 1, 15)
    return date(int(period[:4]), 7, 1)


def _band(years: float, bands: pd.DataFrame) -> pd.Series | None:
    hit = bands[(bands.min_years <= years) & (years < bands.max_years)]
    return None if hit.empty else hit.iloc[0]


def estimate(backend: Backend, catalog: str, property_id: str, release: Release,
             anchor_sale_id: str | None = None, target_period: str | None = None,
             now: datetime | None = None) -> EstimateResult:
    rid = release.release_id
    history = get_history(backend, catalog, property_id, rid)
    if history.empty:
        return _fail("NO_MATCH", rid)
    warnings = ["SOURCE_STALE"] if release.stale(now) else []

    # Anchor: the user's choice is honoured or refused with its reason, never swapped.
    if anchor_sale_id:
        chosen = history[history.sale_id == anchor_sale_id]
        if chosen.empty:
            return _fail("NO_ELIGIBLE_ANCHOR", rid, anchor_sale_id=anchor_sale_id)
        anchor = chosen.iloc[0]
        if not anchor.anchor_eligible:
            code = ("UNRESOLVED_VAT" if "VAT" in str(anchor.anchor_reason)
                    else "NO_ELIGIBLE_ANCHOR")
            return _fail(code, rid, anchor_sale_id=anchor_sale_id,
                         anchor_date=anchor.sale_date,
                         message=f"{REASONS[code]} Reason: {anchor.anchor_reason}.")
    else:
        eligible = history[history.anchor_eligible & history.analysis_price_eur.notna()]
        if eligible.empty:
            return _fail("NO_ELIGIBLE_ANCHOR", rid)
        anchor = eligible.iloc[-1]

    # Series: first in the estimate chain that publishes both periods.
    chain = release.policy.get("estimate_chain", [])
    available = published_series(backend, catalog, rid)
    tried, any_supported = [], False
    for level, option in enumerate(chain):
        geo, freq = option["geography"], option["frequency"]
        row = available[(available.geography_id == geo) & (available.frequency == freq)]
        if row.empty or not int(row.supported.iloc[0]):
            tried.append(f"{geo}/{freq}: not published")
            continue
        any_supported = True
        series = index_series(backend, catalog, rid, geo, freq).set_index("period")
        shown = series[series.period_state != "suppressed"]
        a = period_of(anchor.sale_date, freq)
        t = target_period if target_period else (shown.index.max() if len(shown) else None)
        if t is not None and freq == "annual" and "Q" in t:
            t = t[:4]
        if t is None or (freq == "quarterly" and "Q" not in t):
            tried.append(f"{geo}/{freq}: target period {t} not in this frequency")
            continue
        if t < a:
            return _fail("TARGET_BEFORE_ANCHOR", rid, anchor_sale_id=anchor.sale_id,
                         anchor_period=a, target_period=t, tried=tried)
        missing = [p for p in (a, t) if p not in shown.index]
        if missing:
            tried.append(f"{geo}/{freq}: {', '.join(missing)} not published")
            continue
        ratio = float(shown.index_value[t] / shown.index_value[a])
        years = (period_midpoint(t) - anchor.sale_date).days / 365.25
        bands = backend.query(
            "SELECT horizon_band, min_years, max_years, level, q_lo, q_hi, status "
            f"FROM {catalog}.gold.release_calibration WHERE release_id = :rid", {"rid": rid})
        band = _band(max(years, 0.0), bands)
        note = None
        if level:
            first = chain[0]
            note = (f"A broader area ({geo}) was used: the first-choice series "
                    f"({first['geography']}) does not publish both periods."
                    if geo != first["geography"] else
                    f"The {freq} series was used: the {first['frequency']} series is not "
                    "published for both periods.")
        base = dict(anchor_sale_id=anchor.sale_id, anchor_date=anchor.sale_date, fallback_note=note,
                    anchor_price=float(anchor.analysis_price_eur),
                    anchor_price_basis=anchor.price_basis, anchor_period=a, target_period=t,
                    geography_id=geo, frequency=freq, fallback_level=level, index_ratio=ratio,
                    release_id=rid, warnings=warnings, tried=tried,
                    provisional=bool((shown.period_state[[a, t]] == "provisional").any()))
        if band is None or band.status != "ok" or band.q_lo is None or pd.isna(band.q_lo):
            return EstimateResult("unavailable", "UNCALIBRATED_INTERVAL",
                                  REASONS["UNCALIBRATED_INTERVAL"],
                                  horizon_band=None if band is None else band.horizon_band,
                                  **base)
        amount = float(anchor.analysis_price_eur) * ratio
        return EstimateResult(
            "ok", amount=amount, interval_lo=amount * math.exp(float(band.q_lo)),
            interval_hi=amount * math.exp(float(band.q_hi)), interval_level=float(band.level),
            label=f"Index-adjusted estimate as of {t}", horizon_band=band.horizon_band, **base)
    code = "UNSUPPORTED_PERIOD" if any_supported else "SPARSE_GEOGRAPHY"
    return _fail(code, rid, anchor_sale_id=anchor.sale_id, anchor_date=anchor.sale_date,
                 tried=tried, warnings=warnings)
