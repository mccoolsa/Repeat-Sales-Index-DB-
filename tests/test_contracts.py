import csv
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ireland_rsi.contracts import (
    ContractViolation,
    QualityEvent,
    RunContext,
    SchemaChangeError,
    Severity,
    SourceField,
    StageResult,
    StageStatus,
    load_contracts,
    select_contract,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ppr_sample_v1.csv"


@pytest.fixture(scope="module")
def contracts():
    return load_contracts(ROOT / "config" / "source_contract.yml")


@pytest.fixture(scope="module")
def v1(contracts):
    return next(c for c in contracts if c.version == 1)


@pytest.fixture(scope="module")
def v1_header(v1):
    return [f.header for f in v1.fields]


def test_fixture_bytes_decode_and_match_contract(v1):
    text = FIXTURE.read_bytes().decode(v1.encoding)
    header = next(csv.reader([text.splitlines()[0]]))
    check = v1.check_header(header)
    assert check.version == 1
    assert check.missing_optional == ()
    assert header[4] == "Price (€)"


def test_fixture_is_not_utf8():
    # Guards against the fixture being re-saved in a different encoding.
    with pytest.raises(UnicodeDecodeError):
        FIXTURE.read_bytes().decode("utf-8")


@pytest.mark.parametrize("dropped", ["Date of Sale (dd/mm/yyyy)", "Price (€)", "Address", "County"])
def test_missing_required_field_fails(v1, v1_header, dropped):
    header = [h for h in v1_header if h != dropped]
    with pytest.raises(ContractViolation):
        v1.check_header(header)


def test_missing_optional_eircode_is_supported(v1, v1_header):
    header = [h for h in v1_header if h != "Eircode"]
    check = v1.check_header(header)
    assert check.missing_optional == ("eircode_raw",)


def test_unknown_column_requires_new_contract_version(v1, v1_header):
    with pytest.raises(SchemaChangeError):
        v1.check_header([*v1_header, "Number of Bedrooms"])


def test_renamed_column_requires_new_contract_version(v1, v1_header):
    header = ["Price (EUR)" if h == "Price (€)" else h for h in v1_header]
    # A renamed required column is both missing and unknown; missing wins.
    with pytest.raises(ContractViolation):
        v1.check_header(header)


def test_reordered_columns_require_new_contract_version(v1, v1_header):
    header = list(v1_header)
    header[1], header[2] = header[2], header[1]
    with pytest.raises(SchemaChangeError):
        v1.check_header(header)


def test_duplicated_column_is_schema_change(v1, v1_header):
    with pytest.raises(SchemaChangeError):
        v1.check_header([*v1_header, "County"])


def test_select_contract_picks_explicit_new_version(v1, v1_header):
    v2 = replace(
        v1,
        version=2,
        fields=(*v1.fields, SourceField("bedrooms_raw", "Number of Bedrooms", required=False)),
    )
    new_header = [*v1_header, "Number of Bedrooms"]

    chosen, _ = select_contract(new_header, [v1, v2])
    assert chosen.version == 2
    chosen, _ = select_contract(v1_header, [v1, v2])
    assert chosen.version == 2  # v2 accepts the old header (new column optional)
    with pytest.raises(SchemaChangeError):
        select_contract(new_header, [v1])


def test_select_contract_reports_violation_when_required_missing(v1, v1_header):
    with pytest.raises(ContractViolation):
        select_contract(v1_header[1:], [v1])


def test_contract_version_must_be_positive_integer(v1):
    with pytest.raises(ValueError):
        replace(v1, version=0)
    with pytest.raises(ValueError):
        replace(v1, version="1")


def test_fingerprint_is_stable_and_version_sensitive(v1):
    assert v1.fingerprint == replace(v1).fingerprint
    assert v1.fingerprint != replace(v1, version=2).fingerprint


def _ctx(**overrides):
    base = dict(
        run_id="r1",
        snapshot_ids=("ppr-all-2026-10-06",),
        as_of=datetime(2026, 10, 6, tzinfo=UTC),
        code_hash="c",
        config_hash="k",
        seed=1,
    )
    return RunContext(**{**base, **overrides})


def test_run_context_requires_explicit_snapshots_and_aware_time():
    assert _ctx().snapshot_ids == ("ppr-all-2026-10-06",)
    with pytest.raises(ValueError):
        _ctx(snapshot_ids=())
    with pytest.raises(ValueError):
        _ctx(snapshot_ids=["ppr-all-2026-10-06"])
    with pytest.raises(ValueError):
        _ctx(as_of=datetime(2026, 10, 6))
    with pytest.raises(ValueError):
        _ctx(seed=-1)


def test_run_context_new_generates_ids():
    a = RunContext.new(["s1"], "c", "k", seed=0)
    b = RunContext.new(["s1"], "c", "k", seed=0)
    assert a.run_id != b.run_id
    assert a.as_of.tzinfo is not None


def test_stage_result_blocked_by_blocking_event():
    ok = StageResult("clean", "r1", StageStatus.SUCCEEDED)
    assert not ok.blocked
    blocked = replace(
        ok, quality_events=(QualityEvent("PRICE_POSITIVE", Severity.BLOCKING, "zero price"),)
    )
    assert blocked.blocked
