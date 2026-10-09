from pathlib import Path

import pytest

from ireland_rsi.contracts import ContractViolation, load_contracts
from ireland_rsi.source_audit import profile_bytes

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ppr_sample_v1.csv"


@pytest.fixture(scope="module")
def contracts():
    return load_contracts(ROOT / "config" / "source_contract.yml")


@pytest.fixture(scope="module")
def profile(contracts):
    return profile_bytes(FIXTURE.read_bytes(), contracts)


def test_counts_and_encoding(profile):
    assert profile["records"] == 5
    assert profile["field_count_distribution"] == {"9": 5}
    assert profile["line_endings"] == {"crlf": 6, "bare_lf": 0}
    assert profile["non_ascii_bytes"]["0x80"] == 6  # header plus five prices


def test_dates_and_prices(profile):
    assert profile["dates"]["min"] == "2010-01-01"
    assert profile["dates"]["max"] == "2025-11-05"
    assert profile["dates"]["unparseable"] == 0
    assert profile["price"]["nonconforming_format"] == 0
    assert profile["price"]["min_eur"] == 95_500.0
    assert profile["price"]["max_eur"] == 227_000.0


def test_exact_duplicates_are_counted_not_removed(profile):
    assert profile["exact_duplicate_rows"] == {
        "extra_occurrences": 1, "groups": 1, "max_multiplicity": 2}
    assert profile["records"] == 5


def test_eircode_profile_normalises_spacing(profile):
    e = profile["eircode"]
    assert e["populated"] == 2
    assert e["strict_valid"] == 2
    assert e["with_space"] == 1
    assert e["codes_with_multiple_sales"] == 1
    assert e["repeated_codes_with_differing_address_text"] == 1


def test_irish_text_survives_decoding(profile):
    assert "Teach/Árasán Cónaithe Atháimhe" in profile["flags"]["description_raw"]


def test_missing_required_column_stops_profiling(contracts):
    raw = FIXTURE.read_bytes().replace(b'"County",', b"", 1)
    with pytest.raises(ContractViolation):
        profile_bytes(raw, contracts)


def test_undecodable_bytes_fail_rather_than_replace(contracts):
    # 0x81 is undefined in cp1252; strict decoding must refuse it.
    raw = FIXTURE.read_bytes().replace(b"Ballyfake", b"Bally\x81fake", 1)
    with pytest.raises(UnicodeDecodeError):
        profile_bytes(raw, contracts)
