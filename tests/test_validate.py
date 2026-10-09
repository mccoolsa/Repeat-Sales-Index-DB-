"""CSO alignment, growth comparison, threshold evidence and experiment settings."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ireland_rsi.validate import (
    BenchmarkError,
    Experiment,
    compare_growth,
    cso_series,
    experiment_grid,
    log_growth,
    monthly_to,
    quarterly_to_annual,
    select_pairs,
    selection_profile,
    shift_period,
    threshold_precision,
    wilson,
)

SPEC = {"frequency": "monthly", "index_statistic": "C01", "time_dimension": "TLIST(M1)",
        "series_dimension": "TYPE"}


def jsonstat(months, values_by_series, unit="Base 2015=100",
             label="Residential Property Price Index"):
    codes = list(values_by_series)
    values = []
    for _stat in ["C01"]:
        for m in months:
            for c in codes:
                values.append(values_by_series[c][months.index(m)])
    return {
        "id": ["STATISTIC", "TLIST(M1)", "TYPE"], "size": [1, len(months), len(codes)],
        "dimension": {
            "STATISTIC": {"category": {"index": ["C01"], "label": {"C01": label},
                                       "unit": {"C01": {"label": unit}}}},
            "TLIST(M1)": {"category": {"index": months}},
            "TYPE": {"category": {"index": codes}},
        },
        "value": values,
    }


def months(start_year, n):
    return [f"{start_year + i // 12}{i % 12 + 1:02d}" for i in range(n)]


def test_cso_series_reads_the_right_cells():
    ms = months(2015, 3)
    data = jsonstat(ms, {"-": [100.0, 101.0, 102.0], "21": [90.0, 91.0, None]})
    s = cso_series(data, SPEC, "21")
    assert list(s.index) == ["2015-01", "2015-02"]
    assert list(s.values) == [90.0, 91.0]


def test_non_index_benchmark_is_rejected():
    ms = months(2015, 3)
    with pytest.raises(BenchmarkError):
        cso_series(jsonstat(ms, {"-": [1, 2, 3]}, unit="Euro"), SPEC, "-")
    with pytest.raises(BenchmarkError):
        cso_series(jsonstat(ms, {"-": [1, 2, 3]}, label="Median price"), SPEC, "-")


def test_monthly_to_quarterly_is_mean_of_complete_quarters_only():
    s = pd.Series({"2015-01": 100.0, "2015-02": 103.0, "2015-03": 106.0,
                   "2015-04": 110.0, "2015-05": 112.0})
    q = monthly_to(s, "quarterly")
    assert list(q.index) == ["2015Q1"]          # Q2 has only two months
    assert q["2015Q1"] == pytest.approx(103.0)


def test_excluded_provisional_months_make_their_period_incomplete():
    s = pd.Series({f"2015-{m:02d}": 100.0 + m for m in range(1, 13)})
    assert list(monthly_to(s, "annual").index) == ["2015"]
    assert monthly_to(s, "annual", {"2015-12"}).empty
    q = monthly_to(s, "quarterly", {"2015-12"})
    assert list(q.index) == ["2015Q1", "2015Q2", "2015Q3"]


def test_quarterly_to_annual_needs_four_quarters():
    s = pd.Series({"2015Q1": 1.0, "2015Q2": 2.0, "2015Q3": 3.0, "2015Q4": 4.0, "2016Q1": 5.0})
    a = quarterly_to_annual(s)
    assert list(a.index) == ["2015"] and a["2015"] == 2.5


def test_period_shift_crosses_years():
    assert shift_period("2015Q1", -1) == "2014Q4"
    assert shift_period("2015Q4", 4) == "2016Q4"
    assert shift_period("2015", -1) == "2014"


def test_growth_metrics_are_invariant_to_rebasing():
    rng = np.random.default_rng(0)
    years = [str(y) for y in range(2010, 2026)]
    ours = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.04, 0.05, len(years)))), index=years)
    bench = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.04, 0.05, len(years)))), index=years)
    a = compare_growth(ours, bench, 1)
    b = compare_growth(ours * 3.7, bench / 1.9, 1)
    for k in ("bias", "mean_abs_diff", "rmse", "correlation", "direction_agreement"):
        assert a[k] == pytest.approx(b[k])
    same = compare_growth(ours, ours * 2, 1)
    assert same["bias"] == pytest.approx(0) and same["correlation"] == pytest.approx(1)
    assert log_growth(ours, 1).index[0] == "2011"


def test_wilson_interval_brackets_the_estimate():
    lo, hi = wilson(9, 10)
    assert lo < 0.9 < hi and hi <= 1
    assert wilson(0, 0) == (None, None)


def test_threshold_precision_uses_sampling_weights_and_ignores_uncertain():
    sample = pd.DataFrame({
        "pair_id": ["p1", "p2", "p3", "p4"],
        "sale_id_a": ["a1", "a2", "a3", "a4"], "sale_id_b": ["b1", "b2", "b3", "b4"],
        "sampling_prob": [0.5, 0.1, 0.5, 0.5]})
    labels = pd.DataFrame({"pair_id": ["p1", "p2", "p3", "p4"],
                           "label": ["match", "non_match", "uncertain", "match"]})
    cands = pd.DataFrame({"sale_id_l": ["b1", "a2", "a3", "a4"],   # order must not matter
                          "sale_id_r": ["a1", "b2", "b3", "b4"],
                          "match_probability": [0.95, 0.8, 0.99, 0.2]})
    tp = threshold_precision(sample, labels, cands, [0.5, 0.9]).set_index("threshold")
    # At 0.5: p1 (match, w=2) and p2 (non-match, w=10) accepted; p3 uncertain is ignored.
    assert tp.loc[0.5, "accepted_labelled"] == 2
    assert tp.loc[0.5, "precision"] == 0.5
    assert tp.loc[0.5, "weighted_precision"] == pytest.approx(2 / 12)
    assert tp.loc[0.9, "precision"] == 1.0
    # Recall within the sample: matches p1 (w=2) and p4 (w=2); only p1 is accepted.
    assert tp.loc[0.9, "weighted_recall_within_sample"] == pytest.approx(0.5)


def pairs_frame():
    return pd.DataFrame({
        "decision": ["included_headline", "included_new_first_leg", "included_headline",
                     "included_headline"],
        "unusual_change": [False, False, True, False],
        "short_hold": [False, True, False, False],
        "eircode_1": ["A65X", None, "F92Y", "F92Y"], "eircode_2": ["A65X", None, "F92Y", "F92Z"],
        "date_1": [date(2014, 1, 1), date(2016, 1, 1), date(2015, 6, 1), date(2018, 1, 1)],
    })


def test_each_experiment_selects_its_own_population():
    p = pairs_frame()
    base = Experiment("baseline", "", 0.5)
    assert len(select_pairs(p, base)) == 3
    assert len(select_pairs(p, Experiment("n", "", 0.5, decisions=(
        "included_headline", "included_new_first_leg")))) == 4
    assert len(select_pairs(p, Experiment("u", "", 0.5, exclude_flags=("unusual_change",)))) == 2
    assert len(select_pairs(p, Experiment("e", "", 0.5, subset="eircode_both"))) == 2
    assert len(select_pairs(p, Experiment("f", "", 0.5, subset="from_2015"))) == 2
    with pytest.raises(ValueError):
        select_pairs(p, Experiment("x", "", 0.5, subset="nonsense"))


def test_grid_records_settings_and_sweeps_thresholds():
    cfg = {"frequency": "annual", "thresholds": [0.3, 0.5, 0.9],
           "experiments": [{"id": "baseline", "description": "b"},
                           {"id": "det", "description": "d", "links": "deterministic"}]}
    grid = {e.id: e for e in experiment_grid(cfg, 0.5)}
    assert set(grid) == {"baseline", "det", "threshold_0.3", "threshold_0.9"}
    assert grid["threshold_0.3"].threshold == 0.3
    assert grid["det"].link_key == ("deterministic", -1.0)
    assert grid["baseline"].link_key != grid["threshold_0.9"].link_key
    assert grid["det"].settings()["links"] == "deterministic"


def test_selection_profile_shares_sum_to_one_and_gaps_to_zero():
    sales = pd.DataFrame({
        "sale_id": [f"s{i}" for i in range(8)],
        "year": [2012, 2013, 2016, 2017, 2020, 2021, 2024, 2025],
        "price_eur": [90e3, 120e3, 160e3, 200e3, 260e3, 300e3, 400e3, 500e3],
        "dwelling_status": ["second_hand"] * 6 + ["new"] * 2,
        "locality": ["Letterkenny", None, "Letterkenny", "Buncrana", None, "Letterkenny",
                     "Buncrana", None]})
    pairs = pd.DataFrame({"sale_id_1": ["s0", "s2"], "sale_id_2": ["s5", "s6"]})
    prof = selection_profile(sales, pairs, {"price_bands_eur": [0, 150000, 300000, 10**8],
                                            "top_localities": 2})
    for _, g in prof.groupby("dimension"):
        assert g.share_all_sales.sum() == pytest.approx(1)
        assert g.share_repeat_legs.sum() == pytest.approx(1)
        assert g.gap.sum() == pytest.approx(0, abs=1e-12)
    assert math.isclose(prof[(prof.dimension == "price_band") & (prof.segment == "300k+")]
                        .share_all_sales.iloc[0], 3 / 8)


def test_final_link_metrics_use_final_properties_and_weights():
    from ireland_rsi.validate import final_link_metrics

    sample = pd.DataFrame({
        "pair_id": ["p1", "p2", "p3", "p4", "p5"],
        "sale_id_a": ["a1", "a2", "a3", "a4", "a5"], "sale_id_b": ["b1", "b2", "b3", "b4", "b5"],
        "sampling_prob": [0.5, 0.1, 0.5, 0.5, 0.5],
        "stratum": ["s", "t", "s", "s", "s"]})
    labels = pd.DataFrame({"pair_id": ["p1", "p2", "p3", "p4", "p5"],
                           "label": ["match", "non_match", "match", "uncertain", "match"]})
    # p1 and p2 end in one property each; p3 does not; p5's sales are missing entirely.
    membership = pd.DataFrame({"sale_id": ["a1", "b1", "a2", "b2", "a3", "b3", "a4", "b4"],
                               "property_id": ["x", "x", "y", "y", "z1", "z2", "u", "u"]})
    m = final_link_metrics(sample, labels, membership, reps=200, seed=1)
    assert m["labelled_binary"] == 4                 # the uncertain pair is excluded
    assert m["linked_labelled"] == 2 and m["false_links"] == 1
    assert m["precision"] == 0.5
    assert m["weighted_precision"] == pytest.approx(2 / 12)
    assert m["weighted_recall"] == pytest.approx(2 / 6)      # p1 linked of p1, p3, p5
    assert m["wp_lo"] <= m["weighted_precision"] <= m["wp_hi"]


def test_label_digest_changes_with_any_label():
    from ireland_rsi.validate import labels_digest

    base = pd.DataFrame({"sample_version": ["v"], "pair_id": ["p1"], "reviewer": ["r"],
                         "labelled_at": [pd.Timestamp("2026-10-07", tz="UTC")],
                         "label": ["match"]})
    assert labels_digest(pd.DataFrame()) == "no-labels"
    assert labels_digest(base) == labels_digest(base.copy())
    assert labels_digest(base) != labels_digest(base.assign(label="non_match"))
    more = pd.concat([base, base.assign(pair_id="p2")], ignore_index=True)
    assert labels_digest(more) != labels_digest(base)


def test_label_digest_ignores_timestamp_representation():
    from ireland_rsi.validate import labels_digest

    utc = pd.DataFrame({"sample_version": ["v"], "pair_id": ["p1"], "reviewer": ["r"],
                        "labelled_at": [pd.Timestamp("2026-10-06 23:52:24.163747", tz="UTC")],
                        "label": ["match"]})
    dublin = utc.assign(labelled_at=utc.labelled_at.dt.tz_convert("Europe/Dublin"))
    naive = utc.assign(labelled_at=utc.labelled_at.dt.tz_localize(None))   # Spark, UTC session
    assert labels_digest(utc) == labels_digest(dublin) == labels_digest(naive)
