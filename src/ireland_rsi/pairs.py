"""Repeat-sale pairs from property histories.

Within each property history, sales are ordered by date and only *consecutive comparable*
sales form pairs: three comparable sales give two pairs, not three. A non-comparable sale
(non-market, unresolved VAT, flagged bulk or extreme price) breaks the history into
segments; pairs never bridge across it unless configured to.

Each pair is traceable to two source sales and gets one decision:

    included_headline           second-hand to second-hand (headline series, D-007)
    included_new_first_leg      new-build first leg (sensitivity series only)
    diagnostic_same_quarter     both sales in one quarter: zero row in a quarterly index
    excluded_zero_holding       no time elapsed
    excluded_new_second_leg     a "new" sale after an earlier sale: implausible

Short-hold resales and unusual price changes are flagged as proxies, never as renovations.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ireland_rsi.storage import sha256_hex

PAIR_COLUMNS = [
    "entity_version", "pair_id", "property_id", "segment", "sale_id_1", "sale_version_1",
    "sale_id_2", "sale_version_2", "date_1", "date_2", "quarter_1", "quarter_2",
    "price_cents_1", "price_cents_2", "status_1", "status_2", "holding_days",
    "holding_years", "log_return", "total_return", "annualised_return", "short_hold",
    "unusual_change", "decision",
]


def _decision(status_1: str, status_2: str, days: int, q1: str, q2: str) -> str:
    if days <= 0:
        return "excluded_zero_holding"
    if status_2 == "new":
        return "excluded_new_second_leg"
    if q1 == q2:
        return "diagnostic_same_quarter"
    if status_1 == "new":
        return "included_new_first_leg"
    return "included_headline"


def _unusual(log_return: float, years: float, cfg: dict) -> bool:
    if years >= cfg["unusual_min_years"]:
        return abs(log_return) / years > cfg["unusual_annual_log_change"]
    return abs(log_return) > cfg["unusual_total_log_change"]


def build_repeat_pairs(history: pd.DataFrame, cfg: dict, entity_version: str
                       ) -> tuple[pd.DataFrame, dict]:
    """Pairs from a history table with one row per sale occurrence and columns:
    property_id, sale_id, sale_version, sale_date, sale_quarter, analysis_price_cents,
    reported_price_cents, index_eligible, dwelling_status, raw_content_fp.
    """
    stats = {"properties": int(history.property_id.nunique()), "duplicate_occurrences": 0,
             "segment_breaks": 0}
    rows = []
    multi = history[history.groupby("property_id").sale_id.transform("size") > 1]
    for property_id, group in multi.groupby("property_id", sort=True):
        g = group.sort_values(["sale_date", "sale_id"])
        # Identical occurrences (same content, same day) are one transaction filed twice.
        dedup = g.drop_duplicates(["raw_content_fp", "sale_date"], keep="first")
        stats["duplicate_occurrences"] += len(g) - len(dedup)
        segment, previous = 0, None
        for rec in dedup.itertuples(index=False):
            if not rec.index_eligible:
                # A non-comparable sale ends the segment, unless bridging is configured.
                if previous is not None and not cfg["bridge_non_comparable"]:
                    stats["segment_breaks"] += 1
                    segment += 1
                    previous = None
                continue
            if previous is not None:
                p1, p2 = int(previous.analysis_price_cents), int(rec.analysis_price_cents)
                days = (rec.sale_date - previous.sale_date).days
                years = days / 365.25
                log_return = math.log(p2 / p1)
                rows.append({
                    "entity_version": entity_version,
                    "pair_id": "pair_" + sha256_hex(
                        f"{entity_version}|{previous.sale_id}|{previous.sale_version}|"
                        f"{rec.sale_id}|{rec.sale_version}".encode())[:16],
                    "property_id": property_id, "segment": segment,
                    "sale_id_1": previous.sale_id, "sale_version_1": int(previous.sale_version),
                    "sale_id_2": rec.sale_id, "sale_version_2": int(rec.sale_version),
                    "date_1": previous.sale_date, "date_2": rec.sale_date,
                    "quarter_1": previous.sale_quarter, "quarter_2": rec.sale_quarter,
                    "price_cents_1": p1, "price_cents_2": p2,
                    "status_1": previous.dwelling_status, "status_2": rec.dwelling_status,
                    "holding_days": days, "holding_years": years, "log_return": log_return,
                    "total_return": p2 / p1 - 1,
                    "annualised_return": math.exp(log_return / years) - 1 if years > 0 else None,
                    "short_hold": years < cfg["short_hold_years"],
                    "unusual_change": _unusual(log_return, years, cfg) if days > 0 else False,
                    "decision": _decision(previous.dwelling_status, rec.dwelling_status, days,
                                          previous.sale_quarter, rec.sale_quarter),
                })
            previous = rec
    pairs = pd.DataFrame(rows, columns=PAIR_COLUMNS)
    return pairs, {**stats, **pair_summary(pairs)}


def pair_summary(pairs: pd.DataFrame) -> dict:
    """Counts by decision and proxy rates with explicit numerators and denominators."""
    if pairs.empty:
        return {"pairs": 0}
    included = pairs[pairs.decision.str.startswith("included")]
    headline = pairs[pairs.decision == "included_headline"]
    by_year = (headline.assign(year=pd.to_datetime(headline.date_2).dt.year)
               .groupby("year").agg(pairs=("pair_id", "size"),
                                    short_hold=("short_hold", "sum"),
                                    unusual=("unusual_change", "sum")))
    return {
        "pairs": len(pairs),
        "by_decision": pairs.decision.value_counts().to_dict(),
        "headline_pairs": len(headline),
        "headline_properties": int(headline.property_id.nunique()),
        "median_holding_years_headline": round(float(headline.holding_years.median()), 2)
        if len(headline) else None,
        "median_log_return_headline": round(float(headline.log_return.median()), 3)
        if len(headline) else None,
        "proxies_included": {
            "denominator_pairs": len(included),
            "short_hold_resales": int(included.short_hold.sum()),
            "unusual_price_changes": int(included.unusual_change.sum()),
        },
        "headline_by_second_sale_year": {
            int(y): {"pairs": int(r.pairs), "short_hold": int(r.short_hold),
                     "unusual": int(r.unusual)} for y, r in by_year.iterrows()},
        "properties_by_pair_count": {str(k): int(v) for k, v in headline.groupby(
            "property_id").size().value_counts().sort_index().items()},
        "holding_years_quantiles_headline": [round(float(x), 2) for x in np.quantile(
            headline.holding_years, [0.1, 0.25, 0.5, 0.75, 0.9])] if len(headline) else [],
    }


def audit_pairs(pairs: pd.DataFrame, history: pd.DataFrame, bridge: bool) -> dict:
    """Pair audit checks: every pair joins two eligible sales of one property, skips no
    eligible sale between them and (unless bridging) crosses no non-comparable sale."""
    h = history.drop_duplicates(["property_id", "raw_content_fp", "sale_date"])
    by_sale = history.set_index("sale_id")
    violations = {"leg_not_eligible": 0, "legs_in_different_properties": 0,
                  "skipped_eligible_sale": 0, "bridged_non_comparable": 0}
    groups = {pid: g for pid, g in h.groupby("property_id")}
    for pr in pairs.itertuples(index=False):
        a, b = by_sale.loc[pr.sale_id_1], by_sale.loc[pr.sale_id_2]
        if not (a.index_eligible and b.index_eligible):
            violations["leg_not_eligible"] += 1
        if a.property_id != b.property_id or a.property_id != pr.property_id:
            violations["legs_in_different_properties"] += 1
            continue
        g = groups[pr.property_id]
        between = g[(g.sale_date > pr.date_1) & (g.sale_date < pr.date_2)]
        violations["skipped_eligible_sale"] += int(between.index_eligible.any())
        if not bridge:
            violations["bridged_non_comparable"] += int((~between.index_eligible).any())
    return {"pairs_checked": len(pairs), "violations": violations,
            "passed": not any(violations.values())}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def history_inputs(tables, snapshot: str, rule_set: str, address_version: str) -> pd.DataFrame:
    """One row per linkage-eligible occurrence with the fields entities and pairs need."""
    s, a = tables.qualify("silver.sales"), tables.qualify("silver.address_features")
    r = tables.qualify("bronze.ppr_rows")
    df = tables.query(
        "SELECT s.sale_id, s.sale_version, s.sale_date, s.sale_quarter, "
        "s.analysis_price_cents, s.reported_price_cents, s.index_eligible, s.dwelling_status, "
        "CASE WHEN s.eircode_valid THEN s.eircode END AS eircode, a.primary_number, "
        "a.unit_id, a.house_range, a.street_core, s.county, p.raw_content_fp "
        f"FROM {s} s JOIN {a} a ON s.snapshot_id = a.snapshot_id AND s.raw_row_id = a.raw_row_id "
        f"JOIN {r} p ON s.raw_row_id = p.raw_row_id "
        f"WHERE s.snapshot_id = '{snapshot}' AND s.rule_set_version = '{rule_set}' "
        f"AND a.address_version = '{address_version}' AND s.linkage_eligible")
    df["sale_date"] = pd.to_datetime(df.sale_date).dt.date
    df["price_key"] = df.reported_price_cents
    df["address_specific"] = address_specificity(tables, df, address_version)
    return df.sort_values("sale_id").reset_index(drop=True)


# Last words that make an unnumbered first component a thoroughfare, estate or block
# ("Trusk Road", "Oak Grove", "Summerhill Apts") rather than a house name.
THOROUGHFARE_WORDS = {
    "road", "street", "lane", "avenue", "drive", "terrace", "crescent", "close", "way",
    "walk", "row", "place", "square", "park", "grove", "court", "green", "gardens", "heights",
    "view", "hill", "rise", "mews", "manor", "lawn", "lawns", "meadows", "wood", "woods",
    "estate", "village", "apartments", "apts", "flats", "quay", "parade", "glen", "vale",
    "brae", "boulevard", "downs", "dale", "fields", "centre", "plaza", "block"}


def address_specificity(tables, df: pd.DataFrame, address_version: str) -> pd.Series:
    """False when an address has no number, unit or range and its first component is only
    a townland or town name, or a thoroughfare/estate/block name: such text is shared by
    several dwellings. Named houses ("Rose Cottage", "The Old Rectory") stay specific."""
    reference_version = address_version.split("+")[1]
    places = tables.query(
        f"SELECT DISTINCT county, alias_norm FROM {tables.qualify('silver.place_aliases')} "
        f"WHERE reference_version = '{reference_version}' AND target_type IN ('townland', 'bua')")
    place_names = set(zip(places.county, places.alias_norm, strict=True))
    unnumbered = df.primary_number.isna() & df.unit_id.isna() & df.house_range.isna()
    is_place = pd.Series([(c, s) in place_names for c, s in zip(df.county, df.street_core,
                                                                strict=True)], index=df.index)
    is_thoroughfare = df.street_core.map(
        lambda s: isinstance(s, str) and bool(s.split()) and s.split()[-1] in THOROUGHFARE_WORDS)
    return ~(unnumbered & (is_place | is_thoroughfare))


def run_histories(tables, scope: str, config, run) -> dict:
    """Constrained property histories and repeat pairs for the active snapshot."""
    from datetime import UTC, datetime

    from ireland_rsi.contracts import TABLE_SCHEMAS
    from ireland_rsi.entities import crosswalk, entity_summary, resolve_entities
    from ireland_rsi.ingest import active_snapshot
    from ireland_rsi.storage import latest_stage_version, record_stage

    cfg = config.raw
    track = cfg["linkage_track"]
    snapshot = active_snapshot(tables, scope)
    rule_set = latest_stage_version(tables, "clean", snapshot)
    address_version = latest_stage_version(tables, "addresses", snapshot)
    model_version = latest_stage_version(tables, f"linkage_{track}", snapshot)
    if not (rule_set and address_version and model_version):
        raise RuntimeError("clean, addresses and linkage must have run for this snapshot")
    entity_version = config.version(model_version)

    sales = history_inputs(tables, snapshot, rule_set, address_version)
    edges = tables.query(
        "SELECT sale_id_l, sale_id_r, match_weight, match_probability "
        f"FROM {tables.qualify('silver.link_candidates')} "
        f"WHERE model_version = '{model_version}' AND track = '{track}' "
        f"AND match_probability >= {float(cfg['accept_threshold'])}")
    known = set(sales.sale_id)
    edges = edges[edges.sale_id_l.isin(known) & edges.sale_id_r.isin(known)]
    membership, rejected = resolve_entities(sales, edges, config, entity_version)

    previous = latest_stage_version(tables, "entities", snapshot)
    counts = {
        "membership_inserted": tables.insert_new("silver.entity_membership", membership),
        "rejected_edges_inserted": tables.insert_new("silver.entity_rejected_edges", rejected),
    }
    if previous and previous != entity_version:
        old = tables.query(
            f"SELECT property_id, sale_id FROM {tables.qualify('silver.entity_membership')} "
            f"WHERE entity_version = '{previous}'")
        cw = crosswalk(old, membership, previous, entity_version)
        counts["crosswalk_inserted"] = tables.insert_new(
            "silver.entity_crosswalk", cw[TABLE_SCHEMAS["silver.entity_crosswalk"].names])

    history = membership[["property_id", "sale_id"]].merge(sales, on="sale_id")
    pairs, pair_stats = build_repeat_pairs(history, cfg["pairs"], entity_version)
    audit = audit_pairs(pairs, history, cfg["pairs"]["bridge_non_comparable"])
    if not audit["passed"]:
        raise RuntimeError(f"pair audit failed: {audit['violations']}")
    counts["pairs_inserted"] = tables.insert_new("silver.repeat_pairs", pairs)
    now = datetime.now(UTC)
    record_stage(tables, "entities", snapshot, entity_version, run.run_id, now)
    return {"snapshot_id": snapshot, "entity_version": entity_version,
            "model_version": model_version, "track": track,
            "accept_threshold": cfg["accept_threshold"],
            "threshold_status": cfg["threshold_status"], "accepted_edges": len(edges),
            "previous_entity_version": previous, "row_counts": counts, "g7_audit": audit,
            "entities": entity_summary(membership, rejected), "pairs": pair_stats}
