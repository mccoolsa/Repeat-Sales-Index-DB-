"""Property-history bootstrap and joint-draw intervals."""

from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ireland_rsi.uncertainty import BootstrapResult, property_bootstrap, resample_rows

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config" / "index.yml").read_text(encoding="utf-8"))


def annual_pairs(n_props, years=6, seed=0, noise=0.05, start=2012, truth=None,
                 thin_year=None):
    """Synthetic annual repeat sales with property IDs; optionally one very thin year."""
    rng = np.random.default_rng(seed)
    truth = truth if truth is not None else np.cumsum(
        np.r_[0, rng.normal(0.05, 0.04, years - 1)])
    allowed = [y for y in range(years) if y != thin_year]
    rows = []
    for p in range(n_props):
        n = int(rng.integers(2, 4))
        ys = np.sort(rng.choice(allowed, size=min(n, len(allowed)), replace=False))
        level = rng.normal(12, 0.3)
        prices = [level + truth[y] + rng.normal(0, noise) for y in ys]
        for a, b, pa, pb in zip(ys[:-1], ys[1:], prices[:-1], prices[1:], strict=True):
            d1 = date(start + a, 3, 1) + timedelta(days=int(rng.integers(0, 200)))
            d2 = date(start + b, 3, 1) + timedelta(days=int(rng.integers(0, 200)))
            rows.append({"property_id": f"p{p}", "date_1": d1, "date_2": d2,
                         "log_return": pb - pa, "holding_years": (d2 - d1).days / 365.25})
    if thin_year is not None:   # exactly one property touches the thin year
        rows.append({"property_id": "thin", "date_1": date(start, 4, 1),
                     "date_2": date(start + thin_year, 4, 1),
                     "log_return": truth[thin_year] - truth[0],
                     "holding_years": float(thin_year)})
    return pd.DataFrame(rows), truth


def test_resampling_keeps_each_property_history_together():
    groups = [np.array([0, 1, 2]), np.array([3]), np.array([4, 5])]
    rng = np.random.default_rng(1)
    for _ in range(50):
        rows = list(resample_rows(groups, rng))
        i = 0
        while i < len(rows):   # rows arrive as whole groups, in group order
            g = next(g for g in groups if g[0] == rows[i])
            assert rows[i:i + len(g)] == list(g)
            i += len(g)


def test_ratio_interval_uses_paired_draws():
    common = np.random.default_rng(2).normal(0, 0.2, size=(500, 1))
    levels = np.hstack([common, common + 0.3])   # periods move together exactly
    boot = BootstrapResult(["2015", "2020"], None, np.log(100) + levels, 500, Counter())
    lo, mid, hi = boot.ratio_interval("2015", "2020")
    assert np.isclose(lo, np.exp(0.3)) and np.isclose(hi, np.exp(0.3))
    width = boot.log_width()
    assert width[0] > 0.5 and width[1] > 0.5     # endpoints alone look very uncertain


def test_bootstrap_intervals_cover_synthetic_truth():
    covered, total = 0, 0
    for seed in range(12):
        truth = np.cumsum(np.r_[0, np.random.default_rng(100 + seed).normal(0.05, 0.04, 5)])
        pairs, _ = annual_pairs(250, seed=seed, truth=truth)
        boot = property_bootstrap(pairs, "annual", CFG, draws=80, seed=seed)
        assert boot.success_rate == 1.0
        g = boot.growth_interval(lag=1)
        true_growth = np.diff(truth)
        covered += int(((g.growth_lo <= true_growth) & (true_growth <= g.growth_hi)).sum())
        total += len(true_growth)
    assert covered / total >= 0.85, f"coverage {covered / total:.2f}"


def test_thin_period_produces_recorded_bootstrap_failures():
    pairs, _ = annual_pairs(150, seed=4, thin_year=4)
    boot = property_bootstrap(pairs, "annual", CFG, draws=60, seed=4)
    assert "2016" in boot.periods
    assert boot.success_rate < 0.95
    assert boot.failures["period_set_changed"] > 0
    assert boot.valid + sum(boot.failures.values()) == boot.attempted


def test_fast_bootstrap_matches_the_refit_reference():
    from ireland_rsi.uncertainty import _property_bootstrap_refit

    pairs, _ = annual_pairs(200, seed=9, thin_year=3)
    fast = property_bootstrap(pairs, "annual", CFG, draws=40, seed=5)
    slow = _property_bootstrap_refit(pairs, "annual", CFG, draws=40, seed=5)
    assert fast.valid == slow.valid and fast.failures == slow.failures
    assert np.allclose(fast.log_levels, slow.log_levels, atol=1e-9)
