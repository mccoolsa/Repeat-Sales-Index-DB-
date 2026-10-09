"""Property-history bootstrap for repeat-sales indices.

A property with three sales contributes two dependent pairs. Resampling pairs independently
would treat them as separate evidence and understate uncertainty, so whole property
histories are resampled with replacement and the full estimator (OLS, variance model,
WLS) is refitted in every replicate.

Intervals describe sampling uncertainty *conditional on* the linkage and filtering
decisions. Systematic false matching is assessed separately through threshold and
disputed-link sensitivity, not by this bootstrap.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ireland_rsi.index import (
    IdentifiabilityError,
    IndexFit,
    build_design,
    cap_weights,
    fit_index,
    fit_variance,
    ols,
    period_of,
    rebase,
    wls_by_transform,
)


@dataclass
class BootstrapResult:
    periods: list[str]
    point: IndexFit
    log_levels: np.ndarray          # valid draws x periods (log of rebased index levels)
    attempted: int
    failures: Counter = field(default_factory=Counter)

    @property
    def valid(self) -> int:
        return int(self.log_levels.shape[0])

    @property
    def success_rate(self) -> float:
        return self.valid / self.attempted if self.attempted else 0.0

    def level_interval(self, level: float = 0.95) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(lower, median, upper) index levels per period from percentile draws."""
        q = [(1 - level) / 2, 0.5, (1 + level) / 2]
        lo, mid, hi = np.quantile(self.log_levels, q, axis=0)
        return np.exp(lo), np.exp(mid), np.exp(hi)

    def log_width(self, level: float = 0.95) -> np.ndarray:
        q = [(1 - level) / 2, (1 + level) / 2]
        lo, hi = np.quantile(self.log_levels, q, axis=0)
        return hi - lo

    def ratio_interval(self, anchor: str, target: str, level: float = 0.95
                       ) -> tuple[float, float, float]:
        """Interval for index[target] / index[anchor] from *paired* draws.

        Using separate endpoint intervals would ignore their covariance; the ratio is
        computed inside each draw instead.
        """
        a, t = self.periods.index(anchor), self.periods.index(target)
        log_ratio = self.log_levels[:, t] - self.log_levels[:, a]
        q = [(1 - level) / 2, 0.5, (1 + level) / 2]
        lo, mid, hi = np.quantile(log_ratio, q)
        return float(np.exp(lo)), float(np.exp(mid)), float(np.exp(hi))

    def growth_interval(self, lag: int, level: float = 0.95) -> pd.DataFrame:
        """Log growth over `lag` periods with percentile intervals."""
        diffs = self.log_levels[:, lag:] - self.log_levels[:, :-lag]
        q = [(1 - level) / 2, 0.5, (1 + level) / 2]
        lo, mid, hi = np.quantile(diffs, q, axis=0)
        return pd.DataFrame({"period": self.periods[lag:], "growth_lo": lo,
                             "growth_median": mid, "growth_hi": hi})


def resample_rows(groups: list[np.ndarray], rng: np.random.Generator) -> np.ndarray:
    """Row positions of one replicate: properties drawn with replacement, each bringing
    all of its pairs."""
    chosen = rng.integers(0, len(groups), size=len(groups))
    return np.concatenate([groups[i] for i in chosen])


def wrs_coefficients(X: np.ndarray, y: np.ndarray, years: np.ndarray, cfg: dict) -> np.ndarray:
    """Three-stage WRS coefficients only (no standard errors): OLS, variance model, WLS."""
    first = ols(X, y)
    variance, _ = fit_variance(y - X @ first, years, cfg["variance_model"])
    weights = cap_weights(1.0 / variance, cfg["variance_model"]["weight_cap_multiple"])
    return wls_by_transform(X, y, 1.0 / weights)


