"""Repeat-sale pairs from property histories."""

import math
from datetime import date
from pathlib import Path

import pandas as pd

from ireland_rsi.entities import EntityConfig
from ireland_rsi.pairs import audit_pairs, build_repeat_pairs

ROOT = Path(__file__).resolve().parents[1]
CFG = EntityConfig.load(ROOT / "config" / "entities.yml").raw["pairs"]


def quarter(d):
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def history(*rows, prop="p1"):
    """rows: (sale_id, date, price_euros, eligible, status[, version, content])"""
    out = []
    for r in rows:
        sale_id, d, price, eligible, status = r[:5]
        version = r[5] if len(r) > 5 else 1
        content = r[6] if len(r) > 6 else f"fp_{sale_id}"
        out.append({"property_id": prop, "sale_id": sale_id, "sale_version": version,
                    "sale_date": d, "sale_quarter": quarter(d),
                    "analysis_price_cents": price * 100 if eligible else None,
                    "reported_price_cents": price * 100, "index_eligible": eligible,
                    "dwelling_status": status, "raw_content_fp": content})
    return pd.DataFrame(out)


SH = "second_hand"


def test_three_sales_give_two_consecutive_pairs():
    h = history(("a", date(2012, 3, 1), 150_000, True, SH),
                ("b", date(2016, 6, 1), 200_000, True, SH),
                ("c", date(2022, 9, 1), 300_000, True, SH))
    pairs, stats = build_repeat_pairs(h, CFG, "v1")
    assert list(zip(pairs.sale_id_1, pairs.sale_id_2, strict=True)) == [("a", "b"), ("b", "c")]
    assert (pairs.decision == "included_headline").all()
    first = pairs.iloc[0]
    assert math.isclose(first.log_return, math.log(200_000 / 150_000))
    assert math.isclose(first.holding_years, (date(2016, 6, 1) - date(2012, 3, 1)).days / 365.25)
    assert stats["headline_pairs"] == 2


def test_same_quarter_pair_is_diagnostic_only():
    h = history(("a", date(2018, 1, 10), 150_000, True, SH),
                ("b", date(2018, 3, 20), 160_000, True, SH))
    pairs, _ = build_repeat_pairs(h, CFG, "v1")
    assert pairs.decision.tolist() == ["diagnostic_same_quarter"]


def test_non_comparable_middle_sale_breaks_the_history():
    h = history(("a", date(2012, 1, 1), 150_000, True, SH),
                ("b", date(2015, 1, 1), 50_000, False, SH),      # non-market transfer
                ("c", date(2020, 1, 1), 250_000, True, SH))
    pairs, stats = build_repeat_pairs(h, CFG, "v1")
    assert pairs.empty and stats["segment_breaks"] == 1
    bridged, _ = build_repeat_pairs(h, {**CFG, "bridge_non_comparable": True}, "v1")
    assert list(zip(bridged.sale_id_1, bridged.sale_id_2, strict=True)) == [("a", "c")]


def test_portfolio_rows_cannot_anchor_a_pair():
    # A flagged bulk filing is not index-eligible, so it is never a price anchor.
    h = history(("bulk", date(2014, 5, 1), 8_000, False, SH),
                ("later", date(2019, 5, 1), 180_000, True, SH))
    pairs, _ = build_repeat_pairs(h, CFG, "v1")
    assert pairs.empty


def test_corrected_middle_sale_rebuilds_both_adjacent_pairs():
    base = [("a", date(2012, 3, 1), 150_000, True, SH), ("b", date(2016, 6, 1), 200_000, True, SH),
            ("c", date(2022, 9, 1), 300_000, True, SH)]
    before, _ = build_repeat_pairs(history(*base), CFG, "v1")
    corrected = history(base[0], ("b", date(2016, 6, 1), 210_000, True, SH, 2), base[2])
    after, _ = build_repeat_pairs(corrected, CFG, "v1")
    assert after.price_cents_2.iloc[0] == after.price_cents_1.iloc[1] == 21_000_000
    assert set(before.pair_id).isdisjoint(after.pair_id)          # both pairs are new versions
    assert after.sale_version_2.iloc[0] == after.sale_version_1.iloc[1] == 2


def test_new_build_legs():
    h = history(("a", date(2012, 1, 1), 200_000, True, "new"),
                ("b", date(2018, 1, 1), 260_000, True, SH),
                ("c", date(2021, 1, 1), 300_000, True, "new"))
    pairs, _ = build_repeat_pairs(h, CFG, "v1")
    assert pairs.decision.tolist() == ["included_new_first_leg", "excluded_new_second_leg"]


def test_duplicate_filing_is_one_transaction_not_a_zero_hold_pair():
    h = history(("a1", date(2015, 2, 1), 150_000, True, SH, 1, "same"),
                ("a2", date(2015, 2, 1), 150_000, True, SH, 1, "same"),
                ("b", date(2020, 2, 1), 220_000, True, SH))
    pairs, stats = build_repeat_pairs(h, CFG, "v1")
    assert len(pairs) == 1 and stats["duplicate_occurrences"] == 1
    assert (pairs.holding_days > 0).all()


def test_proxies_are_flagged_with_their_denominator():
    h = history(("a", date(2020, 1, 1), 100_000, True, SH),
                ("b", date(2021, 1, 1), 200_000, True, SH),
                ("c", date(2030, 1, 1), 230_000, True, SH))
    pairs, stats = build_repeat_pairs(h, CFG, "v1")
    assert pairs.short_hold.tolist() == [True, False]
    assert pairs.unusual_change.tolist() == [True, False]   # doubling in one year
    assert stats["proxies_included"] == {"denominator_pairs": 2, "short_hold_resales": 1,
                                         "unusual_price_changes": 1}


def test_audit_passes_for_built_pairs_and_catches_a_bridge():
    h = history(("a", date(2012, 1, 1), 150_000, True, SH),
                ("b", date(2015, 1, 1), 50_000, False, SH),
                ("c", date(2020, 1, 1), 250_000, True, SH),
                ("d", date(2024, 1, 1), 300_000, True, SH))
    pairs, _ = build_repeat_pairs(h, CFG, "v1")
    assert audit_pairs(pairs, h, bridge=False)["passed"]
    bridged, _ = build_repeat_pairs(h, {**CFG, "bridge_non_comparable": True}, "v1")
    report = audit_pairs(bridged, h, bridge=False)
    assert not report["passed"] and report["violations"]["bridged_non_comparable"] == 1
