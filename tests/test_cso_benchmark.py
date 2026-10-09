"""Check config/cso_benchmark.yml against the archived CSO extract, when present.

The extract lives under data/raw (outside Git), so these tests skip on a fresh clone.
"""

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "config" / "cso_benchmark.yml").read_text(encoding="utf-8"))
ARCHIVE = ROOT / "data" / "raw" / "cso" / CONFIG["observed_in"].removeprefix("cso-rppi-")


def _load(table):
    path = ARCHIVE / f"{table}.json-stat.json"
    if not path.exists():
        pytest.skip(f"archived extract not present: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("table", sorted(CONFIG["tables"]))
def test_series_codes_and_labels_match_extract(table):
    spec = CONFIG["tables"][table]
    data = _load(table)
    assert data["id"][0] == "STATISTIC"
    assert spec["time_dimension"] in data["id"]
    assert spec["series_dimension"] in data["id"]

    stat = data["dimension"]["STATISTIC"]["category"]
    assert stat["label"][spec["index_statistic"]] == "Residential Property Price Index"
    assert stat["unit"][spec["index_statistic"]]["label"] == "Base 2015=100"

    labels = data["dimension"][spec["series_dimension"]]["category"]["label"]
    for series in spec["series"].values():
        assert labels[series["code"]] == series["label"]


@pytest.mark.parametrize("table", sorted(CONFIG["tables"]))
def test_index_values_present_from_declared_start(table):
    spec = CONFIG["tables"][table]
    data = _load(table)
    dims, sizes, values = data["id"], data["size"], data["value"]
    index = {d: data["dimension"][d]["category"]["index"] for d in dims}
    times = index[spec["time_dimension"]]

    def value(time, code):
        sel = {"STATISTIC": spec["index_statistic"], spec["time_dimension"]: time,
               spec["series_dimension"]: code}
        pos = 0
        for d, n in zip(dims, sizes, strict=True):
            pos = pos * n + index[d].index(sel[d])
        return values[pos]

    for series in spec["series"].values():
        start = series["from"].replace("-", "").replace("Q", "")
        covered = [t for t in times if t >= start]
        assert covered, series
        assert all(value(t, series["code"]) is not None for t in covered), series["label"]
