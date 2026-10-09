"""Validation against the CSO and methodological sensitivity.

The CSO Residential Property Price Index is a benchmark, not ground truth: it covers
household purchases only, applies hedonic (mix) adjustment, and is published for regions
rather than towns. Comparisons therefore use *growth rates* over complete, aligned periods.
High correlation between two trending level series is not evidence of anything.

Sensitivity experiments rebuild property histories, pairs and index fits from the same
linkage scores under predeclared alternatives (link threshold, rule-based links, new-build
first legs, estimator, anomaly policy, Eircode subsets, frequency). Every variant is
recorded with its own settings; large shifts are flagged for investigation, never used to
pick the smoothest result.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ireland_rsi.index import IdentifiabilityError, fit_index


class BenchmarkError(ValueError):
    """The benchmark extract is not a compatible price index."""


# ---------------------------------------------------------------------------
# CSO series
# ---------------------------------------------------------------------------


def cso_series(data: dict, table_spec: dict, series_code: str) -> pd.Series:
    """Index levels of one CSO series from a JSON-stat 2.0 extract.

    Monthly periods are returned as "YYYY-MM", quarterly as "YYYYQn". The extract must
    carry the index statistic with unit "Base 2015=100"; a median price or a percentage
    change is rejected rather than treated as an index.
    """
    stat_id = table_spec["index_statistic"]
    stat = data["dimension"]["STATISTIC"]["category"]
    label = stat.get("label", {}).get(stat_id)
    unit = stat.get("unit", {}).get(stat_id, {}).get("label")
    if label != "Residential Property Price Index" or unit != "Base 2015=100":
        raise BenchmarkError(f"{stat_id} is '{label}' in '{unit}', not an index with base 2015")
    dims, sizes, values = data["id"], data["size"], data["value"]
    index = {d: data["dimension"][d]["category"]["index"] for d in dims}
    tdim, sdim = table_spec["time_dimension"], table_spec["series_dimension"]
    if series_code not in index[sdim]:
        raise BenchmarkError(f"series code {series_code} not in {sdim}")
    out = {}
    for t in index[tdim]:
        sel = {"STATISTIC": stat_id, tdim: t, sdim: series_code}
        pos = 0
        for d, n in zip(dims, sizes, strict=True):
            pos = pos * n + index[d].index(sel[d])
        v = values[pos] if isinstance(values, list) else values.get(str(pos))
        if v is None:
            continue
        period = f"{t[:4]}-{t[4:]}" if table_spec["frequency"] == "monthly" else f"{t[:4]}Q{t[4:]}"
        out[period] = float(v)
    return pd.Series(out, dtype=float).sort_index()


def monthly_to(series: pd.Series, frequency: str, exclude_months: set[str] = frozenset()
               ) -> pd.Series:
    """Mean of monthly levels per complete quarter or year (validation alignment).

    A period is complete only if every month is present and none is excluded (e.g. the
    CSO's provisional months); incomplete periods are omitted, not partially averaged.
    """
    months = series[~series.index.isin(exclude_months)]
    if frequency == "quarterly":
        key = [f"{m[:4]}Q{(int(m[5:]) - 1) // 3 + 1}" for m in months.index]
        need = 3
    elif frequency == "annual":
        key = [m[:4] for m in months.index]
        need = 12
    else:
        raise ValueError(frequency)
    g = months.groupby(key)
    return g.mean()[g.size() == need].sort_index()


def quarterly_to_annual(series: pd.Series) -> pd.Series:
    g = series.groupby([p[:4] for p in series.index])
    return g.mean()[g.size() == 4].sort_index()


def shift_period(period: str, n: int) -> str:
    if "Q" in period:
        y, q = int(period[:4]), int(period[5:])
        k = y * 4 + (q - 1) + n
        return f"{k // 4}Q{k % 4 + 1}"
    return str(int(period) + n)


def log_growth(levels: pd.Series, lag: int) -> pd.Series:
    """log(level_t / level_{t-lag}) where both periods exist; invariant to rebasing."""
    out = {}
    for p, v in levels.items():
        prev = shift_period(p, -lag)
        if prev in levels.index:
            out[p] = math.log(v / levels[prev])
    return pd.Series(out, dtype=float)


def compare_growth(ours: pd.Series, bench: pd.Series, lag: int) -> dict:
    """Agreement of growth rates over common periods, in log points x 100."""
    g1, g2 = log_growth(ours, lag) * 100, log_growth(bench, lag) * 100
    common = g1.index.intersection(g2.index)
    if len(common) < 3:
        return {"n": int(len(common))}
    a, b = g1[common], g2[common]
    d = a - b
    lv = ours.index.intersection(bench.index)
    return {
        "n": int(len(common)),
        "first": str(common.min()), "last": str(common.max()),
        "bias": float(d.mean()),
        "mean_abs_diff": float(d.abs().mean()),
        "rmse": float(np.sqrt((d ** 2).mean())),
        "correlation": float(np.corrcoef(a, b)[0, 1]),
        "direction_agreement": float((np.sign(a) == np.sign(b)).mean()),
        "trough_ours": str(ours[lv].idxmin()) if len(lv) else None,
        "trough_benchmark": str(bench[lv].idxmin()) if len(lv) else None,
    }


# ---------------------------------------------------------------------------
# Threshold evidence from human labels (input to the link-threshold gate)
# ---------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def threshold_precision(sample: pd.DataFrame, labels: pd.DataFrame,
                        candidates: pd.DataFrame, thresholds: list[float]) -> pd.DataFrame:
    """Precision of accepted links at each threshold, from reviewed sample pairs.

    `sample`: pair_id, sale_id_a, sale_id_b, sampling_prob. `labels`: current primary label
    per pair. `candidates`: sale_id_l, sale_id_r, match_probability. Weighted precision uses
    inverse sampling probabilities (the sample is stratified); the Wilson interval is on the
    unweighted counts and is indicative only. "Can't tell" labels are excluded.
    """
    prob = {}
    for c in candidates.itertuples(index=False):
        prob[frozenset((c.sale_id_l, c.sale_id_r))] = float(c.match_probability)
    df = sample.merge(labels[["pair_id", "label"]], on="pair_id")
    df = df[df.label.isin(["match", "non_match"])].copy()
    df["p"] = [prob.get(frozenset((a, b)), 0.0) for a, b in zip(df.sale_id_a, df.sale_id_b,
                                                               strict=True)]
    df["w"] = 1.0 / df.sampling_prob
    rows = []
    total_matches_w = float((df.w * (df.label == "match")).sum())
    for t in thresholds:
        acc = df[df.p >= t]
        k = int((acc.label == "match").sum())
        n = len(acc)
        wk = float((acc.w * (acc.label == "match")).sum())
        wn = float(acc.w.sum())
        lo, hi = wilson(k, n)
        rows.append({
            "threshold": t, "accepted_labelled": n, "accepted_matches": k,
            "precision": k / n if n else None, "precision_lo": lo, "precision_hi": hi,
            "weighted_precision": wk / wn if wn else None,
            "weighted_recall_within_sample": wk / total_matches_w if total_matches_w else None,
        })
    return pd.DataFrame(rows)


def labels_digest(labels: pd.DataFrame) -> str:
    """Fingerprint of the label table, so validation re-versions when labels change."""
    from ireland_rsi.storage import sha256_hex

    if labels.empty:
        return "no-labels"
    cols = ["sample_version", "pair_id", "reviewer", "labelled_at", "label"]
    df = labels[cols].copy()
    # Same text on Spark and DuckDB: UTC, microseconds, no zone suffix.
    df["labelled_at"] = pd.to_datetime(df.labelled_at, utc=True).dt.strftime(
        "%Y-%m-%dT%H:%M:%S.%f")
    # Fixed line endings: to_csv defaults to os.linesep (\r\n on Windows, \n on Linux).
    rows = df.astype(str).sort_values(cols).to_csv(index=False, lineterminator="\n")
    return sha256_hex(rows.encode())[:16]


def final_link_metrics(sample: pd.DataFrame, labels: pd.DataFrame, membership: pd.DataFrame,
                       reps: int, seed: int) -> dict:
    """Precision and recall of the final property links against human labels (link-threshold gate).

    A labelled pair counts as linked when both sales end in the same property after the
    clustering constraints, which is what the index actually uses. "Can't tell" labels
    are excluded. Weights are inverse sampling probabilities; the intervals come from a
    bootstrap that resamples labelled pairs within each sampling stratum.
    """
    prop = membership.set_index("sale_id").property_id
    df = sample.merge(labels[["pair_id", "label"]], on="pair_id")
    df = df[df.label.isin(["match", "non_match"])].reset_index(drop=True)
    linked = np.array([prop.get(a) is not None and prop.get(a) == prop.get(b)
                       for a, b in zip(df.sale_id_a, df.sale_id_b, strict=True)], dtype=bool)
    y = (df.label == "match").to_numpy()
    w = (1.0 / df.sampling_prob).to_numpy()

    def stats(i):
        L, Y, W = linked[i], y[i], w[i]
        wl, wy = (W * L).sum(), (W * Y).sum()
        return (((W * (L & Y)).sum() / wl) if wl else np.nan,
                ((W * (L & Y)).sum() / wy) if wy else np.nan)

    wp, wr = stats(np.arange(len(df)))
    rng = np.random.default_rng(seed)
    groups = [np.flatnonzero(df.stratum.to_numpy() == s) for s in sorted(df.stratum.unique())]
    boots = np.array([stats(np.concatenate([rng.choice(g, len(g)) for g in groups]))
                      for _ in range(reps)]) if reps and len(df) else np.empty((0, 2))
    k, n = int((linked & y).sum()), int(linked.sum())
    p_lo, p_hi = wilson(k, n)

    def pct(col, q):
        v = boots[:, col] if len(boots) else np.array([np.nan])
        v = v[~np.isnan(v)]
        return float(np.percentile(v, q)) if len(v) else None

    return {"labelled_binary": len(df), "linked_labelled": n, "false_links": n - k,
            "precision": k / n if n else None, "p_lo": p_lo, "p_hi": p_hi,
            "weighted_precision": None if np.isnan(wp) else float(wp),
            "wp_lo": pct(0, 2.5), "wp_hi": pct(0, 97.5),
            "weighted_recall": None if np.isnan(wr) else float(wr),
            "wr_lo": pct(1, 2.5), "wr_hi": pct(1, 97.5)}


# ---------------------------------------------------------------------------
# Sensitivity experiments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Experiment:
    id: str
    description: str
    threshold: float
    links: str = "probabilistic"          # or "deterministic"
    decisions: tuple[str, ...] = ("included_headline",)
    method: str = "wrs"
    frequency: str = "annual"
    exclude_flags: tuple[str, ...] = ()
    subset: str = "all"                    # all | eircode_both | from_2015

    @property
    def link_key(self) -> tuple[str, float]:
        return (self.links, self.threshold if self.links == "probabilistic" else -1.0)

    def settings(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


def experiment_grid(cfg: dict, baseline_threshold: float) -> list[Experiment]:
    """Predeclared variants: one per configured experiment plus a threshold sweep."""
    out = []
    for e in cfg["experiments"]:
        out.append(Experiment(
            id=e["id"], description=e["description"], threshold=baseline_threshold,
            links=e.get("links", "probabilistic"),
            decisions=tuple(e.get("decisions", ["included_headline"])),
            method=e.get("method", "wrs"), frequency=e.get("frequency", cfg["frequency"]),
            exclude_flags=tuple(e.get("exclude_flags", [])), subset=e.get("subset", "all")))
    for t in cfg["thresholds"]:
        if t != baseline_threshold:
            out.append(Experiment(id=f"threshold_{t:g}", threshold=t,
                                  description=f"Accept links with match probability >= {t:g}"))
    return out


def select_pairs(pairs: pd.DataFrame, exp: Experiment) -> pd.DataFrame:
    """Population, anomaly policy and subset of one experiment."""
    df = pairs[pairs.decision.isin(exp.decisions)]
    for flag in exp.exclude_flags:
        df = df[~df[flag].astype(bool)]
    if exp.subset == "eircode_both":
        df = df[df.eircode_1.notna() & (df.eircode_1 == df.eircode_2)]
    elif exp.subset == "from_2015":
        df = df[pd.to_datetime(df.date_1).dt.year >= 2015]
    elif exp.subset != "all":
        raise ValueError(f"unknown subset {exp.subset}")
    return df.reset_index(drop=True)


def experiment_series(pairs: pd.DataFrame, exp: Experiment, cfg: dict, min_pairs: int
                      ) -> pd.Series:
    """Annual index levels of one experiment, keeping only supported periods.

    A period needs at least `min_pairs` endpoint pairs (a quarter needs a quarter of that),
    so a subset whose base year rests on one or two pairs is not compared as if it were
    estimated. Quarterly fits are averaged to complete years.
    """
    fit = fit_index(pairs, exp.frequency, cfg, exp.method)
    need = min_pairs if exp.frequency == "annual" else max(1, min_pairs // 4)
    levels = pd.Series(fit.index, index=fit.periods)[fit.endpoint_pairs >= need]
    return quarterly_to_annual(levels) if exp.frequency == "quarterly" else levels


def growth_between(levels: pd.Series, start: str, end: str) -> float | None:
    if start in levels.index and end in levels.index:
        return float(math.log(levels[end] / levels[start]))
    return None


def common_window(a: pd.Series, b: pd.Series, start: str, end: str) -> tuple[str, str] | None:
    """The requested window if both series support it, else the widest common one inside it."""
    common = sorted(p for p in a.index.intersection(b.index) if start <= p <= end)
    return (common[0], common[-1]) if len(common) >= 2 else None


def max_common_log_diff(a: pd.Series, b: pd.Series) -> float | None:
    """Largest gap in log level over common periods after rebasing both to their first
    common period; None if fewer than two common periods."""
    common = a.index.intersection(b.index).sort_values()
    if len(common) < 2:
        return None
    la = np.log(a[common] / a[common[0]])
    lb = np.log(b[common] / b[common[0]])
    return float(np.max(np.abs(la - lb)))


# ---------------------------------------------------------------------------
# Selection: how resale properties differ from all eligible sales
# ---------------------------------------------------------------------------


def selection_profile(sales: pd.DataFrame, pairs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Share of all index-eligible sales and of repeat-sale legs by segment.

    `sales`: sale_id, year, price_eur, dwelling_status, locality. A positive gap means the
    segment is over-represented among repeat sales. No reweighting is applied: there is no
    suitable reference stock to weight to.
    """
    legs = set(pairs.sale_id_1) | set(pairs.sale_id_2)
    s = sales.copy()
    s["in_pairs"] = s.sale_id.isin(legs)
    bands = cfg["price_bands_eur"]
    labels = [f"{bands[i] // 1000}k-{bands[i + 1] // 1000}k" if i + 2 < len(bands)
              else f"{bands[i] // 1000}k+" for i in range(len(bands) - 1)]
    s["price_band"] = pd.cut(s.price_eur, bands, labels=labels, right=False).astype(str)
    top = s.locality.fillna("(unassigned)").value_counts().index[: cfg["top_localities"]]
    s["locality_group"] = np.where(s.locality.isin(top), s.locality, "Other / rural")
    s["period"] = (s.year // 4 * 4).astype(str) + "-" + (s.year // 4 * 4 + 3).astype(str)
    rows = []
    for dim, col in [("locality", "locality_group"), ("price_band", "price_band"),
                     ("description", "dwelling_status"), ("period", "period")]:
        all_share = s[col].value_counts(normalize=True)
        pair_share = s.loc[s.in_pairs, col].value_counts(normalize=True)
        n_pairs = s.loc[s.in_pairs, col].value_counts()
        for seg in sorted(all_share.index, key=str):
            rows.append({"dimension": dim, "segment": str(seg),
                         "share_all_sales": float(all_share[seg]),
                         "share_repeat_legs": float(pair_share.get(seg, 0.0)),
                         "repeat_legs": int(n_pairs.get(seg, 0)),
                         "all_sales": int((s[col] == seg).sum())})
    out = pd.DataFrame(rows)
    out["gap"] = out.share_repeat_legs - out.share_all_sales
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _result(version, category, experiment, geography, frequency, metric, segment, value,
            n=None, detail=None) -> dict:
    v = None if value is None or (isinstance(value, float) and not math.isfinite(value)) \
        else float(value)
    return {"validation_version": version, "category": category, "experiment": experiment,
            "geography_id": geography, "frequency": frequency, "metric": metric,
            "segment": segment, "value": v, "n": None if n is None else int(n),
            "detail": None if detail is None else json.dumps(detail, default=str)}


def load_cso(objects, root, cso_cfg: dict, table: str) -> dict:
    """Pinned CSO extract bytes from the object store, adding them from the local archive
    when first needed. The checksum, not the path, identifies the evidence."""
    from pathlib import Path

    from ireland_rsi.storage import sha256_hex

    digest = cso_cfg["files"][table]["sha256"]
    try:
        return json.loads(objects.get(digest, ".json"))
    except FileNotFoundError:
        pass
    archive = Path(root) / "data" / "raw" / "cso" / cso_cfg["acquisition_id"].removeprefix(
        "cso-rppi-") / f"{table}.json-stat.json"
    if not archive.exists():
        raise FileNotFoundError(
            f"CSO extract {table} ({digest[:12]}) is not in the object store; upload the "
            f"archived file to {objects.object_path(digest, '.json')}")
    data = archive.read_bytes()
    if sha256_hex(data) != digest:
        raise BenchmarkError(f"archived {table} does not match the pinned checksum")
    objects.put(data, ".json")
    return json.loads(data)


def rebuild_pairs(sales: pd.DataFrame, edges: pd.DataFrame, entity_cfg, label: str
                  ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pairs and property membership from one set of accepted edges, in memory."""
    from ireland_rsi.entities import resolve_entities
    from ireland_rsi.pairs import build_repeat_pairs

    membership, _ = resolve_entities(sales, edges, entity_cfg, label)
    history = membership[["property_id", "sale_id"]].merge(sales, on="sale_id")
    pairs, _ = build_repeat_pairs(history, entity_cfg.raw["pairs"], label)
    eir = dict(zip(sales.sale_id, sales.eircode, strict=True))
    pairs["eircode_1"] = pairs.sale_id_1.map(eir)
    pairs["eircode_2"] = pairs.sale_id_2.map(eir)
    return pairs, membership


def _with_towns(pairs: pd.DataFrame, towns: pd.DataFrame) -> pd.DataFrame:
    t = towns.set_index("sale_id")
    out = pairs.copy()
    for i in (1, 2):
        out[f"town_{i}"] = out[f"sale_id_{i}"].map(t.locality_name)
        out[f"conf_{i}"] = out[f"sale_id_{i}"].map(t.locality_confidence)
    return out


def run_validation(tables, objects, root, profile_name: str, scope: str, cfg: dict,
                   cfg_bytes: bytes, cso_spec: dict, index_cfg: dict, entity_cfg, run) -> dict:
    """CSO growth comparison, threshold evidence, sensitivity grid and selection profile."""
    from datetime import UTC, datetime
    from pathlib import Path

    from ireland_rsi.contracts import TABLE_SCHEMAS
    from ireland_rsi.index import _select_geography
    from ireland_rsi.ingest import active_snapshot
    from ireland_rsi.labels import current_labels
    from ireland_rsi.pairs import history_inputs
    from ireland_rsi.storage import latest_stage_version, record_stage, sha256_hex

    snapshot = active_snapshot(tables, scope)
    rule_set = latest_stage_version(tables, "clean", snapshot)
    address_version = latest_stage_version(tables, "addresses", snapshot)
    entity_version = latest_stage_version(tables, "entities", snapshot)
    uncertainty_version = latest_stage_version(tables, "uncertainty", snapshot)
    track = entity_cfg.raw["linkage_track"]
    model_version = latest_stage_version(tables, f"linkage_{track}", snapshot)
    if not uncertainty_version:
        raise RuntimeError("uncertainty must exist for the snapshot")
    labels = (tables.query(f"SELECT * FROM {tables.qualify('silver.link_labels')}")
              if tables.exists("silver.link_labels") else pd.DataFrame())
    version = "vv-" + sha256_hex(cfg_bytes + Path(__file__).read_bytes()
                                 + f"{uncertainty_version}|{entity_version}".encode()
                                 + labels_digest(labels).encode())[:12]
    now = datetime.now(UTC)
    results: list[dict] = []
    series_rows: list[dict] = []
    report: dict = {"benchmarks": [], "sensitivity": [], "flags": []}

    # --- 1. CSO growth comparison against the published (uncertainty) series ---------
    unc = tables.query(
        "SELECT f.geography_id, f.frequency, u.period, u.index_value, u.period_state "
        f"FROM {tables.qualify('silver.index_uncertainty')} u "
        f"JOIN {tables.qualify('silver.index_fits')} f ON u.fit_id = f.fit_id "
        f"WHERE u.uncertainty_version = '{uncertainty_version}'")
    cso_tables = {t: load_cso(objects, root, cfg["cso_extract"], t)
                  for t in cfg["cso_extract"]["files"]}
    for b in cfg["benchmarks"].get(profile_name, []):
        spec = cso_spec["tables"][b["cso"]["table"]]
        raw = cso_series(cso_tables[b["cso"]["table"]], spec,
                         spec["series"][b["cso"]["series"]]["code"])
        provisional = set(raw.index[-cso_spec.get("provisional_months", 0):]) \
            if spec["frequency"] == "monthly" else set()
        bench = (monthly_to(raw, b["frequency"], provisional) if spec["frequency"] == "monthly"
                 else (raw if b["frequency"] == "quarterly" else quarterly_to_annual(raw)))
        ours_all = unc[(unc.geography_id == b["geography_id"]) & (unc.frequency == b["frequency"])]
        # Our provisional (newest, incomplete) period is not a complete comparison period.
        ours = ours_all[ours_all.period_state != "provisional"].set_index("period").index_value
        published = ours_all[ours_all.period_state == "supported"].set_index("period").index_value
        lag = 4 if b["frequency"] == "quarterly" else 1
        for label, series in [("all_periods", ours), ("published_periods", published)]:
            m = compare_growth(series.sort_index(), bench, lag)
            name = f"{b['cso']['table']}.{b['cso']['series']}"
            for k in ("bias", "mean_abs_diff", "rmse", "correlation", "direction_agreement"):
                if k in m:
                    results.append(_result(version, "benchmark", name, b["geography_id"],
                                           b["frequency"], f"yoy_{k}", label, m[k], m["n"],
                                           {"caveat": b["caveat"], "first": m.get("first"),
                                            "last": m.get("last")}))
            report["benchmarks"].append({"benchmark": name, "geography_id": b["geography_id"],
                                         "frequency": b["frequency"], "periods": label, **m})
        if b["frequency"] == "quarterly":
            m = compare_growth(ours.sort_index(), bench, 1)
            name = f"{b['cso']['table']}.{b['cso']['series']}"
            results.append(_result(version, "benchmark", name,
                                   b["geography_id"], b["frequency"], "qoq_mean_abs_diff",
                                   "all_periods", m.get("mean_abs_diff"), m["n"]))
        for p, v in bench.items():
            series_rows.append({"validation_version": version,
                                "experiment": f"cso:{b['cso']['series']}",
                                "geography_id": b["geography_id"], "frequency": b["frequency"],
                                "period": p, "index_value": float(v)})

    # --- 2. Link threshold evidence from human labels (link-threshold gate input) ------
    sens = cfg["sensitivity"]
    candidates = tables.query(
        "SELECT sale_id_l, sale_id_r, match_weight, match_probability, baseline_link "
        f"FROM {tables.qualify('silver.link_candidates')} "
        f"WHERE model_version = '{model_version}' AND track = '{track}'")
    labelled = 0
    primary = None
    if not labels.empty:
        sample = tables.query(
            "SELECT pair_id, sale_id_a, sale_id_b, sampling_prob, stratum "
            f"FROM {tables.qualify('silver.label_sample')}")
        cur = current_labels(labels)
        primary = cur.sort_values("labelled_at").groupby("pair_id").head(1)
        labelled = int(primary.pair_id.nunique())
        # Raw pair scores, before clustering constraints (diagnostic).
        tp = threshold_precision(sample, primary, candidates, sens["thresholds"])
        for r in tp.itertuples(index=False):
            for k in ("precision", "weighted_precision", "weighted_recall_within_sample"):
                results.append(_result(version, "linkage", "labels", "scope", None, k,
                                       f"threshold={r.threshold:g}", getattr(r, k),
                                       r.accepted_labelled,
                                       {"lo": r.precision_lo, "hi": r.precision_hi}
                                       if k == "precision" else None))
        report["threshold_precision"] = tp.to_dict("records")
    report["labelled_pairs"] = labelled

    # --- 3. Sensitivity grid ----------------------------------------------------------
    sales = history_inputs(tables, snapshot, rule_set, address_version)
    towns = tables.query(
        "SELECT DISTINCT s.sale_id, a.locality_name, a.locality_confidence "
        f"FROM {tables.qualify('silver.sales')} s "
        f"JOIN {tables.qualify('silver.address_features')} a "
        "ON s.snapshot_id = a.snapshot_id AND s.raw_row_id = a.raw_row_id "
        f"WHERE s.snapshot_id = '{snapshot}' AND s.rule_set_version = '{rule_set}' "
        f"AND a.address_version = '{address_version}'").drop_duplicates("sale_id")
    known = set(sales.sale_id)
    candidates = candidates[candidates.sale_id_l.isin(known) & candidates.sale_id_r.isin(known)]
    baseline_t = float(entity_cfg.raw["accept_threshold"])
    grid = experiment_grid(sens, baseline_t)
    geos = {g["id"]: g for g in index_cfg["geographies"]}
    pair_cache: dict = {}
    baseline_levels: dict = {}
    start, end = sens["growth_window"]
    for exp in grid:
        if exp.link_key not in pair_cache:
            edges = (candidates[candidates.baseline_link] if exp.links == "deterministic"
                     else candidates[candidates.match_probability >= exp.threshold])
            pairs, membership = rebuild_pairs(sales, edges, entity_cfg, f"exp|{exp.link_key}")
            pair_cache[exp.link_key] = (_with_towns(pairs, towns), len(edges))
            if primary is not None:
                # Link-threshold gate metric: are labelled pairs in the same *final* property?
                fl = final_link_metrics(sample, primary, membership,
                                        sens["final_link_bootstrap"], seed=0)
                segment = (f"threshold={exp.threshold:g}" if exp.links == "probabilistic"
                           else "deterministic")
                report.setdefault("final_links", []).append({"links": segment, **fl})
                for k in ("weighted_precision", "precision", "weighted_recall"):
                    lo, hi = {"weighted_precision": ("wp_lo", "wp_hi"),
                              "precision": ("p_lo", "p_hi"),
                              "weighted_recall": ("wr_lo", "wr_hi")}[k]
                    results.append(_result(version, "linkage", "final_links", "scope", None,
                                           k, segment, fl[k], fl["linked_labelled"],
                                           {"lo": fl[lo], "hi": fl[hi],
                                            "false_links": fl["false_links"]}))
        all_pairs, n_edges = pair_cache[exp.link_key]
        chosen = select_pairs(all_pairs, exp)
        for geo_id in sens["geographies"]:
            geo_pairs = _select_geography(chosen, geos[geo_id])
            row = {"experiment": exp.id, "geography_id": geo_id, "accepted_links": n_edges,
                   "pairs": len(geo_pairs),
                   "properties": int(geo_pairs.property_id.nunique()) if len(geo_pairs) else 0}
            try:
                levels = experiment_series(geo_pairs, exp, index_cfg, sens["min_endpoint_pairs"])
                if len(levels) < 2:
                    raise ValueError("fewer than two supported periods")
            except (IdentifiabilityError, ValueError, np.linalg.LinAlgError) as exc:
                row["status"] = f"failed: {exc}"
                report["sensitivity"].append(row)
                results.append(_result(version, "sensitivity", exp.id, geo_id, "annual",
                                       "pairs", "all", len(geo_pairs), len(geo_pairs),
                                       {"status": row["status"], **exp.settings()}))
                continue
            if exp.id == "baseline":
                baseline_levels[geo_id] = levels
            base = baseline_levels.get(geo_id)
            window = common_window(levels, base if base is not None else levels, start, end)
            growth = shift = None
            if window:
                growth = growth_between(levels, *window)
                shift = growth - growth_between(base, *window) if base is not None else None
            row.update(status="ok", periods=len(levels),
                       window=None if window is None else f"{window[0]}-{window[1]}",
                       log_growth=growth, growth_shift_vs_baseline=shift,
                       max_log_gap_vs_baseline=None if base is None else
                       max_common_log_diff(levels, base),
                       example_estimate_eur=None if growth is None or window != (start, end)
                       else round(sens["example_anchor_price_eur"] * math.exp(growth)))
            if shift is not None and abs(shift) > sens["flag_log_growth_shift"]:
                report["flags"].append(f"{exp.id} / {geo_id}: {row['window']} log growth "
                                       f"shifts by {shift:+.3f}")
            report["sensitivity"].append(row)
            for metric in ("pairs", "properties", "log_growth", "growth_shift_vs_baseline",
                           "max_log_gap_vs_baseline", "example_estimate_eur"):
                results.append(_result(version, "sensitivity", exp.id, geo_id, "annual", metric,
                                       row["window"] if "growth" in metric else
                                       f"{start}-{end}" if "example" in metric else "all",
                                       row.get(metric), row["pairs"],
                                       exp.settings() if metric == "pairs" else None))
            for p, v in levels.items():
                series_rows.append({"validation_version": version, "experiment": exp.id,
                                    "geography_id": geo_id, "frequency": "annual",
                                    "period": p, "index_value": float(v)})

    # --- 4. Selection of resale properties --------------------------------------------
    sel_sales = sales[sales.index_eligible].copy()
    sel_sales["year"] = pd.to_datetime(sel_sales.sale_date).dt.year
    sel_sales["price_eur"] = sel_sales.analysis_price_cents / 100
    sel_sales["locality"] = sel_sales.sale_id.map(towns.set_index("sale_id").locality_name)
    baseline_pairs = select_pairs(pair_cache[(("probabilistic", baseline_t))][0],
                                  next(e for e in grid if e.id == "baseline"))
    prof = selection_profile(sel_sales, baseline_pairs, cfg["selection"])
    for r in prof.itertuples(index=False):
        results.append(_result(version, "selection", r.dimension, "scope", None,
                               "share_gap", r.segment, r.gap, r.repeat_legs,
                               {"share_all_sales": r.share_all_sales,
                                "share_repeat_legs": r.share_repeat_legs,
                                "all_sales": r.all_sales}))
    report["selection"] = prof.to_dict("records")

    # --- 5. Frozen settings and gate status -------------------------------------------
    frozen = {
        "accept_threshold": baseline_t,
        "threshold_status": entity_cfg.raw["threshold_status"],
        "anomaly_policy": "flag only; unusual-change and short-hold pairs stay in the headline",
        "headline_method": "wrs", "headline_population": index_cfg["population"],
        "labelled_pairs": labelled,
    }
    blocked = frozen["threshold_status"] != "frozen"
    results.append(_result(version, "freeze", "settings", "scope", None, "headline_claims_blocked",
                           "all", float(blocked), labelled, frozen))
    report["frozen_settings"] = frozen
    report["g10_status"] = ("measured; headline claims blocked until the link-threshold "
                            "gate freezes the link threshold" if blocked else "passed")

    res_df = pd.DataFrame(results)[TABLE_SCHEMAS["gold.validation_results"].names]
    res_df = res_df.drop_duplicates(list(TABLE_SCHEMAS["gold.validation_results"].names[:7]))
    counts = {"results_inserted": tables.insert_new("gold.validation_results", res_df),
              "series_inserted": tables.insert_new(
                  "gold.sensitivity_series",
                  pd.DataFrame(series_rows)[TABLE_SCHEMAS["gold.sensitivity_series"].names])}
    record_stage(tables, "validation", snapshot, version, run.run_id, now)
    return {"snapshot_id": snapshot, "validation_version": version,
            "uncertainty_version": uncertainty_version, "entity_version": entity_version,
            "row_counts": counts, **report}
