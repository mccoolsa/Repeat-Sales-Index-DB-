"""Lookup backtest and property prediction intervals.

The lookup estimate is

    estimated_price = anchor_price * index[target] / index[anchor]

Each repeat pair is a test case: its first sale is the anchor, its second sale the truth.
Two rules keep the test honest:

* **No future data.** For target year Y the index is fitted only on pairs whose later sale
  is in or before Y. A full-history index is never used to score an earlier sale.
* **No self-evidence.** Properties are split into folds; a fold is predicted from an index
  fitted without any of its pairs (grouped cross-fitting).

There is one present-day source snapshot, so filing delays cannot be replayed: this is a
retrospective temporal backtest, not a measurement of real-time performance.

Property prediction intervals are empirical quantiles of out-of-sample log errors in the
calibration window, by holding-period band, and are tested on a later, untouched window.
Those errors already contain index estimation error, so index uncertainty is not added again.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ireland_rsi.index import IdentifiabilityError, fit_index
from ireland_rsi.storage import sha256_hex
from ireland_rsi.validate import wilson


def fold_of(property_id: str, folds: int) -> int:
    """Deterministic fold from the property ID (stable across runs and machines)."""
    return int(sha256_hex(property_id.encode())[:8], 16) % folds


def window_of(year: int, windows: dict) -> str | None:
    for name, (lo, hi) in windows.items():
        if lo <= year <= hi:
            return name
    return None


def training_pairs(pairs: pd.DataFrame, target_year: int, fold: int) -> pd.DataFrame:
    """Pairs a fold's index may use: later sale by the end of the target year, and no pair
    of any property in the evaluated fold."""
    year_2 = pd.to_datetime(pairs.date_2).dt.year
    return pairs[(year_2 <= target_year) & (pairs.fold != fold)].reset_index(drop=True)


@dataclass
class FoldSeries:
    """One fold fit reduced to what a lookup needs: supported periods and their levels."""
    levels: dict[str, float]
    properties: int

    def ratio(self, anchor: str, target: str, min_properties: int) -> float | None:
        if (self.properties < min_properties or anchor not in self.levels
                or target not in self.levels):
            return None
        return self.levels[target] / self.levels[anchor]


def fold_series(train: pd.DataFrame, cfg: dict, index_cfg: dict) -> FoldSeries:
    try:
        fit = fit_index(train, cfg["frequency"], index_cfg, "wrs")
    except (IdentifiabilityError, ValueError, np.linalg.LinAlgError):
        return FoldSeries({}, 0)
    properties = int(train.iloc[fit.used].property_id.nunique())
    ok = fit.endpoint_pairs >= cfg["min_endpoint_pairs"]
    levels = {p: float(v) for p, v, keep in zip(fit.periods, fit.index, ok, strict=True) if keep}
    return FoldSeries(levels, properties)


def predict_pairs(pairs: pd.DataFrame, cfg: dict, index_cfg: dict, town: dict | None,
                  national: pd.Series | None) -> pd.DataFrame:
    """Out-of-sample predictions for every evaluable pair and method.

    `pairs`: pair_id, property_id, date_1, date_2, log_return, holding_years, price_1_eur,
    price_2_eur, town_2, conf_2. `town`: the index geography config for the town level of
    the fallback chain (or None). `national`: annual CSO national levels (or None).
    A method without a supported series returns status 'unsupported' and no amount; it
    never substitutes zero or the nominal price.
    """
    data = pairs.copy()
    data["fold"] = [fold_of(p, cfg["folds"]) for p in data.property_id]
    y1 = pd.to_datetime(data.date_1).dt.year
    y2 = pd.to_datetime(data.date_2).dt.year
    lo = min(v[0] for v in cfg["windows"].values())
    hi = max(v[1] for v in cfg["windows"].values())
    in_town = pd.Series(False, index=data.index)
    if town is not None:
        in_town = (data.town_2 == town["locality_name"]) & data.conf_2.isin(town["confidence"])
    rows = []
    for year in range(lo, hi + 1):
        evaluate = data[(y2 == year) & (y1 < year)]
        if evaluate.empty:
            continue
        for fold in range(cfg["folds"]):
            test = evaluate[evaluate.fold == fold]
            if test.empty:
                continue
            train = training_pairs(data, year, fold)
            county = fold_series(train, cfg, index_cfg)
            local = None
            if town is not None and in_town[test.index].any():
                t = train[(train.town_1 == town["locality_name"])
                          & (train.town_2 == town["locality_name"])
                          & train.conf_1.isin(town["confidence"])
                          & train.conf_2.isin(town["confidence"])]
                local = fold_series(t, cfg, index_cfg)
            need = cfg["min_series_properties"]
            for idx, r in test.iterrows():
                a, b = str(pd.Timestamp(r.date_1).year), str(year)
                base = {"pair_id": r.pair_id, "property_id": r.property_id, "fold": fold,
                        "window": window_of(year, cfg["windows"]), "target_year": year,
                        "anchor_period": a, "target_period": b,
                        "holding_years": float(r.holding_years), "actual_eur": r.price_2_eur}
                county_ratio = county.ratio(a, b, need)
                options = {
                    "nominal": ("none", 0, 1.0),
                    "national_cso": ("national_cso", 0, None if national is None or a not in
                                     national.index or b not in national.index
                                     else float(national[b] / national[a])),
                    "county": ("scope", 0, county_ratio),
                }
                # The lookup chain: the property's town first (level 0) when it has one,
                # then Donegal. Properties outside the town start at Donegal (level 0).
                if in_town[idx]:
                    local_ratio = None if local is None else local.ratio(a, b, need)
                    chain = [(town["id"], local_ratio), ("scope", county_ratio)]
                    # Diagnostic only: would a thinner town series have helped?
                    relaxed = None if local is None else local.ratio(
                        a, b, cfg["diagnostic_min_town_properties"])
                    options["town_relaxed"] = (town["id"], 0, relaxed)
                else:
                    chain = [("scope", county_ratio)]
                level, (geo, ratio) = next(((i, c) for i, c in enumerate(chain)
                                            if c[1] is not None), (None, (None, None)))
                options["local"] = (geo, level, ratio)
                for method, (geo, level, ratio) in options.items():
                    pred = None if ratio is None else r.price_1_eur * ratio
                    rows.append({**base, "method": method, "geography_id": geo,
                                 "fallback_level": level, "in_town": bool(in_town[idx]),
                                 "predicted_eur": pred,
                                 "log_error": None if pred is None
                                 else math.log(r.price_2_eur / pred),
                                 "status": "ok" if pred is not None else "unsupported"})
    return pd.DataFrame(rows)


def horizon_band(years: float, bands: list[dict]) -> str:
    for b in bands:
        if b["min"] <= years < b["max"]:
            return b["band"]
    return bands[-1]["band"]


def calibrate_intervals(pred: pd.DataFrame, cfg: dict, method: str = "local") -> pd.DataFrame:
    """Interval quantiles from calibration-window errors; coverage on the test window."""
    ic = cfg["intervals"]
    level = ic["level"]
    q = [(1 - level) / 2, (1 + level) / 2]
    ok = pred[(pred.method == method) & (pred.status == "ok")].copy()
    ok["band"] = [horizon_band(y, ic["horizon_bands"]) for y in ok.holding_years]
    rows = []
    for b in ic["horizon_bands"]:
        cal = ok[(ok.window == "calibration") & (ok.band == b["band"])].log_error
        test = ok[(ok.window == "test") & (ok.band == b["band"])].log_error
        row = {"horizon_band": b["band"], "min_years": float(b["min"]),
               "max_years": float(b["max"]), "level": level, "n_calibration": len(cal),
               "q_lo": None, "q_hi": None, "n_test": len(test), "coverage_test": None,
               "coverage_lo": None, "coverage_hi": None, "median_width": None}
        if len(cal) < ic["min_calibration_pairs"]:
            row["status"] = "insufficient_calibration"
            rows.append(row)
            continue
        q_lo, q_hi = np.quantile(cal, q)
        row.update(q_lo=float(q_lo), q_hi=float(q_hi),
                   median_width=float(math.exp(q_hi) - math.exp(q_lo)))
        if len(test):
            inside = int(((test >= q_lo) & (test <= q_hi)).sum())
            cov_lo, cov_hi = wilson(inside, len(test))
            row.update(coverage_test=inside / len(test), coverage_lo=cov_lo, coverage_hi=cov_hi)
            row["status"] = ("ok" if inside / len(test) >= level - ic["coverage_tolerance"]
                             else "coverage_failed")
        else:
            row["status"] = "untested"
        rows.append(row)
    return pd.DataFrame(rows)


def error_metrics(pred: pd.DataFrame) -> dict:
    """Accuracy of predictions with status ok (amounts in euro, errors as log points)."""
    ok = pred[pred.status == "ok"]
    if ok.empty:
        return {"n": 0}
    ape = (ok.predicted_eur / ok.actual_eur - 1).abs()
    return {
        "n": int(len(ok)),
        "median_abs_pct_error": float(ape.median() * 100),
        "mean_abs_eur_error": float((ok.predicted_eur - ok.actual_eur).abs().mean()),
        "mean_log_error": float(ok.log_error.mean()),     # positive: estimates too low
        "rmse_log": float(np.sqrt((ok.log_error ** 2).mean())),
    }


MAIN_METHODS = ("local", "county", "national_cso", "nominal")


def summarise(pred: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Metrics by window and method.

    * ``common``: pairs every main method could predict, so methods are compared on the
      same cases;
    * ``band=...``: the common pairs by holding period;
    * ``town=relaxed_comparable``: town properties the relaxed town series and Donegal
      could both predict, to test whether a town index adds anything;
    * ``series=...``: the lookup method by the series it used;
    * ``support_rate``: share of each method's cases with an estimate.
    """
    main = pred[pred.method.isin(MAIN_METHODS)]
    supported = main[main.status == "ok"].groupby("pair_id").method.nunique()
    common = set(supported[supported == len(MAIN_METHODS)].index)
    relaxed = pred[(pred.method == "town_relaxed") & (pred.status == "ok")].pair_id
    county = pred[(pred.method == "county") & (pred.status == "ok")].pair_id
    town_cases = set(relaxed) & set(county)
    bands = cfg["intervals"]["horizon_bands"]
    p = pred.assign(band=[horizon_band(y, bands) for y in pred.holding_years])
    rows = []
    for (window, method), g in p.groupby(["window", "method"]):
        rows.append({"window": window, "method": method, "segment": "support_rate",
                     "metric": "support_rate", "n": g.pair_id.nunique(),
                     "value": float((g.status == "ok").mean())})
        segments = [("common", g[g.pair_id.isin(common)])]
        segments += [(f"band={b}", s[s.pair_id.isin(common)]) for b, s in g.groupby("band")]
        if method in ("town_relaxed", "county"):
            segments.append(("town=relaxed_comparable", g[g.pair_id.isin(town_cases)]))
        if method == "local":
            segments += [(f"series={geo}", s) for geo, s in g.groupby("geography_id")]
        for seg_name, seg in segments:
            m = error_metrics(seg)
            for k, v in m.items():
                if k != "n":
                    rows.append({"window": window, "method": method, "segment": seg_name,
                                 "metric": k, "n": m["n"], "value": v})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_backtest(tables, objects, root, scope: str, cfg: dict, cfg_bytes: bytes,
                 index_cfg: dict, validation_cfg: dict, cso_spec: dict, run) -> dict:
    from datetime import UTC, datetime
    from pathlib import Path

    from ireland_rsi.contracts import TABLE_SCHEMAS
    from ireland_rsi.index import _pairs_with_geography
    from ireland_rsi.ingest import active_snapshot
    from ireland_rsi.storage import latest_stage_version, record_stage
    from ireland_rsi.validate import _result, cso_series, load_cso, monthly_to

    snapshot = active_snapshot(tables, scope)
    entity_version = latest_stage_version(tables, "entities", snapshot)
    rule_set = latest_stage_version(tables, "clean", snapshot)
    address_version = latest_stage_version(tables, "addresses", snapshot)
    validation_version = latest_stage_version(tables, "validation", snapshot)
    if not validation_version:
        raise RuntimeError("validation must exist for the snapshot (validation check)")
    version = "bv-" + sha256_hex(cfg_bytes + Path(__file__).read_bytes()
                                 + (Path(__file__).parent / "index.py").read_bytes()
                                 + f"{entity_version}|{validation_version}".encode())[:12]
    pairs = _pairs_with_geography(tables, entity_version, rule_set, address_version,
                                  ["included_headline"])
    prices = tables.query(
        "SELECT pair_id, price_cents_1, price_cents_2 "
        f"FROM {tables.qualify('silver.repeat_pairs')} WHERE entity_version = '{entity_version}'")
    pairs = pairs.merge(prices, on="pair_id")
    pairs["price_1_eur"] = pairs.price_cents_1 / 100
    pairs["price_2_eur"] = pairs.price_cents_2 / 100

    spec = cso_spec["tables"]["HPM09"]
    raw = cso_series(load_cso(objects, root, validation_cfg["cso_extract"], "HPM09"), spec,
                     spec["series"]["national_all"]["code"])
    national = monthly_to(raw, "annual")
    town = next((g for g in index_cfg["geographies"] if g["id"] != "scope"), None)

    pred = predict_pairs(pairs, cfg, index_cfg, town, national)
    pred.insert(0, "backtest_version", version)
    calib = calibrate_intervals(pred, cfg)
    calib.insert(0, "backtest_version", version)
    summary = summarise(pred, cfg)

    results = [_result(version, "backtest", r.method, "scope", cfg["frequency"],
                       r.metric, f"{r.window}|{r.segment}", r.value, r.n)
               for r in summary.itertuples(index=False)]
    for r in calib.itertuples(index=False):
        results.append(_result(version, "calibration", "local", "scope", cfg["frequency"],
                               "coverage_test", r.horizon_band, r.coverage_test, r.n_test,
                               {"status": r.status, "q_lo": r.q_lo, "q_hi": r.q_hi,
                                "coverage_lo": r.coverage_lo, "coverage_hi": r.coverage_hi}))
    now = datetime.now(UTC)
    counts = {
        "predictions_inserted": tables.insert_new(
            "gold.backtest_predictions",
            pred[TABLE_SCHEMAS["gold.backtest_predictions"].names]),
        "calibration_inserted": tables.insert_new(
            "gold.prediction_calibration",
            calib[TABLE_SCHEMAS["gold.prediction_calibration"].names]),
        "results_inserted": tables.insert_new(
            "gold.validation_results",
            pd.DataFrame(results)[TABLE_SCHEMAS["gold.validation_results"].names]),
    }
    record_stage(tables, "backtest", snapshot, version, run.run_id, now)
    test = summary[(summary.window == "test") & (summary.segment == "common")]
    table = test.pivot_table(index="method", columns="metric", values="value").round(3)
    return {"snapshot_id": snapshot, "backtest_version": version,
            "experiment": cfg["experiment"], "row_counts": counts,
            "test_window_common_pairs": table.reset_index().to_dict("records"),
            "calibration": calib.drop(columns="backtest_version").round(4).to_dict("records"),
            "support_rate": summary[summary.metric == "support_rate"]
            .pivot_table(index="method", columns="window", values="value").round(3)
            .reset_index().to_dict("records"),
            "town_diagnostic": summary[summary.segment == "town=relaxed_comparable"]
            .pivot_table(index=["window", "method"], columns="metric", values="value")
            .round(3).reset_index().to_dict("records"),
            "local_by_series_test": summary[
                (summary.window == "test") & (summary.method == "local")
                & summary.segment.str.startswith("series")].round(4).to_dict("records")}