def property_bootstrap(pairs: pd.DataFrame, frequency: str, cfg: dict, draws: int,
                       seed: int, method: str = "wrs") -> BootstrapResult:
    """Resample property histories with replacement and refit each replicate.

    A replicate counts as valid only if it identifies exactly the full-sample periods with
    the same rebasing status; otherwise the failure reason is recorded (e.g. a thin period
    lost all its pairs). Valid-draw counts feed the publication gates.

    For OLS and WRS, a valid replicate's design is a resampling of the full-sample design
    rows, so each replicate reuses those rows and solves in NumPy. This is mathematically the
    same as rebuilding the design from dates (tested against `_property_bootstrap_refit`)
    and about 20x faster, which matters on serverless compute.
    """
    if method not in ("ols", "wrs"):
        return _property_bootstrap_refit(pairs, frequency, cfg, draws, seed, method)
    data = pairs.reset_index(drop=True)
    point = fit_index(data, frequency, cfg, method)
    p1 = np.array([period_of(d, frequency) for d in data.date_1])
    p2 = np.array([period_of(d, frequency) for d in data.date_2])
    design = build_design(p1, p2, data.log_return.to_numpy(), data.holding_years.to_numpy())
    row_of = {int(pos): r for r, pos in enumerate(design.used)}
    groups = [np.array([row_of[int(i)] for i in idx if int(i) in row_of], dtype=int)
              for idx in data.groupby("property_id", sort=True).indices.values()]
    X, y, years, k = design.X, design.y, design.years, design.X.shape[1]
    rng = np.random.default_rng(seed)
    out, failures = [], Counter()
    for _ in range(draws):
        rows = resample_rows(groups, rng)
        Xb = X[rows]
        if len(rows) == 0 or np.any(np.abs(Xb).sum(axis=0) == 0) or (
                k and np.linalg.matrix_rank(Xb) < k):
            failures["period_set_changed"] += 1   # a period lost, or the graph split
            continue
        try:
            coef = (ols(Xb, y[rows]) if method == "ols"
                    else wrs_coefficients(Xb, y[rows], years[rows], cfg))
        except np.linalg.LinAlgError:
            failures["numerical"] += 1
            continue
        levels, status = rebase(point.periods, np.concatenate([[0.0], coef]), cfg["base"])
        if status != point.base_status:
            failures["base_lost"] += 1
            continue
        out.append(np.log(levels))
    levels = np.vstack(out) if out else np.empty((0, len(point.periods)))
    return BootstrapResult(point.periods, point, levels, draws, failures)


def _property_bootstrap_refit(pairs: pd.DataFrame, frequency: str, cfg: dict, draws: int,
                              seed: int, method: str = "wrs") -> BootstrapResult:
    """Reference implementation: rebuild and refit each replicate from its pairs."""
    data = pairs.reset_index(drop=True)
    point = fit_index(data, frequency, cfg, method)
    groups = list(data.groupby("property_id", sort=True).indices.values())
    rng = np.random.default_rng(seed)
    out, failures = [], Counter()
    for _ in range(draws):
        rows = resample_rows(groups, rng)
        try:
            fit = fit_index(data.iloc[rows], frequency, cfg, method)
        except IdentifiabilityError:
            failures["unidentifiable"] += 1
            continue
        except np.linalg.LinAlgError:
            failures["numerical"] += 1
            continue
        if fit.periods != point.periods:
            failures["period_set_changed"] += 1
            continue
        if fit.base_status != point.base_status:
            failures["base_lost"] += 1
            continue
        out.append(np.log(fit.index))
    levels = np.vstack(out) if out else np.empty((0, len(point.periods)))
    return BootstrapResult(point.periods, point, levels, draws, failures)


def property_weight_share(pairs: pd.DataFrame, fit: IndexFit) -> float:
    """Largest share of total regression weight carried by one property."""
    used = pairs.iloc[fit.used]
    w = pd.Series(np.asarray(fit.weights, float), index=used.index)
    by_property = w.groupby(used.property_id.values).sum()
    return float(by_property.max() / by_property.sum())


def downsample_calibration(pairs: pd.DataFrame, frequency: str, cfg: dict,
                           fractions: list[float], reps: int, draws: int, seed: int
                           ) -> pd.DataFrame:
    """Refit on random subsets of properties to see how support relates to precision.

    For each subset: distinct properties, bootstrap success, median 95% log interval width,
    and the RMS log difference from the full-sample fit over the subset's periods (a proxy
    for estimation error, since the full sample is the best available estimate).
    """
    data = pairs.reset_index(drop=True)
    full = fit_index(data, frequency, cfg, "wrs")
    full_log = dict(zip(full.periods, np.log(full.index), strict=True))
    props = np.array(sorted(data.property_id.unique()))
    rng = np.random.default_rng(seed)
    rows = []
    for frac in fractions:
        for rep in range(reps):
            keep = set(rng.choice(props, size=max(2, int(round(frac * len(props)))),
                                  replace=False))
            sub = data[data.property_id.isin(keep)]
            try:
                boot = property_bootstrap(sub, frequency, cfg, draws, seed + rep)
            except IdentifiabilityError:
                rows.append({"fraction": frac, "rep": rep, "properties": len(keep),
                             "identified": False})
                continue
            common = [p for p in boot.periods if p in full_log]
            err = np.array([np.log(boot.point.index[boot.periods.index(p)]) - full_log[p]
                            for p in common])
            rows.append({
                "fraction": frac, "rep": rep, "properties": len(keep), "identified": True,
                "periods": len(boot.periods), "bootstrap_success": boot.success_rate,
                "median_log_width": float(np.median(boot.log_width())) if boot.valid
                else np.nan,
                "rms_log_error_vs_full": float(np.sqrt(np.mean(err ** 2)))})
    return pd.DataFrame(rows)
