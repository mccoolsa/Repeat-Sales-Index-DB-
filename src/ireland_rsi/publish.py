"""Versioned releases and the current-release pointer.

A release is an immutable bundle of serving tables keyed by `release_id`, plus a manifest
recording every input version, the gate results and run metrics. The release ID is a hash
of those inputs, so publishing the same inputs twice is a no-op.

Readers (the lookup app and the dashboard) only ever follow the *current-release pointer*:
the latest `publish` or `rollback` event in the append-only `gold.release_events` table.
The pointer moves in one single-row insert after every serving table of the release has
been written, so a partly written release is never visible, and a failed release leaves the
previous one current. Rolling back is a new event pointing at an earlier complete release;
nothing is deleted or overwritten. Delta time travel is not relied on for archival.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from ireland_rsi.storage import latest_stage_version, sha256_hex

REQUIRED_STAGES = ("clean", "addresses", "entities", "index", "uncertainty", "validation",
                   "backtest")
RELEASE_TABLES = ("gold.index_vintages", "gold.lookup_history", "gold.release_calibration",
                  "gold.revisions")
POINTER_ACTIONS = ("publish", "rollback")


class ReleaseError(RuntimeError):
    """A release cannot be published or a rollback target is not a complete release."""


@dataclass
class Manifest:
    release_id: str
    release_state: str            # research | published | failed
    profile: str
    scope: str
    snapshot_id: str
    versions: dict
    gates: dict
    metrics: dict
    lookup_policy: dict
    code_hash: str
    config_hash: str
    previous_release_id: str | None
    run_id: str

    @property
    def hard_gates_passed(self) -> bool:
        return all(g["passed"] for g in self.gates.values() if g["hard"])

    def row(self, created_at) -> dict:
        return {"release_id": self.release_id, "release_state": self.release_state,
                "created_at": created_at, "profile": self.profile, "scope": self.scope,
                "snapshot_id": self.snapshot_id,
                "versions_json": json.dumps(self.versions, sort_keys=True),
                "gates_json": json.dumps(self.gates, sort_keys=True, default=str),
                "metrics_json": json.dumps(self.metrics, sort_keys=True, default=str),
                "lookup_policy_json": json.dumps(self.lookup_policy, sort_keys=True),
                "code_hash": self.code_hash, "config_hash": self.config_hash,
                "previous_release_id": self.previous_release_id, "run_id": self.run_id}


def make_release_id(profile: str, snapshot_id: str, versions: dict, code_hash: str,
                    config_hash: str) -> str:
    key = json.dumps({"profile": profile, "snapshot": snapshot_id, "versions": versions,
                      "code": code_hash, "config": config_hash}, sort_keys=True)
    return "rel-" + sha256_hex(key.encode())[:12]


# ---------------------------------------------------------------------------
# Pointer
# ---------------------------------------------------------------------------


def current_release(tables) -> str | None:
    if not tables.exists("gold.release_events"):
        return None
    df = tables.query(
        f"SELECT release_id FROM {tables.qualify('gold.release_events')} "
        f"WHERE action IN ('publish', 'rollback') ORDER BY event_at DESC, event_id DESC LIMIT 1")
    return None if df.empty else str(df.iloc[0, 0])


def _event(tables, release_id: str, action: str, run_id: str, reason: str | None) -> None:
    from ireland_rsi.contracts import TABLE_SCHEMAS

    at = datetime.now(UTC)
    event_id = "evt-" + sha256_hex(f"{release_id}|{action}|{run_id}|{at.isoformat()}".encode())[:16]
    tables.insert_new("gold.release_events", pd.DataFrame(
        [(event_id, release_id, action, at, run_id, reason)],
        columns=TABLE_SCHEMAS["gold.release_events"].names))


def release_is_complete(tables, release_id: str) -> bool:
    """A release can be pointed at only if its manifest says it passed and its serving
    tables hold rows."""
    if not tables.exists("gold.release_manifest"):
        return False
    m = tables.query(
        f"SELECT release_state FROM {tables.qualify('gold.release_manifest')} "
        f"WHERE release_id = '{release_id}'")
    if m.empty or m.iloc[0, 0] == "failed":
        return False
    for t in ("gold.index_vintages", "gold.lookup_history"):
        if not tables.exists(t):
            return False
        n = tables.query(f"SELECT count(*) FROM {tables.qualify(t)} "
                         f"WHERE release_id = '{release_id}'").iloc[0, 0]
        if not n:
            return False
    return True


def rollback(tables, to_release_id: str, run_id: str, reason: str) -> str:
    if not release_is_complete(tables, to_release_id):
        raise ReleaseError(f"{to_release_id} is not a complete release; pointer unchanged")
    _event(tables, to_release_id, "rollback", run_id, reason)
    return to_release_id


# ---------------------------------------------------------------------------
# Building a release
# ---------------------------------------------------------------------------


def gather_versions(tables, snapshot: str, track: str) -> dict:
    stages = list(REQUIRED_STAGES) + [f"linkage_{track}"]
    return {s: latest_stage_version(tables, s, snapshot) for s in sorted(stages)}


def evaluate_gates(tables, versions: dict, entity_cfg: dict) -> dict:
    """Hard gates block publication; soft gates decide research vs published state."""
    q = tables.qualify
    gates = {"all_stages_present": {
        "hard": True, "passed": all(versions.values()),
        "detail": sorted(k for k, v in versions.items() if not v)}}
    if not gates["all_stages_present"]["passed"]:
        return gates
    pairs = tables.query(f"SELECT count(*) FROM {q('silver.repeat_pairs')} "
                         f"WHERE entity_version = '{versions['entities']}' "
                         "AND decision = 'included_headline'").iloc[0, 0]
    gates["g7_pairs_audited"] = {"hard": True, "passed": bool(pairs),
                                 "detail": f"{pairs} headline pairs (audit enforced when "
                                           "property histories are built)"}
    sup = tables.query(f"SELECT geography_id, frequency, supported FROM "
                       f"{q('silver.series_support')} "
                       f"WHERE uncertainty_version = '{versions['uncertainty']}'")
    gates["g9_supported_series"] = {
        "hard": True, "passed": bool(sup.supported.any()),
        "detail": [f"{r.geography_id}/{r.frequency}" for r in sup.itertuples() if r.supported]}
    n_val = tables.query(f"SELECT count(*) FROM {q('gold.validation_results')} "
                         f"WHERE validation_version = '{versions['validation']}'").iloc[0, 0]
    gates["g10_validation_recorded"] = {"hard": True, "passed": bool(n_val),
                                        "detail": f"{n_val} validation results"}
    cal = tables.query(f"SELECT horizon_band, status FROM {q('gold.prediction_calibration')} "
                       f"WHERE backtest_version = '{versions['backtest']}'")
    gates["g11_calibrated_bands"] = {
        "hard": True, "passed": bool((cal.status == "ok").any()),
        "detail": {r.horizon_band: r.status for r in cal.itertuples()}}
    gates["g6_threshold_frozen"] = {
        "hard": False, "passed": entity_cfg["threshold_status"] == "frozen",
        "detail": f"accept_threshold {entity_cfg['accept_threshold']} "
                  f"({entity_cfg['threshold_status']})"}
    return gates


def index_vintages(tables, release_id: str, uncertainty_version: str) -> pd.DataFrame:
    q = tables.qualify
    df = tables.query(
        "SELECT u.fit_id, f.geography_id, f.frequency, u.period, u.index_value, u.level_lo, "
        "u.level_hi, u.log_width, u.endpoint_pairs, u.period_state, u.state_reason, "
        "s.supported AS series_supported, s.properties AS series_properties "
        f"FROM {q('silver.index_uncertainty')} u "
        f"JOIN {q('silver.index_fits')} f ON u.fit_id = f.fit_id "
        f"JOIN {q('silver.series_support')} s ON s.fit_id = u.fit_id "
        "AND s.uncertainty_version = u.uncertainty_version "
        f"WHERE u.uncertainty_version = '{uncertainty_version}'")
    df.insert(0, "release_id", release_id)
    return df


def _link_strength(n_sales: int, weight) -> str:
    if n_sales <= 1:
        return "single_sale"
    if weight is None or (isinstance(weight, float) and math.isnan(weight)):
        return "linked"
    p = 1 / (1 + 2 ** (-float(weight)))
    return "strong" if p >= 0.99 else "moderate" if p >= 0.9 else "weak"


def lookup_history(tables, release_id: str, snapshot: str, versions: dict) -> pd.DataFrame:
    """Public register facts per sale, with its property and anchor eligibility."""
    q = tables.qualify
    df = tables.query(
        "SELECT s.sale_id, s.sale_date, s.address_raw AS address, a.address_norm AS "
        "address_search, s.county, a.locality_name, a.locality_confidence, "
        "s.reported_price_cents, s.analysis_price_cents, s.price_basis, s.dwelling_status, "
        "s.not_full_market_price, s.index_eligible, s.disposition, s.reason_codes, "
        "m.property_id, m.n_sales, m.attach_weight "
        f"FROM {q('silver.sales')} s JOIN {q('silver.address_features')} a "
        "ON s.snapshot_id = a.snapshot_id AND s.raw_row_id = a.raw_row_id "
        f"LEFT JOIN {q('silver.entity_membership')} m ON m.sale_id = s.sale_id "
        f"AND m.entity_version = '{versions['entities']}' "
        f"WHERE s.snapshot_id = '{snapshot}' AND s.rule_set_version = '{versions['clean']}' "
        f"AND a.address_version = '{versions['addresses']}' AND s.parse_valid")
    df = df.sort_values("sale_id").drop_duplicates("sale_id").reset_index(drop=True)
    unlinked = df.property_id.isna()
    df.loc[unlinked, "property_id"] = "unlinked_" + df.loc[unlinked, "sale_id"]
    df["property_sales"] = df.n_sales.fillna(1).astype("int32")
    df["reported_price_eur"] = df.reported_price_cents / 100
    df["analysis_price_eur"] = df.analysis_price_cents / 100
    df["anchor_eligible"] = df.index_eligible.fillna(False).astype(bool)
    df["anchor_reason"] = [None if ok else (codes or disp) for ok, codes, disp in
                           zip(df.anchor_eligible, df.reason_codes, df.disposition, strict=True)]
    df["link_strength"] = [_link_strength(n, w) for n, w in
                           zip(df.property_sales, df.attach_weight, strict=True)]
    df["sale_date"] = pd.to_datetime(df.sale_date).dt.date
    df.insert(0, "release_id", release_id)
    from ireland_rsi.contracts import TABLE_SCHEMAS

    return df[TABLE_SCHEMAS["gold.lookup_history"].names]


def release_calibration(tables, release_id: str, backtest_version: str) -> pd.DataFrame:
    df = tables.query(
        "SELECT horizon_band, min_years, max_years, level, q_lo, q_hi, coverage_test, n_test, "
        f"status FROM {tables.qualify('gold.prediction_calibration')} "
        f"WHERE backtest_version = '{backtest_version}'")
    df.insert(0, "release_id", release_id)
    return df


def revisions(current: pd.DataFrame, previous: pd.DataFrame | None, previous_id: str | None,
              cause: str) -> pd.DataFrame:
    """Level changes of every period both releases publish (not suppressed)."""
    cols = ["release_id", "previous_release_id", "geography_id", "frequency", "period",
            "previous_value", "value", "log_revision", "cause"]
    if previous is None or previous.empty:
        return pd.DataFrame(columns=cols)
    keys = ["geography_id", "frequency", "period"]
    cur = current[current.period_state != "suppressed"]
    prev = previous[previous.period_state != "suppressed"]
    j = cur[["release_id", *keys, "index_value"]].merge(
        prev[[*keys, "index_value"]], on=keys, suffixes=("", "_prev"))
    j["previous_release_id"] = previous_id
    j["previous_value"] = j.index_value_prev
    j["value"] = j.index_value
    j["log_revision"] = [math.log(v / p) for v, p in zip(j.value, j.previous_value, strict=True)]
    j["cause"] = cause
    return j[cols]


def revision_cause(previous: dict | None, snapshot: str, code_hash: str,
                   config_hash: str) -> str:
    """Separate new source information from methodological change."""
    if previous is None:
        return "initial"
    source = previous["snapshot_id"] != snapshot
    method = previous["code_hash"] != code_hash or previous["config_hash"] != config_hash
    return {(True, True): "source+method", (True, False): "source",
            (False, True): "method", (False, False): "none"}[(source, method)]


def run_metrics(tables, snapshot: str, versions: dict) -> dict:
    """Operational metrics recorded with every release."""
    q = tables.qualify
    where = f"WHERE snapshot_id = '{snapshot}' AND rule_set_version = '{versions['clean']}'"
    sales = tables.query(
        "SELECT count(*) AS rows, sum(CASE WHEN index_eligible THEN 1 ELSE 0 END) AS eligible "
        f"FROM {q('silver.sales')} {where}").iloc[0]
    quarantined = (tables.query(f"SELECT count(*) FROM {q('silver.quarantine')} {where}")
                   .iloc[0, 0] if tables.exists("silver.quarantine") else 0)
    ent = tables.query(
        "SELECT count(DISTINCT property_id) AS properties, count(*) AS sales, "
        "sum(CASE WHEN n_sales > 1 THEN 1 ELSE 0 END) AS linked_sales "
        f"FROM {q('silver.entity_membership')} WHERE entity_version = '{versions['entities']}'"
    ).iloc[0]
    refused = tables.query(f"SELECT count(*) FROM {q('silver.entity_rejected_edges')} "
                           f"WHERE entity_version = '{versions['entities']}'").iloc[0, 0]
    sup = tables.query(
        "SELECT geography_id, frequency, supported, bootstrap_attempted, bootstrap_valid "
        f"FROM {q('silver.series_support')} "
        f"WHERE uncertainty_version = '{versions['uncertainty']}'")
    return {
        "sales_rows": int(sales.rows), "quarantined_rows": int(quarantined),
        "quarantine_rate": float(quarantined / sales.rows),   # quarantined rows stay in sales
        "eligible_rate": float(sales.eligible / sales.rows),
        "properties": int(ent.properties), "linked_sale_share": float(ent.linked_sales / ent.sales),
        "cluster_conflicts_refused": int(refused),
        "bootstrap_failure_rate": {f"{r.geography_id}/{r.frequency}":
                                   round(1 - r.bootstrap_valid / r.bootstrap_attempted, 4)
                                   for r in sup.itertuples()},
    }


def publish_release(tables, manifest: Manifest, frames: dict[str, pd.DataFrame],
                    simulate_failure_after: str | None = None) -> dict:
    """Write a release and move the pointer, or record a failure and leave it alone.

    `simulate_failure_after` (drills only) raises after writing that serving table, to
    prove that a half-written release never becomes current.
    """
    from ireland_rsi.contracts import TABLE_SCHEMAS

    now = datetime.now(UTC)
    before = current_release(tables)
    if not manifest.hard_gates_passed:
        manifest.release_state = "failed"
        tables.insert_new("gold.release_manifest", pd.DataFrame(
            [manifest.row(now)])[TABLE_SCHEMAS["gold.release_manifest"].names])
        failed = sorted(k for k, g in manifest.gates.items() if g["hard"] and not g["passed"])
        _event(tables, manifest.release_id, "fail", manifest.run_id, ", ".join(failed))
        return {"release_id": manifest.release_id, "published": False,
                "current_release": before, "failed_gates": failed}
    counts = {}
    for table in RELEASE_TABLES:
        df = frames[table]
        # Written even when empty, so every serving table exists for readers and views.
        counts[table] = tables.insert_new(table, df[TABLE_SCHEMAS[table].names])
        if simulate_failure_after == table:
            raise ReleaseError(f"simulated failure after writing {table}")
    counts["gold.release_manifest"] = tables.insert_new("gold.release_manifest", pd.DataFrame(
        [manifest.row(now)])[TABLE_SCHEMAS["gold.release_manifest"].names])
    if before == manifest.release_id:
        return {"release_id": manifest.release_id, "published": False, "unchanged": True,
                "current_release": before, "row_counts": counts}
    _event(tables, manifest.release_id, "publish", manifest.run_id, manifest.release_state)
    return {"release_id": manifest.release_id, "published": True,
            "previous_release": before, "current_release": current_release(tables),
            "release_state": manifest.release_state, "row_counts": counts}


def lookup_policy(policy: dict) -> dict:
    """What the lookup app needs from the publication policy, frozen into the release so the
    app never mixes one release's data with another's rules."""
    lk = policy["lookup"]
    return {"estimate_chain": policy["fallback"][lk["estimate_chain"]],
            "display_chains": policy["fallback"], "stale_after_days": lk["stale_after_days"],
            "index_interval": policy["bootstrap"]["interval"]}


def run_publish(tables, profile, entity_cfg: dict, policy: dict, code_hash: str,
                config_hash: str, run, simulate: str | None = None) -> dict:
    """Assemble the release for the active snapshot, check gates and publish."""
    from ireland_rsi.ingest import active_snapshot

    snapshot = active_snapshot(tables, profile.scope)
    versions = gather_versions(tables, snapshot, entity_cfg["linkage_track"])
    gates = evaluate_gates(tables, versions, entity_cfg)
    if simulate == "gate":
        gates["drill_forced_failure"] = {"hard": True, "passed": False,
                                         "detail": "failure drill"}
    release_id = make_release_id(profile.name, snapshot, versions, code_hash, config_hash)
    if simulate:   # drills get their own ID so they can never shadow a real release
        release_id += "-drill-" + sha256_hex(simulate.encode())[:6]
    previous_id = current_release(tables)
    prev_manifest = None
    if previous_id:
        m = tables.query(f"SELECT snapshot_id, code_hash, config_hash FROM "
                         f"{tables.qualify('gold.release_manifest')} "
                         f"WHERE release_id = '{previous_id}'")
        prev_manifest = None if m.empty else m.iloc[0].to_dict()
    state = "published" if all(g["passed"] for g in gates.values()) else "research"
    metrics = run_metrics(tables, snapshot, versions) if gates["all_stages_present"]["passed"] \
        else {}
    manifest = Manifest(release_id, state, profile.name, profile.scope, snapshot, versions,
                        gates, metrics, lookup_policy(policy), code_hash, config_hash,
                        previous_id if previous_id != release_id else None, run.run_id)
    frames: dict[str, pd.DataFrame] = {}
    if manifest.hard_gates_passed:
        vint = index_vintages(tables, release_id, versions["uncertainty"])
        prev_vint = None
        if previous_id and previous_id != release_id:
            prev_vint = tables.query(f"SELECT * FROM {tables.qualify('gold.index_vintages')} "
                                     f"WHERE release_id = '{previous_id}'")
        cause = revision_cause(prev_manifest, snapshot, code_hash, config_hash)
        frames = {
            "gold.index_vintages": vint,
            "gold.lookup_history": lookup_history(tables, release_id, snapshot, versions),
            "gold.release_calibration": release_calibration(tables, release_id,
                                                            versions["backtest"]),
            "gold.revisions": revisions(vint, prev_vint, previous_id, cause),
        }
    result = publish_release(tables, manifest, frames,
                             simulate_failure_after=simulate if simulate and simulate.startswith(
                                 "gold.") else None)
    rev = frames.get("gold.revisions")
    return {**result, "release_state": manifest.release_state, "snapshot_id": snapshot,
            "versions": versions, "gates": gates, "metrics": metrics,
            "revisions": None if rev is None or rev.empty else {
                "periods": len(rev), "cause": rev.cause.iloc[0],
                "max_abs_log_revision": round(float(rev.log_revision.abs().max()), 5)}}
