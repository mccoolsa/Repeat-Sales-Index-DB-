"""Repeat-sales index estimation.

For pair i sold in periods s < t:

    y_i = log(price_t / price_s),   X_i,s = -1,  X_i,t = +1,  all other entries 0
    y_i = X_i beta + error_i,       one reference coefficient fixed at 0, no intercept
    index_t = 100 * exp(beta_t) / mean(exp(beta_b) for b in base periods)

Estimators:
* ``ols``     unweighted repeat sales.
* ``wrs``     Case-Shiller three-stage weighted repeat sales: OLS, then a model of squared
              residuals against holding years (variance = a + b * years, a, b >= 0), then WLS
              with weights 1 / variance (floored and capped).
* ``huber``   robust M-estimate (sensitivity).
* ``trimmed`` WRS after symmetric trimming of log returns (sensitivity).

Every published period must connect to the reference period through pairs, and the reduced
design must have full rank; otherwise the fit fails instead of producing numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

METHODS = ("ols", "wrs", "huber", "trimmed")


class IdentifiabilityError(Exception):
    """The periods requested cannot all be identified from the available pairs."""


def period_of(d: date, frequency: str) -> str:
    if frequency == "quarterly":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    if frequency == "annual":
        return str(d.year)
    raise ValueError(f"unknown frequency {frequency}")


# ---------------------------------------------------------------------------
# Design matrix and identifiability
# ---------------------------------------------------------------------------


@dataclass
class Design:
    periods: list[str]          # estimable periods in time order; periods[0] is the reference
    X: np.ndarray               # pairs x (periods - 1); reference column removed
    y: np.ndarray
    years: np.ndarray
    used: np.ndarray            # positions of the input pairs used
    excluded_same_period: int
    excluded_disconnected: int
    unidentified_periods: list[str]


def _components(periods: list[str], p1: np.ndarray, p2: np.ndarray) -> dict[str, str]:
    parent = {p: p for p in periods}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in zip(p1, p2, strict=True):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {p: find(p) for p in periods}


def build_design(period_1: np.ndarray, period_2: np.ndarray, log_return: np.ndarray,
                 holding_years: np.ndarray) -> Design:
    """Design for the connected set of periods holding the most pairs.

    The reference is the earliest period in that set. Pairs in other components cannot be
    placed on the same scale and are excluded (and their periods reported unidentified).
    """
    period_1, period_2 = np.asarray(period_1), np.asarray(period_2)
    same = period_1 == period_2
    keep = np.flatnonzero(~same)
    p1, p2 = period_1[keep], period_2[keep]
    if len(keep) == 0:
        raise IdentifiabilityError("no pairs span two different periods")
    periods = sorted(set(p1) | set(p2))
    comp = _components(periods, p1, p2)
    pair_comp = np.array([comp[a] for a in p1])
    counts = pd.Series(pair_comp).value_counts()
    main = counts.index[0]
    in_main = pair_comp == main
    estimable = sorted(p for p in periods if comp[p] == main)
    unidentified = sorted(p for p in periods if comp[p] != main)
    col = {p: i - 1 for i, p in enumerate(estimable)}  # reference -> -1 (dropped)
    used = keep[in_main]
    X = np.zeros((len(used), len(estimable) - 1))
    for r, (a, b) in enumerate(zip(period_1[used], period_2[used], strict=True)):
        if col[a] >= 0:
            X[r, col[a]] -= 1.0
        if col[b] >= 0:
            X[r, col[b]] += 1.0
    if X.shape[1] and np.linalg.matrix_rank(X) < X.shape[1]:
        raise IdentifiabilityError("reduced design matrix is rank deficient")
    return Design(estimable, X, np.asarray(log_return, float)[used],
                  np.asarray(holding_years, float)[used], used, int(same.sum()),
                  int((~in_main).sum()), unidentified)


# ---------------------------------------------------------------------------
# Estimators
# ---------------------------------------------------------------------------


def ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.linalg.lstsq(X, y, rcond=None)[0]


def wls_by_weights(X: np.ndarray, y: np.ndarray, w: np.ndarray):
    """WLS through an API that takes objective weights w_i = 1 / variance_i."""
    import statsmodels.api as sm

    return sm.WLS(y, X, weights=w).fit()


def wls_by_transform(X: np.ndarray, y: np.ndarray, variance: np.ndarray) -> np.ndarray:
    """The same WLS by scaling each row by 1 / sqrt(variance_i) and solving OLS."""
    s = 1.0 / np.sqrt(variance)
    return ols(X * s[:, None], y * s)


@dataclass
class VarianceModel:
    form: str
    coefficients: list[float]
    floor: float
    status: str


def fit_variance(residuals: np.ndarray, years: np.ndarray, cfg: dict
                 ) -> tuple[np.ndarray, VarianceModel]:
    """Non-negative least squares of squared residuals on holding years."""
    from scipy.optimize import nnls

    cols = [np.ones_like(years), years]
    if cfg.get("form") == "quadratic":
        cols.append(years ** 2)
    A = np.column_stack(cols)
    coef, _ = nnls(A, residuals ** 2)
    fitted = A @ coef
    status = "ok"
    if not np.all(np.isfinite(coef)) or not np.any(coef > 0):
        status = "degenerate"
    positive = fitted[fitted > 0]
    floor = max(cfg["min_floor"], float(np.quantile(positive, cfg["floor_quantile"]))
                if positive.size else cfg["min_floor"])
    variance = np.maximum(fitted, floor)
    return variance, VarianceModel(cfg.get("form", "linear"), [float(c) for c in coef],
                                   floor, status)


def cap_weights(w: np.ndarray, multiple: float) -> np.ndarray:
    return np.minimum(w, multiple * float(np.median(w)))


def effective_sample_size(w: np.ndarray) -> float:
    return float(w.sum() ** 2 / (w ** 2).sum())


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


@dataclass
class IndexFit:
    method: str
    frequency: str
    periods: list[str]
    beta: np.ndarray            # one per period; reference = 0
    se: np.ndarray              # conventional standard errors (bootstrap in the uncertainty stage)
    index: np.ndarray           # rebased levels
    base: str
    base_status: str
    used: np.ndarray            # positions of pairs used
    residuals: np.ndarray
    weights: np.ndarray
    variance: VarianceModel | None
    excluded_same_period: int
    excluded_disconnected: int
    excluded_trimmed: int
    unidentified_periods: list[str]
    endpoint_pairs: np.ndarray  # pairs with each period as an endpoint
    diagnostics: dict = field(default_factory=dict)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame({"period": self.periods, "beta": self.beta, "se": self.se,
                             "index_value": self.index, "endpoint_pairs": self.endpoint_pairs,
                             "is_reference": [i == 0 for i in range(len(self.periods))]})


def rebase(periods: list[str], beta: np.ndarray, base: str) -> tuple[np.ndarray, str]:
    """Levels with the mean of the base periods = 100; falls back to the reference."""
    base_idx = [i for i, p in enumerate(periods) if p == base or p.startswith(f"{base}Q")]
    expected = 4 if periods and "Q" in periods[0] else 1
    levels = np.exp(beta)
    if len(base_idx) == expected:
        return 100.0 * levels / levels[base_idx].mean(), "ok"
    return 100.0 * levels / levels[0], "fallback_reference_period"


def fit_index(pairs: pd.DataFrame, frequency: str, cfg: dict, method: str = "wrs") -> IndexFit:
    """Fit one geography and frequency. `pairs` needs date_1, date_2, log_return,
    holding_years (one row per pair)."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method}")
    trimmed = 0
    data = pairs.reset_index(drop=True)
    if method == "trimmed":
        lo, hi = data.log_return.quantile(cfg["robust"]["trim_quantiles"])
        keep = data.log_return.between(lo, hi)
        trimmed = int((~keep).sum())
        data = data[keep].reset_index(drop=True)
    p1 = np.array([period_of(d, frequency) for d in data.date_1])
    p2 = np.array([period_of(d, frequency) for d in data.date_2])
    design = build_design(p1, p2, data.log_return.to_numpy(), data.holding_years.to_numpy())
    X, y = design.X, design.y

    variance_model = None
    weights = np.ones(len(y))
    if method == "ols":
        coef = ols(X, y)
        se = _ols_se(X, y, coef)
    elif method == "huber":
        import statsmodels.api as sm

        res = sm.RLM(y, X, M=sm.robust.norms.HuberT(t=cfg["robust"]["huber_t"])).fit()
        coef, se, weights = res.params, res.bse, res.weights
    else:  # wrs and trimmed
        first = ols(X, y)
        variance, variance_model = fit_variance(y - X @ first, design.years,
                                                cfg["variance_model"])
        weights = cap_weights(1.0 / variance, cfg["variance_model"]["weight_cap_multiple"])
        res = wls_by_weights(X, y, weights)
        coef, se = res.params, res.bse

    beta = np.concatenate([[0.0], np.asarray(coef, float)])
    se_full = np.concatenate([[0.0], np.asarray(se, float)])
    index, base_status = rebase(design.periods, beta, cfg["base"])
    residuals = y - X @ np.asarray(coef, float)
    pos = {p: i for i, p in enumerate(design.periods)}
    endpoints = np.zeros(len(design.periods), int)
    for a, b in zip(p1[design.used], p2[design.used], strict=True):
        endpoints[pos[a]] += 1
        endpoints[pos[b]] += 1
    fit = IndexFit(method, frequency, design.periods, beta, se_full, index, cfg["base"],
                   base_status, design.used, residuals, weights, variance_model,
                   design.excluded_same_period, design.excluded_disconnected, trimmed,
                   design.unidentified_periods, endpoints)
    fit.diagnostics = diagnostics(fit, design)
    return fit


