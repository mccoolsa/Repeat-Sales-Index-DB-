"""Repeat-sales estimators recover synthetic truth and fail when unidentifiable."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from ireland_rsi.index import (
    IdentifiabilityError,
    build_design,
    cap_weights,
    fit_index,
    fit_variance,
    ols,
    period_of,
    rebase,
    wls_by_transform,
    wls_by_weights,
)

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config" / "index.yml").read_text(encoding="utf-8"))
START = date(2012, 1, 1)
QUARTERS = 24


def quarter_start(q: int) -> date:
    return date(2012 + q // 4, 1 + 3 * (q % 4), 1)


def synthetic_pairs(n_props=4000, seed=7, noise=0.04, drift=0.01, flat=False):
    """True log index is a random walk; noise variance grows with holding time."""
    rng = np.random.default_rng(seed)
    steps = np.zeros(QUARTERS) if flat else rng.normal(0.015, 0.02, QUARTERS)
    steps[0] = 0.0
    truth = np.cumsum(steps)
    rows = []
    for _ in range(n_props):
        n = rng.integers(2, 4)
        qs = np.sort(rng.choice(QUARTERS, size=n, replace=False))
        value = rng.normal(12, 0.4)  # property-specific log price level
        prices = []
        for q in qs:
            prices.append(value + truth[q] + (0 if flat else rng.normal(0, noise)))
        for a, b, pa, pb in zip(qs[:-1], qs[1:], prices[:-1], prices[1:], strict=True):
            d1 = quarter_start(a) + timedelta(days=int(rng.integers(0, 80)))
            d2 = quarter_start(b) + timedelta(days=int(rng.integers(0, 80)))
            years = (d2 - d1).days / 365.25
            extra = 0 if flat else rng.normal(0, drift * np.sqrt(years))
            rows.append({"date_1": d1, "date_2": d2, "log_return": pb - pa + extra,
                         "holding_years": years})
    return pd.DataFrame(rows), truth


@pytest.fixture(scope="module")
def synthetic():
    return synthetic_pairs()


@pytest.mark.parametrize("method", ["ols", "wrs", "huber", "trimmed"])
def test_estimators_recover_synthetic_truth(synthetic, method):
    pairs, truth = synthetic
    fit = fit_index(pairs, "quarterly", CFG, method=method)
    assert fit.periods[0] == "2012Q1" and len(fit.periods) == QUARTERS
    error = np.abs(fit.beta - (truth - truth[0]))
    assert error.max() < 0.02, f"{method}: max log error {error.max():.4f}"


def test_uniform_prices_give_a_flat_index_of_100():
    pairs, _ = synthetic_pairs(n_props=500, flat=True)
    for method in ("ols", "wrs"):
        fit = fit_index(pairs, "quarterly", CFG, method=method)
        assert np.allclose(fit.index, 100.0, atol=1e-8)


def test_rebasing_changes_levels_but_not_ratios(synthetic):
    pairs, _ = synthetic
    fit = fit_index(pairs, "quarterly", CFG, method="wrs")
    on_2015, status = rebase(fit.periods, fit.beta, "2015")
    on_ref, ref_status = rebase(fit.periods, fit.beta, "2099")   # base not available
    assert status == "ok" and ref_status == "fallback_reference_period"
    assert np.isclose(np.mean(on_2015[[i for i, p in enumerate(fit.periods)
                                       if p.startswith("2015")]]), 100.0)
    assert np.allclose(on_2015 / on_2015[0], on_ref / on_ref[0])


def test_disconnected_periods_are_reported_not_spliced():
    early = [("2012Q1", "2012Q3"), ("2012Q3", "2013Q1")] * 5
    late = [("2018Q1", "2018Q3")] * 3
    p1, p2 = zip(*(early + late), strict=True)
    n = len(p1)
    design = build_design(np.array(p1), np.array(p2), np.zeros(n), np.ones(n))
    assert design.periods == ["2012Q1", "2012Q3", "2013Q1"]
    assert design.unidentified_periods == ["2018Q1", "2018Q3"]
    assert design.excluded_disconnected == 3


def test_same_period_only_is_unidentifiable():
    with pytest.raises(IdentifiabilityError):
        build_design(np.array(["2012Q1"] * 3), np.array(["2012Q1"] * 3), np.zeros(3),
                     np.ones(3))


def test_weighted_api_and_row_transformation_agree(synthetic):
    pairs, _ = synthetic
    p1 = np.array([period_of(d, "quarterly") for d in pairs.date_1])
    p2 = np.array([period_of(d, "quarterly") for d in pairs.date_2])
    d = build_design(p1, p2, pairs.log_return.to_numpy(), pairs.holding_years.to_numpy())
    variance, _ = fit_variance(d.y - d.X @ ols(d.X, d.y), d.years, CFG["variance_model"])
    by_weights = wls_by_weights(d.X, d.y, 1.0 / variance).params
    by_transform = wls_by_transform(d.X, d.y, variance)
    assert np.allclose(by_weights, by_transform, atol=1e-10)


def test_variance_and_weights_are_finite_positive_floored_and_capped(synthetic):
    pairs, _ = synthetic
    fit = fit_index(pairs, "quarterly", CFG, method="wrs")
    vm = fit.variance
    assert vm.status == "ok" and all(c >= 0 for c in vm.coefficients)
    assert vm.coefficients[1] > 0          # variance grows with holding time
    assert np.all(np.isfinite(fit.weights)) and np.all(fit.weights > 0)
    cap = CFG["variance_model"]["weight_cap_multiple"]
    assert fit.weights.max() <= cap * np.median(fit.weights) + 1e-12
    assert np.isclose(cap_weights(np.array([1.0, 1.0, 100.0]), 10).max(), 10.0)


def test_same_period_pairs_are_excluded_and_counted():
    pairs, _ = synthetic_pairs(n_props=300)
    same = pairs.iloc[:5].assign(date_2=lambda d: d.date_1 + timedelta(days=5))
    fit = fit_index(pd.concat([pairs, same], ignore_index=True), "quarterly", CFG, "ols")
    assert fit.excluded_same_period >= 5


def test_annual_frequency_and_diagnostics(synthetic):
    pairs, _ = synthetic
    fit = fit_index(pairs, "annual", CFG, method="wrs")
    assert fit.periods == [str(y) for y in range(2012, 2018)]
    assert fit.base_status == "ok"
    d = fit.diagnostics
    assert d["effective_sample_size"] > 0 and 0 < d["max_weight_share"] < 1
    assert fit.endpoint_pairs.sum() == 2 * d["pairs_used"]
