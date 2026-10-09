"""Temporal, grouped backtest and interval calibration."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from ireland_rsi.backtest import (
    calibrate_intervals,
    fold_of,
    horizon_band,
    predict_pairs,
    summarise,
    training_pairs,
)

ROOT = Path(__file__).resolve().parents[1]
INDEX_CFG = yaml.safe_load((ROOT / "config" / "index.yml").read_text(encoding="utf-8"))
CFG = {
    "folds": 3, "frequency": "annual",
    "windows": {"development": [2013, 2014], "calibration": [2015, 2016], "test": [2017, 2018]},
    "min_series_properties": 20, "min_endpoint_pairs": 5, "diagnostic_min_town_properties": 10,
    "intervals": {"level": 0.9, "min_calibration_pairs": 10, "coverage_tolerance": 0.05,
                  "horizon_bands": [{"band": "0-3y", "min": 0, "max": 3},
                                    {"band": "3y+", "min": 3, "max": 100}]},
}
TOWN = {"id": "town:T", "locality_name": "T", "confidence": ["high"]}


def synthetic(n_props=400, seed=0, noise=0.05, growth=0.06):
    """Repeat sales 2010-2018 on a known index exp(growth * (year - 2010))."""
    rng = np.random.default_rng(seed)
    rows = []
    for p in range(n_props):
        years = np.sort(rng.choice(range(2010, 2019), size=int(rng.integers(2, 4)),
                                   replace=False))
        base = rng.normal(12, 0.3)
        town = "T" if p % 2 else "U"
        dates = [date(int(y), 2, 1) + timedelta(days=int(rng.integers(0, 300))) for y in years]
        prices = [float(np.exp(base + growth * (y - 2010) + rng.normal(0, noise)))
                  for y in years]
        for i in range(len(years) - 1):
            rows.append({
                "pair_id": f"p{p}_{i}", "property_id": f"prop{p}",
                "date_1": dates[i], "date_2": dates[i + 1],
                "log_return": np.log(prices[i + 1] / prices[i]),
                "holding_years": (dates[i + 1] - dates[i]).days / 365.25,
                "price_1_eur": prices[i], "price_2_eur": prices[i + 1],
                "town_1": town, "town_2": town, "conf_1": "high", "conf_2": "high"})
    return pd.DataFrame(rows)


def test_fold_assignment_is_deterministic_and_balanced():
    folds = [fold_of(f"prop{i}", 5) for i in range(2000)]
    assert folds == [fold_of(f"prop{i}", 5) for i in range(2000)]
    assert min(np.bincount(folds)) > 300


def test_held_out_properties_never_enter_their_fit():
    data = synthetic(200)
    data["fold"] = [fold_of(p, 3) for p in data.property_id]
    for year in (2014, 2017):
        for fold in range(3):
            train = training_pairs(data, year, fold)
            held = set(data[data.fold == fold].property_id)
            assert not held & set(train.property_id)
            assert pd.to_datetime(train.date_2).dt.year.max() <= year


def test_future_prices_cannot_change_an_earlier_prediction():
    data = synthetic(400, seed=1)
    before = predict_pairs(data, CFG, INDEX_CFG, TOWN, None)
    later = pd.to_datetime(data.date_2).dt.year > 2016
    bumped = data.copy()
    bumped.loc[later, "price_2_eur"] *= 3.0
    bumped.loc[later, "log_return"] += np.log(3.0)
    after = predict_pairs(bumped, CFG, INDEX_CFG, TOWN, None)
    keep = before.target_year <= 2016
    a = before[keep].sort_values(["pair_id", "method"]).reset_index(drop=True)
    b = after[after.target_year <= 2016].sort_values(["pair_id", "method"]).reset_index(drop=True)
    pd.testing.assert_series_equal(a.predicted_eur, b.predicted_eur)
    assert not np.allclose(before[~keep & (before.method == "nominal")].actual_eur.values,
                           after[(after.target_year > 2016) & (after.method == "nominal")]
                           .actual_eur.values)


def test_ratio_arithmetic_recovers_a_noise_free_index():
    data = synthetic(400, seed=2, noise=0.0, growth=0.06)
    pred = predict_pairs(data, CFG, INDEX_CFG, TOWN, None)
    county = pred[(pred.method == "county") & (pred.status == "ok")]
    assert len(county) > 100
    # Prices depend on the year only, so anchor * index ratio must reproduce the later price.
    assert np.allclose(county.predicted_eur / county.actual_eur, 1.0, rtol=1e-6)
    assert np.allclose(county.log_error, 0.0, atol=1e-6)
    nominal = pred[pred.method == "nominal"]
    assert np.allclose(nominal.predicted_eur, data.set_index("pair_id").loc[
        nominal.pair_id].price_1_eur.values)


def test_unsupported_cases_return_no_amount_rather_than_zero():
    data = synthetic(30, seed=3)        # far below the 20-property floor per fold and year
    cfg = {**CFG, "min_series_properties": 1000}
    pred = predict_pairs(data, cfg, INDEX_CFG, TOWN, None)
    county = pred[pred.method == "county"]
    assert (county.status == "unsupported").all()
    assert county.predicted_eur.isna().all()
    national = pred[pred.method == "national_cso"]
    assert (national.status == "unsupported").all()      # no national series supplied
    local = pred[pred.method == "local"]
    assert local.fallback_level.isna().all() and local.predicted_eur.isna().all()


def test_local_falls_back_to_the_county_when_the_town_is_thin():
    data = synthetic(400, seed=4)
    cfg = {**CFG, "min_series_properties": 150}   # town alone (~200 props, minus a fold) fails
    pred = predict_pairs(data, cfg, INDEX_CFG, TOWN, None)
    local = pred[(pred.method == "local") & (pred.status == "ok")]
    town_rows = local[local.in_town]
    assert (town_rows.geography_id == "scope").any()
    assert (town_rows[town_rows.geography_id == "scope"].fallback_level == 1).all()
    assert (local[~local.in_town].fallback_level == 0).all()


def test_interval_calibration_ignores_test_window_errors():
    rng = np.random.default_rng(5)
    n = 200
    pred = pd.DataFrame({
        "method": "local", "status": "ok",
        "window": ["calibration"] * n + ["test"] * n,
        "holding_years": rng.uniform(0, 8, 2 * n),
        "log_error": np.r_[rng.normal(0, 0.1, n), rng.normal(0, 0.1, n)]})
    a = calibrate_intervals(pred, CFG)
    shifted = pred.copy()
    shifted.loc[shifted.window == "test", "log_error"] += 5.0
    b = calibrate_intervals(shifted, CFG)
    assert np.allclose(a.q_lo, b.q_lo) and np.allclose(a.q_hi, b.q_hi)
    assert (a.status == "ok").all()
    assert (b.status == "coverage_failed").all() and (b.coverage_test == 0).all()


def test_too_few_calibration_cases_withholds_the_band():
    pred = pd.DataFrame({"method": "local", "status": "ok", "window": "calibration",
                         "holding_years": [1.0] * 5, "log_error": [0.0] * 5})
    out = calibrate_intervals(pred, CFG).set_index("horizon_band")
    assert out.loc["0-3y", "status"] == "insufficient_calibration"
    assert pd.isna(out.loc["0-3y", "q_lo"])


def test_horizon_band_edges():
    bands = CFG["intervals"]["horizon_bands"]
    assert horizon_band(0.0, bands) == "0-3y"
    assert horizon_band(3.0, bands) == "3y+"
    assert horizon_band(250.0, bands) == "3y+"


def test_summary_compares_methods_on_common_pairs():
    data = synthetic(400, seed=6)
    pred = predict_pairs(data, CFG, INDEX_CFG, TOWN, None)
    s = summarise(pred, CFG)
    rate = s[(s.metric == "support_rate") & (s.method == "nominal")]
    assert (rate.value == 1.0).all()
    common = s[(s.segment == "common") & (s.metric == "median_abs_pct_error")]
    # No national series in this test, so no pair is common to all four main methods.
    assert common.empty
    with pytest.raises(KeyError):
        s.set_index("segment").loc["nonexistent"]