def _ols_se(X: np.ndarray, y: np.ndarray, coef: np.ndarray) -> np.ndarray:
    resid = y - X @ coef
    dof = max(len(y) - X.shape[1], 1)
    sigma2 = float(resid @ resid) / dof
    return np.sqrt(np.diag(sigma2 * np.linalg.pinv(X.T @ X)))


def diagnostics(fit: IndexFit, design: Design) -> dict:
    w = np.asarray(fit.weights, float)
    std = fit.residuals * np.sqrt(w) if fit.method in ("wrs", "trimmed") else fit.residuals
    mad = float(np.median(np.abs(std - np.median(std)))) * 1.4826 or 1.0
    z = (std - np.median(std)) / mad
    return {
        "pairs_used": int(len(fit.used)),
        "periods": len(fit.periods),
        "reference_period": fit.periods[0],
        "rank_ok": True,
        "effective_sample_size": round(effective_sample_size(w), 1),
        "max_weight_share": round(float(w.max() / w.sum()), 5),
        "periods_with_fewer_than_20_endpoint_pairs": int((fit.endpoint_pairs < 20).sum()),
        "residual_sd": round(float(np.std(fit.residuals)), 4),
        "robust_z_over_3": int((np.abs(z) > 3).sum()),
        "robust_z_over_4": int((np.abs(z) > 4).sum()),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _pairs_with_geography(tables, entity_version: str, rule_set: str, address_version: str,
                          decisions: list[str]) -> pd.DataFrame:
    """Pairs of a population with each leg's town and town confidence."""
    p = tables.qualify("silver.repeat_pairs")
    s = tables.qualify("silver.sales")
    a = tables.qualify("silver.address_features")
    in_list = ", ".join(f"'{d}'" for d in decisions)
    df = tables.query(
        "SELECT p.pair_id, p.property_id, p.date_1, p.date_2, p.log_return, p.holding_years, "
        "a1.locality_name AS town_1, a1.locality_confidence AS conf_1, "
        "a2.locality_name AS town_2, a2.locality_confidence AS conf_2 "
        f"FROM {p} p "
        f"JOIN {s} s1 ON s1.sale_id = p.sale_id_1 AND s1.rule_set_version = '{rule_set}' "
        f"JOIN {a} a1 ON a1.raw_row_id = s1.raw_row_id "
        f"AND a1.address_version = '{address_version}' "
        f"JOIN {s} s2 ON s2.sale_id = p.sale_id_2 AND s2.rule_set_version = '{rule_set}' "
        f"JOIN {a} a2 ON a2.raw_row_id = s2.raw_row_id "
        f"AND a2.address_version = '{address_version}' "
        f"WHERE p.entity_version = '{entity_version}' AND p.decision IN ({in_list})")
    df["date_1"] = pd.to_datetime(df.date_1).dt.date
    df["date_2"] = pd.to_datetime(df.date_2).dt.date
    return df.drop_duplicates("pair_id").sort_values("pair_id").reset_index(drop=True)


def _select_geography(pairs: pd.DataFrame, geo: dict) -> pd.DataFrame:
    if geo["id"] == "scope":
        return pairs
    ok = set(geo["confidence"])
    mask = ((pairs.town_1 == geo["locality_name"]) & (pairs.town_2 == geo["locality_name"])
            & pairs.conf_1.isin(ok) & pairs.conf_2.isin(ok))
    return pairs[mask].reset_index(drop=True)


def run_index(tables, scope: str, cfg: dict, cfg_bytes: bytes, run) -> dict:
    """Fit every configured geography, frequency, method and population."""
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from ireland_rsi.contracts import TABLE_SCHEMAS
    from ireland_rsi.ingest import active_snapshot
    from ireland_rsi.storage import latest_stage_version, record_stage, sha256_hex

    snapshot = active_snapshot(tables, scope)
    entity_version = latest_stage_version(tables, "entities", snapshot)
    rule_set = latest_stage_version(tables, "clean", snapshot)
    address_version = latest_stage_version(tables, "addresses", snapshot)
    if not entity_version:
        raise RuntimeError("property histories must exist for the snapshot")
    fit_version = "fv-" + sha256_hex(cfg_bytes + Path(__file__).read_bytes()
                                     + entity_version.encode())[:12]
    populations = {cfg["population"]: ["included_headline"],
                   **cfg.get("sensitivity_populations", {})}
    now = datetime.now(UTC)
    fit_rows, point_frames, resid_frames, summary = [], [], [], []
    for population, decisions in populations.items():
        all_pairs = _pairs_with_geography(tables, entity_version, rule_set, address_version,
                                          decisions)
        methods = METHODS if population == cfg["population"] else ("wrs",)
        for geo in cfg["geographies"]:
            pairs = _select_geography(all_pairs, geo)
            for frequency in cfg["frequencies"]:
                for method in methods:
                    fit_id = "fit-" + sha256_hex(
                        f"{fit_version}|{geo['id']}|{frequency}|{method}|{population}".encode()
                    )[:16]
                    row = dict.fromkeys(TABLE_SCHEMAS["silver.index_fits"].names)
                    row.update(fit_id=fit_id, fit_version=fit_version,
                               entity_version=entity_version, geography_id=geo["id"],
                               frequency=frequency, method=method, population=population,
                               base=cfg["base"], pairs_input=len(pairs), created_at=now,
                               run_id=run.run_id)
                    try:
                        fit = fit_index(pairs, frequency, cfg, method)
                    except (IdentifiabilityError, ValueError, np.linalg.LinAlgError) as exc:
                        row.update(status="failed", failure_reason=str(exc))
                        fit_rows.append(row)
                        summary.append({k: row[k] for k in (
                            "geography_id", "frequency", "method", "population", "status",
                            "failure_reason")})
                        continue
                    vm = fit.variance
                    status = "ok" if (vm is None or vm.status == "ok") else "variance_unstable"
                    row.update(
                        status=status, reference_period=fit.periods[0],
                        base_status=fit.base_status, pairs_used=len(fit.used),
                        properties_used=int(pairs.iloc[fit.used].property_id.nunique()),
                        excluded_same_period=fit.excluded_same_period,
                        excluded_disconnected=fit.excluded_disconnected,
                        excluded_trimmed=fit.excluded_trimmed,
                        unidentified_periods=",".join(fit.unidentified_periods),
                        variance_coefficients=json.dumps(vm.coefficients) if vm else None,
                        variance_floor=vm.floor if vm else None,
                        variance_status=vm.status if vm else None,
                        diagnostics_json=json.dumps(fit.diagnostics))
                    fit_rows.append(row)
                    points = fit.frame().assign(fit_id=fit_id)
                    point_frames.append(points[TABLE_SCHEMAS["silver.index_points"].names])
                    if method == "wrs" and population == cfg["population"]:
                        # Residuals are for the pairs used, which may be fewer than the input.
                        resid_frames.append(pd.DataFrame({
                            "fit_id": fit_id, "pair_id": pairs.iloc[fit.used].pair_id.values,
                            "residual": fit.residuals, "weight": fit.weights}))
                    last = fit.frame().iloc[-1]
                    summary.append({
                        "geography_id": geo["id"], "frequency": frequency, "method": method,
                        "population": population, "status": status,
                        "pairs_used": len(fit.used), "periods": len(fit.periods),
                        "first": fit.periods[0], "last": fit.periods[-1],
                        "last_index": round(float(last.index_value), 1),
                        "ess": fit.diagnostics["effective_sample_size"],
                        "thin_periods": fit.diagnostics[
                            "periods_with_fewer_than_20_endpoint_pairs"]})
    fits = pd.DataFrame(fit_rows)[TABLE_SCHEMAS["silver.index_fits"].names]
    counts = {"fits_inserted": tables.insert_new("silver.index_fits", fits)}
    if point_frames:
        counts["points_inserted"] = tables.insert_new("silver.index_points",
                                                      pd.concat(point_frames))
    if resid_frames:
        counts["residuals_inserted"] = tables.insert_new("silver.index_pair_residuals",
                                                         pd.concat(resid_frames))
    record_stage(tables, "index", snapshot, fit_version, run.run_id, now)
    return {"snapshot_id": snapshot, "entity_version": entity_version,
            "fit_version": fit_version, "row_counts": counts, "fits": summary}
