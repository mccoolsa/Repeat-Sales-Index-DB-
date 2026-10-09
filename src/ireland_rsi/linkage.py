"""Candidate generation and probabilistic linkage with Splink.

Two tracks:
* address_only: no Eircode column exists in the input, so the model cannot use it. This is
  the track evaluated against Eircode weak labels and human labels.
* production: adds an Eircode comparison and an Eircode recovery block.

Transaction price and dates are never identity features: a model must not link sales
because the resulting price growth looks plausible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pandas as pd

TRACKS = ("address_only", "production")
# A street component made only of these words ("the avenue", "crescent") names a
# thoroughfare type, not a place, so it needs extra evidence to form a block.
GENERIC_STREET_WORDS = {
    "the", "avenue", "crescent", "green", "close", "court", "drive", "park", "road", "lane",
    "grove", "way", "view", "lawn", "lawns", "heights", "square", "place", "street", "terrace",
    "walk", "rise", "mews", "gardens", "hill", "wood", "woods", "meadows", "manor", "row",
    "vale", "glen", "downs", "dale", "parade", "apartments", "house", "hall",
    "lodge", "cottages", "village", "estate", "main", "new", "old", "upper", "lower", "north",
    "south", "east", "west", "block", "a", "b", "c", "d"}


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def street_key(street_core: str | None) -> str | None:
    """First distinctive street token, for the missing-locality recovery block."""
    if not isinstance(street_core, str):
        return None
    words = [w for w in street_core.split() if w not in {"the", "saint", "upper", "lower"}]
    return words[0] if words else None


def linkage_frame(tables, snapshot_id: str, address_version: str,
                  rule_set_version: str) -> pd.DataFrame:
    """Linkage-eligible occurrences with address features and (valid) Eircode."""
    from ireland_rsi.labels import mask_eircode

    s, a = tables.qualify("silver.sales"), tables.qualify("silver.address_features")
    df = tables.query(
        "SELECT s.sale_id, s.raw_row_id, s.sale_date, s.county, "
        "CASE WHEN s.eircode_valid THEN s.eircode END AS eircode, a.address_norm, "
        "a.street_core, a.primary_number, a.unit_id, a.locality_id, a.townland_id, "
        f"a.postal_district, a.locality_parts_json FROM {s} s JOIN {a} a "
        "ON s.snapshot_id = a.snapshot_id AND s.raw_row_id = a.raw_row_id "
        f"WHERE s.snapshot_id = '{snapshot_id}' AND s.rule_set_version = '{rule_set_version}' "
        f"AND a.address_version = '{address_version}' AND s.linkage_eligible")
    df["address_norm"] = df.address_norm.map(mask_eircode)
    df["second_part"] = df.pop("locality_parts_json").map(
        lambda s: (json.loads(s) or [None])[0] if isinstance(s, str) else None)
    return df.sort_values("sale_id").reset_index(drop=True)


def linkage_input(frame: pd.DataFrame, track: str) -> pd.DataFrame:
    """Splink input from the linkage frame (one row per linkage-eligible occurrence)."""
    if track not in TRACKS:
        raise ValueError(f"unknown track {track}")
    cols = ["sale_id", "county", "address_norm", "street_core", "primary_number", "unit_id",
            "locality_id", "townland_id", "postal_district", "second_part"]
    if track == "production":
        cols.append("eircode")
    df = frame[[c for c in cols if c in frame.columns]].rename(columns={"sale_id": "unique_id"})
    if "postal_district" not in df:
        df["postal_district"] = None
    if "second_part" not in df:
        df["second_part"] = None
    df["street_key"] = df.street_core.map(street_key)
    df["street_generic"] = df.street_core.map(
        lambda s: isinstance(s, str) and set(s.split()) <= GENERIC_STREET_WORDS)
    # Splink works with real nulls; pandas 3 may hold NaN in string columns.
    return df.astype(object).where(df.notna(), None).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockingRule:
    name: str
    sql: str
    tracks: tuple[str, ...] = TRACKS


BLOCKING_RULES = (
    BlockingRule("exact_address",
                 "l.county = r.county AND l.address_norm = r.address_norm"),
    BlockingRule("number_street",
                 "l.county = r.county AND l.street_core = r.street_core "
                 "AND l.primary_number = r.primary_number"),
    # Street spelling variants in the same town; the first street word splits city blocks.
    BlockingRule("locality_number_street_key",
                 "l.county = r.county AND l.locality_id = r.locality_id "
                 "AND l.primary_number = r.primary_number AND l.street_key = r.street_key"),
    BlockingRule("rural_name",
                 "l.county = r.county AND l.street_core = r.street_core "
                 "AND l.primary_number IS NULL AND r.primary_number IS NULL "
                 "AND NOT l.street_generic"),
    BlockingRule("rural_generic_name_with_place",
                 "l.county = r.county AND l.street_core = r.street_core "
                 "AND l.second_part = r.second_part "
                 "AND l.primary_number IS NULL AND r.primary_number IS NULL "
                 "AND l.street_generic"),
    BlockingRule("townland_both_unnumbered",
                 "l.county = r.county AND l.townland_id = r.townland_id "
                 "AND l.primary_number IS NULL AND r.primary_number IS NULL"),
    BlockingRule("recovery_street_key_number",
                 "l.county = r.county AND l.street_key = r.street_key "
                 "AND l.primary_number = r.primary_number "
                 "AND (l.locality_id IS NULL OR r.locality_id IS NULL)"),
    BlockingRule("recovery_eircode_any_county", "l.eircode = r.eircode", ("production",)),
)


def rules_for(track: str) -> list[BlockingRule]:
    return [r for r in BLOCKING_RULES if track in r.tracks]


def rule_hit_sql(rule: BlockingRule) -> str:
    """The rule as a boolean over Splink prediction columns (col_l / col_r)."""
    return re.sub(r"\b([lr])\.([a-z_]+)", r"\2_\1", rule.sql)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

RETAINED = ["address_norm", "street_key", "street_generic", "second_part"]

# Unique, deterministic rules used only to estimate the prior match rate. The assumed recall
# of these rules is a documented assumption, varied in validation sensitivity runs.
PRIOR_RULES = ["l.county = r.county AND l.address_norm = r.address_norm "
               "AND l.primary_number IS NOT NULL"]
PRIOR_RECALL = 0.6

# EM sessions: each blocks on some fields and trains m-probabilities for the others. Only
# blocks where true matches are common are used: blocks dominated by neighbours (same
# street, different houses) or by different rural homes in one townland made EM learn
# "same street" instead of "same property" (house-number m fell to chance, 0.012).
EM_SESSIONS = (
    ("number_street", "l.county = r.county AND l.street_core = r.street_core "
                      "AND l.primary_number = r.primary_number"),
    ("locality_number_street_key", "l.county = r.county AND l.locality_id = r.locality_id "
                                   "AND l.primary_number = r.primary_number "
                                   "AND l.street_key = r.street_key"),
)


# m-probabilities set by assumption where EM cannot identify them (documented in D-018 and
# varied in validation sensitivity runs). Lists are [agree, disagree].
ASSUMED_M = {
    "county": [0.995, 0.005],          # county field errors are rare
    "primary_number": [0.97, 0.03],    # true repeat sales almost always share the number
    "unit_conflict": 0.005,            # different unit IDs: rare data-entry error for a match
}


def comparisons(track: str) -> list:
    import splink.comparison_level_library as cll
    import splink.comparison_library as cl

    unit = cl.CustomComparison(
        output_column_name="unit_id",
        comparison_description="Apartment/unit identifier",
        comparison_levels=[
            cll.CustomLevel("unit_id_l IS NULL AND unit_id_r IS NULL",
                            "neither has a unit").configure(is_null_level=True),
            cll.ExactMatchLevel("unit_id"),
            cll.CustomLevel("unit_id_l IS NULL OR unit_id_r IS NULL", "only one has a unit"),
            cll.ElseLevel().configure(m_probability=ASSUMED_M["unit_conflict"]),
        ])
    # County and house number are held constant inside the training blocks, so EM cannot
    # estimate their m-probabilities. They are fixed by assumption (ASSUMED_M); their
    # u-probabilities, including term frequencies for common numbers, are estimated.
    comps = [
        cl.ExactMatch("county").configure(m_probabilities=ASSUMED_M["county"]),
        cl.ExactMatch("primary_number").configure(
            term_frequency_adjustments=True, m_probabilities=ASSUMED_M["primary_number"]),
        unit,
        cl.JaroWinklerAtThresholds("street_core", [0.95, 0.88]).configure(
            term_frequency_adjustments=True),
        cl.ExactMatch("locality_id"),
        cl.ExactMatch("townland_id"),
        cl.ExactMatch("postal_district"),
    ]
    if track == "production":
        comps.append(cl.ExactMatch("eircode"))
    return comps


@dataclass
class TrainedModel:
    track: str
    version: str
    settings_json: dict
    linker: object
    training_log: dict


def train(df: pd.DataFrame, track: str, seed: int = 20261006,
          u_max_pairs: float = 2e7, em_max_pairs: float = 5e6) -> TrainedModel:
    """Fit u by random sampling, the prior by deterministic rules, and m by EM."""
    import json as _json
    import time

    from splink import Linker, SettingsCreator
    from splink.backends.duckdb import DuckDBAPI

    from ireland_rsi.storage import sha256_hex

    settings = SettingsCreator(
        link_type="dedupe_only",
        comparisons=comparisons(track),
        blocking_rules_to_generate_predictions=[r.sql for r in rules_for(track)],
        retain_matching_columns=True,
        retain_intermediate_calculation_columns=False,
        additional_columns_to_retain=RETAINED,
        max_iterations=50,
        em_convergence=1e-4,
    )
    linker = Linker(DuckDBAPI().register(df), settings, log_level="WARNING")
    log: dict = {"records": len(df)}
    t0 = time.time()
    linker.training.estimate_probability_two_random_records_match(PRIOR_RULES, recall=PRIOR_RECALL)
    linker.training.estimate_u_using_random_sampling(max_pairs=u_max_pairs, seed=seed)
    log["u_seconds"] = round(time.time() - t0, 1)
    for name, rule in EM_SESSIONS:
        t = time.time()
        linker.training.estimate_parameters_using_expectation_maximisation(
            rule, max_pairs=em_max_pairs, record_sample_proportion=1.0)
        log[f"em_{name}_seconds"] = round(time.time() - t, 1)
    trained = linker.misc.save_model_to_json()
    log["train_seconds"] = round(time.time() - t0, 1)
    # linker_uid is random per Linker instance; it would change the version on every run.
    stable = {k: v for k, v in trained.items() if k not in VOLATILE_SETTINGS}
    canonical = _json.dumps(_round_floats(stable), sort_keys=True, default=str)
    version = "lm-" + sha256_hex(canonical.encode())[:12]
    return TrainedModel(track, version, trained, linker, log)


VOLATILE_SETTINGS = {"linker_uid"}


def _round_floats(obj, digits: int = 9):
    """Round floats to significant digits so the model version does not depend on
    last-bit differences between platforms (identical outputs gave different IDs)."""
    if isinstance(obj, float):
        return float(f"{obj:.{digits}g}")
    if isinstance(obj, dict):
        return {k: _round_floats(v, digits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, digits) for v in obj]
    return obj


def score_candidates(model: TrainedModel) -> pd.DataFrame:
    """Score every candidate pair and record all blocking rules that generated it."""
    import numpy as np

    preds = model.linker.inference.predict().as_pandas_dataframe()
    hits = np.full(len(preds), "", dtype=object)
    for rule in rules_for(model.track):
        mask = _eval_rule(preds, rule)
        preds[f"rule_{rule.name}"] = mask
        hits = np.where(mask, np.where(hits == "", rule.name, hits + "," + rule.name), hits)
    preds["rules_hit"] = hits
    preds["baseline_link"] = deterministic_baseline(preds)
    return preds


def _eval_rule(preds: pd.DataFrame, rule: BlockingRule) -> pd.Series:
    """Evaluate a blocking rule on prediction columns with SQL null semantics."""
    import duckdb

    con = duckdb.connect()
    con.register("p", preds)
    sql = rule_hit_sql(rule)
    out = con.execute(f"SELECT coalesce(({sql}), false) AS hit FROM p").df().hit
    con.close()
    return out.astype(bool).to_numpy()


def deterministic_baseline(preds: pd.DataFrame) -> pd.Series:
    """Rule-based baseline: identical address text, or the same number and street with
    no unit conflict and no conflicting locality."""
    same_text = preds.address_norm_l.eq(preds.address_norm_r)
    units_ok = (preds.unit_id_l.isna() & preds.unit_id_r.isna()) | preds.unit_id_l.eq(
        preds.unit_id_r)
    locality_ok = preds.locality_id_l.isna() | preds.locality_id_r.isna() | preds.locality_id_l.eq(
        preds.locality_id_r)
    structured = (preds.primary_number_l.notna() & preds.primary_number_l.eq(
        preds.primary_number_r) & preds.street_core_l.eq(preds.street_core_r)
        & units_ok & locality_ok)
    return (same_text | structured).fillna(False).astype(bool)


# ---------------------------------------------------------------------------
# Blocking report, persistence and orchestration
# ---------------------------------------------------------------------------

GAMMAS = ["county", "primary_number", "unit_id", "street_core", "locality_id", "townland_id",
          "postal_district", "eircode"]


def blocking_report(df: pd.DataFrame, track: str) -> dict:
    """Marginal and cumulative candidate counts per rule, and the reduction achieved."""
    import splink.blocking_analysis as ba
    from splink.backends.duckdb import DuckDBAPI

    rules = rules_for(track)
    counts = ba.count_comparisons_from_blocking_rules(
        DuckDBAPI().register(df), blocking_rules=[r.sql for r in rules],
        link_type="dedupe_only", record_sample_proportion=1.0)
    possible = len(df) * (len(df) - 1) // 2
    total = counts[-1]["cumulative_comparison_count"] if counts else 0
    return {
        "records": len(df),
        "possible_pairs": possible,
        "candidate_pairs": int(total),
        "reduction_ratio": round(possible / max(total, 1)),
        "rules": [{"name": r.name, "sql": r.sql,
                   "marginal": int(c["marginal_comparison_count"]),
                   "cumulative": int(c["cumulative_comparison_count"])}
                  for r, c in zip(rules, counts, strict=True)],
    }


def candidate_rows(preds: pd.DataFrame, model: TrainedModel) -> pd.DataFrame:
    from ireland_rsi.contracts import TABLE_SCHEMAS

    out = pd.DataFrame({
        "model_version": model.version, "track": model.track,
        "sale_id_l": preds.unique_id_l, "sale_id_r": preds.unique_id_r,
        "match_weight": preds.match_weight.astype(float),
        "match_probability": preds.match_probability.astype(float),
        "rules_hit": preds.rules_hit, "baseline_link": preds.baseline_link.astype(bool),
    })
    for g in GAMMAS:
        col = f"gamma_{g}"
        out[col] = (preds[col].astype("Int32") if col in preds
                    else pd.array([pd.NA] * len(preds), dtype="Int32"))
    return out[TABLE_SCHEMAS["silver.link_candidates"].names]


def log_to_mlflow(model: TrainedModel, report: dict, inputs: dict,
                  experiment: str) -> str | None:
    """Log parameters, blocking metrics and the model JSON to MLflow, if available."""
    import json as _json
    import tempfile

    try:
        import mlflow
    except ImportError:
        return None
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=f"{model.track}-{model.version}") as run:
        mlflow.set_tags({"track": model.track, "model_version": model.version, **inputs})
        mlflow.log_params({"prior_recall_assumption": PRIOR_RECALL,
                           "assumed_m": _json.dumps(ASSUMED_M),
                           "em_sessions": ",".join(n for n, _ in EM_SESSIONS),
                           "blocking_rules": ",".join(r["name"] for r in report["rules"])})
        mlflow.log_metrics({
            "records": report["records"], "candidate_pairs": report["candidate_pairs"],
            "reduction_ratio": report["reduction_ratio"],
            **{f"rule_marginal_{r['name']}": r["marginal"] for r in report["rules"]},
            **{k: v for k, v in model.training_log.items() if isinstance(v, (int, float))}})
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/splink_model_{model.track}.json"
            with open(path, "w", encoding="utf-8") as fh:
                _json.dump(model.settings_json, fh, indent=2, default=str)
            mlflow.log_artifact(path)
        return run.info.run_id


def run_linkage(tables, objects, scope: str, track: str, run,
                mlflow_experiment: str | None = None) -> dict:
    """Train and score one track for the active snapshot, persisting candidates and model."""
    import json as _json
    from datetime import UTC, datetime

    from ireland_rsi.contracts import TABLE_SCHEMAS
    from ireland_rsi.ingest import active_snapshot
    from ireland_rsi.storage import latest_stage_version, record_stage

    snapshot = active_snapshot(tables, scope)
    rule_set = latest_stage_version(tables, "clean", snapshot)
    address_version = latest_stage_version(tables, "addresses", snapshot)
    frame = linkage_frame(tables, snapshot, address_version, rule_set)
    df = linkage_input(frame, track)
    report = blocking_report(df, track)
    model = train(df, track)
    preds = score_candidates(model)
    settings_text = _json.dumps(model.settings_json, sort_keys=True, default=str)
    settings_sha, _, _ = objects.put(settings_text.encode("utf-8"), ".json")
    inputs = {"snapshot_id": snapshot, "rule_set_version": rule_set,
              "address_version": address_version}
    counts = {"candidates_inserted": tables.insert_new("silver.link_candidates",
                                                       candidate_rows(preds, model))}
    model_row = pd.DataFrame([{
        "model_version": model.version, "track": track, "trained_at": datetime.now(UTC),
        **inputs, "records": len(df), "candidate_pairs": len(preds),
        "settings_sha256": settings_sha, "settings_json": settings_text,
        "blocking_report_json": _json.dumps(report),
        "training_log_json": _json.dumps(model.training_log), "run_id": run.run_id,
    }])[TABLE_SCHEMAS["silver.link_models"].names]
    counts["model_inserted"] = tables.insert_new("silver.link_models", model_row)
    record_stage(tables, f"linkage_{track}", snapshot, model.version, run.run_id,
                 datetime.now(UTC))
    mlflow_run = (log_to_mlflow(model, report, inputs, mlflow_experiment)
                  if mlflow_experiment else None)
    return {"track": track, "model_version": model.version, **inputs,
            "blocking": report, "training": model.training_log, "row_counts": counts,
            "scored_pairs": len(preds),
            "probability_bands": {f">={t}": int((preds.match_probability >= t).sum())
                                  for t in (0.5, 0.9, 0.99)},
            "baseline_links": int(preds.baseline_link.sum()), "mlflow_run_id": mlflow_run}
