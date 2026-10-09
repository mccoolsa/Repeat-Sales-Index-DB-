"""Releases, the current-release pointer, revisions and rollback."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from ireland_rsi.contracts import TABLE_SCHEMAS
from ireland_rsi.index import fit_index
from ireland_rsi.publish import (
    Manifest,
    ReleaseError,
    current_release,
    make_release_id,
    publish_release,
    release_is_complete,
    revision_cause,
    revisions,
    rollback,
)
from ireland_rsi.storage import DuckDBTableStore

ROOT = Path(__file__).resolve().parents[1]
INDEX_CFG = yaml.safe_load((ROOT / "config" / "index.yml").read_text(encoding="utf-8"))
GATE_OK = {"g": {"hard": True, "passed": True, "detail": ""},
           "soft": {"hard": False, "passed": False, "detail": "provisional"}}


def frame(table, rows):
    cols = TABLE_SCHEMAS[table].names
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)


def release_frames(release_id, level=100.0):
    vint = frame("gold.index_vintages", [
        {"release_id": release_id, "geography_id": "scope", "frequency": "annual",
         "period": str(y), "index_value": level + y - 2015, "period_state": "supported",
         "series_supported": True, "endpoint_pairs": 30, "series_properties": 400}
        for y in (2015, 2016)])
    hist = frame("gold.lookup_history", [
        {"release_id": release_id, "sale_id": "s1", "property_id": "p1", "property_sales": 1,
         "sale_date": date(2015, 1, 1), "address": "1 MAIN ST", "anchor_eligible": True,
         "not_full_market_price": False}])
    cal = frame("gold.release_calibration", [
        {"release_id": release_id, "horizon_band": "0-3y", "status": "ok"}])
    return {"gold.index_vintages": vint, "gold.lookup_history": hist,
            "gold.release_calibration": cal,
            "gold.revisions": frame("gold.revisions", [])}


def manifest(release_id, gates=None, previous=None, snapshot="snap1"):
    return Manifest(release_id, "research", "donegal", "ppr:county=Donegal", snapshot, {},
                    gates or GATE_OK, {}, {}, "code", "cfg", previous, "run-1")


@pytest.fixture
def tables():
    return DuckDBTableStore()


def test_release_id_depends_on_every_input():
    a = make_release_id("donegal", "s1", {"index": "fv-1"}, "c", "k")
    assert a == make_release_id("donegal", "s1", {"index": "fv-1"}, "c", "k")
    assert a != make_release_id("donegal", "s2", {"index": "fv-1"}, "c", "k")
    assert a != make_release_id("donegal", "s1", {"index": "fv-2"}, "c", "k")
    assert a != make_release_id("donegal", "s1", {"index": "fv-1"}, "c2", "k")


def test_publish_moves_the_pointer_and_repeating_is_idempotent(tables):
    r = publish_release(tables, manifest("rel-a"), release_frames("rel-a"))
    assert r["published"] and current_release(tables) == "rel-a"
    again = publish_release(tables, manifest("rel-a"), release_frames("rel-a"))
    assert again.get("unchanged") and not again["published"]
    events = tables.query("SELECT * FROM gold.release_events")
    assert len(events) == 1
    assert tables.query("SELECT count(*) FROM gold.index_vintages").iloc[0, 0] == 2


def test_failed_gates_keep_the_previous_release_current(tables):
    publish_release(tables, manifest("rel-a"), release_frames("rel-a"))
    bad = {"g": {"hard": True, "passed": False, "detail": "no supported series"}}
    r = publish_release(tables, manifest("rel-b", gates=bad), release_frames("rel-b"))
    assert not r["published"] and r["failed_gates"] == ["g"]
    assert current_release(tables) == "rel-a"
    assert not release_is_complete(tables, "rel-b")
    with pytest.raises(ReleaseError):
        rollback(tables, "rel-b", "run-2", "should be refused")
    assert current_release(tables) == "rel-a"


def test_a_partially_written_release_never_becomes_visible(tables):
    publish_release(tables, manifest("rel-a"), release_frames("rel-a"))
    with pytest.raises(ReleaseError):
        publish_release(tables, manifest("rel-b"), release_frames("rel-b"),
                        simulate_failure_after="gold.index_vintages")
    assert current_release(tables) == "rel-a"
    assert not release_is_complete(tables, "rel-b")   # no manifest, no history rows


def test_soft_gate_failure_publishes_as_research_not_failed(tables):
    m = manifest("rel-a")
    assert m.hard_gates_passed and not all(g["passed"] for g in m.gates.values())
    r = publish_release(tables, m, release_frames("rel-a"))
    assert r["published"] and r["release_state"] == "research"


def test_rollback_selects_a_prior_complete_release_without_deleting(tables):
    publish_release(tables, manifest("rel-a"), release_frames("rel-a"))
    publish_release(tables, manifest("rel-b", previous="rel-a"), release_frames("rel-b", 110))
    assert current_release(tables) == "rel-b"
    rollback(tables, "rel-a", "run-3", "drill")
    assert current_release(tables) == "rel-a"
    # Both vintages remain and reproduce their own values.
    v = tables.query("SELECT release_id, index_value FROM gold.index_vintages "
                     "WHERE period = '2016' ORDER BY release_id")
    assert list(v.index_value) == [101.0, 111.0]
    rollback(tables, "rel-b", "run-4", "roll forward")
    assert current_release(tables) == "rel-b"


def test_revision_cause_separates_source_from_method():
    prev = {"snapshot_id": "s1", "code_hash": "c", "config_hash": "k"}
    assert revision_cause(None, "s1", "c", "k") == "initial"
    assert revision_cause(prev, "s2", "c", "k") == "source"
    assert revision_cause(prev, "s1", "c2", "k") == "method"
    assert revision_cause(prev, "s2", "c", "k2") == "source+method"
    assert revision_cause(prev, "s1", "c", "k") == "none"


def test_revisions_cover_published_periods_only():
    cur = release_frames("rel-b", 110)["gold.index_vintages"]
    prev = release_frames("rel-a", 100)["gold.index_vintages"]
    prev.loc[0, "period_state"] = "suppressed"
    rev = revisions(cur, prev, "rel-a", "source")
    assert list(rev.period) == ["2016"]
    assert rev.log_revision.iloc[0] == pytest.approx(np.log(111 / 101))
    assert revisions(cur, None, None, "initial").empty


def _pairs(n, seed, years=range(2010, 2017)):
    rng = np.random.default_rng(seed)
    rows = []
    for p in range(n):
        y1, y2 = sorted(rng.choice(list(years), 2, replace=False))
        d1 = date(int(y1), 3, 1) + timedelta(days=int(rng.integers(0, 200)))
        d2 = date(int(y2), 3, 1) + timedelta(days=int(rng.integers(0, 200)))
        lr = 0.05 * (y2 - y1) + rng.normal(0, 0.05)
        rows.append({"property_id": f"p{p}", "date_1": d1, "date_2": d2, "log_return": lr,
                     "holding_years": (d2 - d1).days / 365.25})
    return pd.DataFrame(rows)


def test_a_late_filed_old_sale_revises_old_periods():
    """A full rebuild picks up a late filing for 2011: the 2011 level moves, so updating
    only the newest period would be wrong."""
    pairs = _pairs(300, seed=1)
    late = pd.DataFrame([{"property_id": f"late{i}", "date_1": date(2011, 5, 1),
                          "date_2": date(2016, 5, 1), "log_return": 0.6,
                          "holding_years": 5.0} for i in range(40)])
    before = fit_index(pairs, "annual", INDEX_CFG)
    after = fit_index(pd.concat([pairs, late], ignore_index=True), "annual", INDEX_CFG)
    b = pd.Series(before.index, before.periods)
    a = pd.Series(after.index, after.periods)
    assert abs(np.log(a["2011"] / b["2011"])) > 0.01
    assert b["2015"] == pytest.approx(100) and a["2015"] == pytest.approx(100)
