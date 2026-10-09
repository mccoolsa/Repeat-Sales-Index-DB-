"""Publication gates and fallback selection."""

from datetime import date
from pathlib import Path

import yaml
from tests.test_uncertainty import CFG, annual_pairs

from ireland_rsi.geography import SeriesSupport, evaluate_series, select_series
from ireland_rsi.uncertainty import property_bootstrap

ROOT = Path(__file__).resolve().parents[1]
POLICY = yaml.safe_load((ROOT / "config" / "publication.yml").read_text(encoding="utf-8"))


def support(geo, freq, supported, states):
    return SeriesSupport(geo, freq, supported, [] if supported else ["thin"], {}, states, {})


def test_large_series_is_supported_and_newest_period_provisional():
    pairs, _ = annual_pairs(1500, seed=1, start=2012, years=6)
    boot = property_bootstrap(pairs, "annual", CFG, draws=60, seed=1)
    s = evaluate_series("scope", "annual", pairs, boot, POLICY)
    assert s.supported, s.reasons
    assert s.period_states["2017"] == "provisional"
    assert s.period_states["2015"] == "supported"


def test_sparse_town_series_is_suppressed_with_reasons():
    pairs, _ = annual_pairs(40, seed=2, start=2012, years=6)
    boot = property_bootstrap(pairs, "annual", CFG, draws=40, seed=2)
    s = evaluate_series("town:X", "annual", pairs, boot, POLICY)
    assert not s.supported
    assert any("properties" in r for r in s.reasons)
    assert set(s.period_states.values()) == {"suppressed"}


def test_thin_periods_are_suppressed_inside_a_supported_series():
    pairs, _ = annual_pairs(1500, seed=3, start=2012, years=6, thin_year=4)
    boot = property_bootstrap(pairs, "annual", CFG, draws=40, seed=3)
    s = evaluate_series("scope", "annual", pairs, boot, POLICY)
    assert s.period_states["2016"] == "suppressed"
    assert "endpoint pairs" in s.period_reasons["2016"]


CHAIN = POLICY["fallback"]["town:Letterkenny"]


def test_annual_town_fallback_is_explicitly_labelled():
    supports = {
        ("town:Letterkenny", "quarterly"): support("town:Letterkenny", "quarterly", False, {}),
        ("town:Letterkenny", "annual"): support("town:Letterkenny", "annual", True,
                                                {"2015": "supported", "2025": "supported"}),
    }
    sel = select_series(CHAIN, supports, date(2015, 5, 1), date(2025, 8, 1))
    assert (sel.geography_id, sel.frequency, sel.fallback_level) == (
        "town:Letterkenny", "annual", 1)
    assert (sel.anchor_period, sel.target_period) == ("2015", "2025")


def test_county_fallback_uses_one_common_series_for_both_periods():
    supports = {
        ("town:Letterkenny", "annual"): support("town:Letterkenny", "annual", True,
                                                {"2015": "supported", "2025": "suppressed"}),
        ("scope", "quarterly"): support("scope", "quarterly", True,
                                        {"2015Q2": "supported", "2025Q3": "provisional"}),
    }
    sel = select_series(CHAIN, supports, date(2015, 5, 1), date(2025, 8, 1))
    # The town shows the anchor but not the target, so both come from the county series.
    assert (sel.geography_id, sel.frequency) == ("scope", "quarterly")
    assert (sel.anchor_period, sel.target_period) == ("2015Q2", "2025Q3")


def test_no_supported_path_returns_no_estimate_with_a_reason():
    supports = {key: support(*key, False, {}) for key in [
        ("town:Letterkenny", "quarterly"), ("town:Letterkenny", "annual"),
        ("scope", "quarterly"), ("scope", "annual")]}
    sel = select_series(CHAIN, supports, date(2015, 5, 1), date(2025, 8, 1))
    assert sel.geography_id is None and sel.reason == "SPARSE_GEOGRAPHY"
    assert len(sel.tried) == 4
