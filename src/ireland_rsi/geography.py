"""Publication gates and geography fallback.

A series is published only if it passes the series gates; within a published series, a
period is shown only if it passes the period gates. A local estimate uses the first series
in the fallback chain that covers *both* its anchor and target periods: the anchor is never
divided by a target from another geography or frequency, and missing periods are never
interpolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from ireland_rsi.index import period_of
from ireland_rsi.uncertainty import BootstrapResult, property_weight_share

PERIOD_STATES = ("supported", "provisional", "suppressed")


@dataclass
class SeriesSupport:
    geography_id: str
    frequency: str
    supported: bool
    reasons: list[str]
    metrics: dict
    period_states: dict[str, str]
    period_reasons: dict[str, str]


def evaluate_series(geography_id: str, frequency: str, pairs, boot: BootstrapResult,
                    policy: dict) -> SeriesSupport:
    """Apply series and period gates to one bootstrapped fit."""
    fit = boot.point
    sg, pg = policy["series_gates"], policy["period_gates"]
    properties = int(pairs.iloc[fit.used].property_id.nunique())
    metrics = {
        "properties": properties,
        "pairs_used": int(len(fit.used)),
        "effective_sample_size": fit.diagnostics["effective_sample_size"],
        "bootstrap_attempted": boot.attempted,
        "bootstrap_valid": boot.valid,
        "bootstrap_success": round(boot.success_rate, 4),
        "bootstrap_failures": dict(boot.failures),
        "max_property_weight_share": round(property_weight_share(pairs, fit), 5),
        "base_status": fit.base_status,
        "unidentified_periods": list(fit.unidentified_periods),
    }
    reasons = []
    if properties < sg["min_properties"]:
        reasons.append(f"properties {properties} < {sg['min_properties']}")
    if metrics["effective_sample_size"] < sg["min_effective_sample_size"]:
        reasons.append(f"effective sample size {metrics['effective_sample_size']} < "
                       f"{sg['min_effective_sample_size']}")
    if boot.success_rate < sg["min_bootstrap_success"]:
        reasons.append(f"bootstrap success {boot.success_rate:.1%} < "
                       f"{sg['min_bootstrap_success']:.0%}")
    if metrics["max_property_weight_share"] > sg["max_property_weight_share"]:
        reasons.append(f"one property carries {metrics['max_property_weight_share']:.1%} "
                       "of the weight")
    if sg.get("require_base_identified") and fit.base_status != "ok":
        reasons.append("base period (2015) not fully identified")

    states, why = {}, {}
    widths = boot.log_width(policy["bootstrap"]["interval"]) if boot.valid else None
    n_prov = policy.get("provisional_periods", 0)
    for i, period in enumerate(fit.periods):
        problems = []
        if fit.endpoint_pairs[i] < pg["min_endpoint_pairs"]:
            problems.append(f"{int(fit.endpoint_pairs[i])} endpoint pairs")
        if widths is None:
            problems.append("no valid bootstrap draws")
        elif widths[i] > pg["max_log_interval_width"]:
            problems.append(f"interval width {widths[i]:.2f}")
        if reasons:
            problems.append("series not supported")
        if problems:
            states[period], why[period] = "suppressed", "; ".join(problems)
        elif i >= len(fit.periods) - n_prov:
            states[period], why[period] = "provisional", "newest period"
        else:
            states[period], why[period] = "supported", ""
    metrics["periods_supported"] = sum(s != "suppressed" for s in states.values())
    metrics["periods_suppressed"] = sum(s == "suppressed" for s in states.values())
    if widths is not None:
        metrics["median_log_interval_width"] = round(float(np.median(widths)), 4)
    return SeriesSupport(geography_id, frequency, not reasons, reasons, metrics, states, why)


@dataclass
class Selection:
    geography_id: str | None
    frequency: str | None
    anchor_period: str | None
    target_period: str | None
    fallback_level: int | None       # 0 = first choice in the chain
    reason: str | None               # set when no series qualifies (estimate reason code)
    tried: list[str]


def select_series(chain: list[dict], supports: dict[tuple[str, str], SeriesSupport],
                  anchor: date, target: date) -> Selection:
    """First series in the chain that is published and shows both periods."""
    tried = []
    for level, option in enumerate(chain):
        key = (option["geography"], option["frequency"])
        s = supports.get(key)
        a, t = period_of(anchor, key[1]), period_of(target, key[1])
        if s is None:
            tried.append(f"{key}: no fit")
            continue
        if not s.supported:
            tried.append(f"{key}: series not supported ({'; '.join(s.reasons)})")
            continue
        missing = [p for p in (a, t) if s.period_states.get(p, "suppressed") == "suppressed"]
        if missing:
            tried.append(f"{key}: periods suppressed {missing}")
            continue
        return Selection(key[0], key[1], a, t, level, None, tried)
    reason = ("SPARSE_GEOGRAPHY" if all("not supported" in x or "no fit" in x for x in tried)
              else "UNSUPPORTED_PERIOD")
    return Selection(None, None, None, None, None, reason, tried)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_uncertainty(tables, scope: str, index_cfg: dict, policy: dict, policy_bytes: bytes,
                    run, draws: int) -> dict:
    """Bootstrap each headline WRS fit, apply publication gates and persist the results."""
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    import pandas as pd

    from ireland_rsi.contracts import TABLE_SCHEMAS
    from ireland_rsi.index import _pairs_with_geography, _select_geography
    from ireland_rsi.ingest import active_snapshot
    from ireland_rsi.storage import latest_stage_version, record_stage, sha256_hex
    from ireland_rsi.uncertainty import property_bootstrap

    snapshot = active_snapshot(tables, scope)
    fit_version = latest_stage_version(tables, "index", snapshot)
    entity_version = latest_stage_version(tables, "entities", snapshot)
    rule_set = latest_stage_version(tables, "clean", snapshot)
    address_version = latest_stage_version(tables, "addresses", snapshot)
    if not fit_version:
        raise RuntimeError("index fits must exist for the snapshot")
    here = Path(__file__).parent
    logic = (here / "geography.py").read_bytes() + (here / "uncertainty.py").read_bytes()
    version = "uv-" + sha256_hex(policy_bytes + logic + f"{fit_version}|{draws}".encode())[:12]
    fits = tables.query(
        f"SELECT fit_id, geography_id, frequency FROM {tables.qualify('silver.index_fits')} "
        f"WHERE fit_version = '{fit_version}' AND method = 'wrs' "
        f"AND population = '{index_cfg['population']}' AND status = 'ok'")
    all_pairs = _pairs_with_geography(tables, entity_version, rule_set, address_version,
                                      ["included_headline"])
    geos = {g["id"]: g for g in index_cfg["geographies"]}
    level = policy["bootstrap"]["interval"]
    now = datetime.now(UTC)
    supports, unc_frames, draw_frames, support_rows, summary = {}, [], [], [], []
    for f in fits.sort_values(["geography_id", "frequency"]).itertuples(index=False):
        pairs = _select_geography(all_pairs, geos[f.geography_id])
        boot = property_bootstrap(pairs, f.frequency, index_cfg, draws,
                                  policy["bootstrap"]["seed"])
        s = evaluate_series(f.geography_id, f.frequency, pairs, boot, policy)
        supports[(f.geography_id, f.frequency)] = s
        if boot.valid:
            lo, mid, hi = boot.level_interval(level)
            width = boot.log_width(level)
        else:
            lo = mid = hi = width = np.full(len(boot.periods), np.nan)
        unc_frames.append(pd.DataFrame({
            "uncertainty_version": version, "fit_id": f.fit_id, "period": boot.periods,
            "index_value": boot.point.index, "level_lo": lo, "level_median": mid,
            "level_hi": hi, "log_width": width, "endpoint_pairs": boot.point.endpoint_pairs,
            "period_state": [s.period_states[p] for p in boot.periods],
            "state_reason": [s.period_reasons[p] for p in boot.periods]}))
        if boot.valid:
            d, p = np.meshgrid(np.arange(boot.valid), np.arange(len(boot.periods)),
                               indexing="ij")
            draw_frames.append(pd.DataFrame({
                "uncertainty_version": version, "fit_id": f.fit_id,
                "draw": d.ravel().astype("int32"),
                "period": np.asarray(boot.periods)[p.ravel()],
                "log_level": boot.log_levels.ravel()}))
        m = s.metrics
        support_rows.append({
            "uncertainty_version": version, "fit_id": f.fit_id,
            "geography_id": f.geography_id, "frequency": f.frequency,
            "supported": s.supported, "reasons": "; ".join(s.reasons),
            "properties": m["properties"], "effective_sample_size": m["effective_sample_size"],
            "bootstrap_attempted": m["bootstrap_attempted"],
            "bootstrap_valid": m["bootstrap_valid"],
            "max_property_weight_share": m["max_property_weight_share"],
            "periods_supported": m["periods_supported"],
            "periods_suppressed": m["periods_suppressed"],
            "metrics_json": json.dumps(m, default=str), "created_at": now,
            "run_id": run.run_id})
        summary.append({"geography_id": f.geography_id, "frequency": f.frequency,
                        "supported": s.supported, "reasons": s.reasons,
                        **{k: m[k] for k in ("properties", "bootstrap_success",
                                             "max_property_weight_share",
                                             "periods_supported", "periods_suppressed",
                                             "median_log_interval_width") if k in m}})
    counts = {
        "uncertainty_inserted": tables.insert_new(
            "silver.index_uncertainty",
            pd.concat(unc_frames)[TABLE_SCHEMAS["silver.index_uncertainty"].names]),
        "support_inserted": tables.insert_new(
            "silver.series_support",
            pd.DataFrame(support_rows)[TABLE_SCHEMAS["silver.series_support"].names]),
    }
    if draw_frames:
        counts["draws_inserted"] = tables.insert_new("silver.index_draws",
                                                     pd.concat(draw_frames))
    record_stage(tables, "uncertainty", snapshot, version, run.run_id, now)
    demos = {}
    for geo, chain in policy["fallback"].items():
        for anchor, target in [(date(2015, 5, 1), date(2025, 8, 1)),
                               (date(2012, 2, 1), date(2026, 3, 1))]:
            sel = select_series(chain, supports, anchor, target)
            demos[f"{geo} {anchor}->{target}"] = {
                "series": f"{sel.geography_id}/{sel.frequency}" if sel.geography_id else None,
                "fallback_level": sel.fallback_level, "periods": [sel.anchor_period,
                                                                  sel.target_period],
                "reason": sel.reason, "tried": sel.tried}
    return {"snapshot_id": snapshot, "fit_version": fit_version, "uncertainty_version": version,
            "draws": draws, "row_counts": counts, "series": summary, "fallback_examples": demos}
