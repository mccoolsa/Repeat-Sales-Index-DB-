"""Typed parsing, VAT rules, comparability flags and disposition accounting."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml

from ireland_rsi.clean import (
    RuleConfig,
    apply_vat_rule,
    classify_source_description,
    clean_sales,
    development_key,
    parse_market_flag,
    parse_price_eur,
    parse_sale_date,
    parse_vat_flag,
    run_clean,
)
from ireland_rsi.contracts import RunContext, load_contracts
from ireland_rsi.ingest import FetchResult, land_snapshot, parse_member
from ireland_rsi.storage import DuckDBTableStore, ObjectStore

ROOT = Path(__file__).resolve().parents[1]
RULES = RuleConfig.load(ROOT / "config" / "quality_rules.yml", ROOT / "config" / "vat_rules.yml")
CONTRACTS = load_contracts(ROOT / "config" / "source_contract.yml")
FLAGS = RULES.quality["flags"]
HEADER = ("Date of Sale (dd/mm/yyyy),Address,County,Eircode,Price (€),Not Full Market Price,"
          "VAT Exclusive,Description of Property,Property Size Description")
SH, NEW = "Second-Hand Dwelling house /Apartment", "New Dwelling house /Apartment"
CUTOFF = date(2026, 10, 6)
RUN = RunContext.new(["test"], code_hash="c", config_hash="k", seed=0,
                     as_of=datetime(2026, 10, 6, tzinfo=UTC))


def row(date_="01/02/2024", address="1 Main St, Letterkenny", county="Donegal", eircode="",
        price="€200,000.00", nfmp="No", vat="No", desc=SH, size=""):
    return (date_, address, county, eircode, price, nfmp, vat, desc, size)


def occurrences(rows):
    text = "\r\n".join([HEADER] + [",".join(f'"{v}"' for v in r) for r in rows]) + "\r\n"
    df = parse_member(text.encode("cp1252"), "m" * 64, CONTRACTS).rows
    return df.assign(snapshot_id="ppr:national:test", sale_id=[f"s{i}" for i in range(len(df))],
                     sale_version=1, match_type="new")


def clean(rows, cutoff=CUTOFF):
    return clean_sales(occurrences(rows), RULES, RUN, cutoff)


def by_address(result, address):
    frame = result.sales[result.sales.address_raw == address]
    assert len(frame) == 1, address
    return frame.iloc[0]


# --- named transformations -------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("29/02/2024", date(2024, 2, 29)),   # leap day
    ("29/02/2023", None),                # not a leap year
    ("31/04/2024", None),
    ("2024-02-01", None),                # wrong format
    ("1/2/2024", None),                  # not zero-padded
    ("", None),
])
def test_parse_sale_date_is_strict(raw, expected):
    assert parse_sale_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("€343,000.00", (34_300_000, None)),
    ("€5,001.00", (500_100, None)),
    ("€387,665,198.00", (38_766_519_800, None)),
    ("€0.00", (None, "PRICE_NOT_POSITIVE")),
    ("", (None, "PRICE_MISSING")),
    ("   ", (None, "PRICE_MISSING")),
    ("343000", (None, "PRICE_MALFORMED")),
    ("€343000.00", (None, "PRICE_MALFORMED")),
    ("€343,000", (None, "PRICE_MALFORMED")),
    ("EUR 343,000.00", (None, "PRICE_MALFORMED")),
    ("€-1,000.00", (None, "PRICE_MALFORMED")),
])
def test_parse_price_eur_returns_integer_cents(raw, expected):
    assert parse_price_eur(raw) == expected


def test_flags_accept_only_yes_and_no():
    assert parse_market_flag("Yes", FLAGS) is True
    assert parse_vat_flag("No", FLAGS) is False
    assert parse_vat_flag("yes", FLAGS) is None
    assert parse_market_flag("", FLAGS) is None


@pytest.mark.parametrize("raw,status,language", [
    (SH, "second_hand", "en"),
    (NEW, "new", "en"),
    ("Teach/Árasán Cónaithe Atháimhe", "second_hand", "ga"),
    ("Teach/Árasán Cónaithe Nua", "new", "ga"),
    ("Teach/?ras?n C?naithe Nua", "new", "ga"),   # source-corrupted fadas
])
def test_classify_source_description(raw, status, language):
    assert classify_source_description(raw, RULES.quality["descriptions"]) == {
        "status": status, "language": language}


def test_unknown_description_is_not_guessed():
    assert classify_source_description("House", RULES.quality["descriptions"]) is None


def test_vat_13_5_percent_example():
    result = apply_vat_rule(20_000_000, date(2024, 5, 1), RULES.vat)
    assert result.rule_id == "VAT_RESIDENTIAL_13_5" and result.rate == "0.135"
    assert result.gross_cents == 22_700_000   # EUR 200,000 -> EUR 227,000


def test_vat_9_percent_example_needs_evidence_of_a_qualifying_apartment():
    sale = date(2026, 1, 15)
    unresolved = apply_vat_rule(20_000_000, sale, RULES.vat)
    assert not unresolved.resolved and unresolved.rule_id == "VAT_QUALIFYING_APARTMENT_9_OR_13_5"
    qualifying = apply_vat_rule(20_000_000, sale, RULES.vat, qualifying_apartment=True)
    assert qualifying.gross_cents == 21_800_000   # EUR 200,000 -> EUR 218,000
    other = apply_vat_rule(20_000_000, sale, RULES.vat, qualifying_apartment=False)
    assert other.gross_cents == 22_700_000


def test_vat_boundary_dates():
    assert apply_vat_rule(100, date(2025, 10, 7), RULES.vat).resolved
    assert not apply_vat_rule(100, date(2025, 10, 8), RULES.vat).resolved


def test_vat_rounding_is_half_up_to_the_cent():
    # 101 cents * 1.135 = 114.635 -> 115
    assert apply_vat_rule(101, date(2024, 1, 1), RULES.vat).gross_cents == 115


def test_development_key_drops_numbers_and_unit_words():
    assert development_key("Apt 4, The Grove, Letterkenny") == "the grove letterkenny"
    assert development_key("114 Charlestown Centre, Finglas") == "charlestown centre finglas"


# --- snapshot cleaning -------------------------------------------------------------


def test_second_hand_market_sale_is_eligible_at_reported_price():
    s = by_address(clean([row()]), "1 Main St, Letterkenny")
    assert s.disposition == "eligible" and s.index_eligible and s.linkage_eligible
    assert s.price_basis == "reported" and s.analysis_price_cents == 20_000_000
    assert s.sale_quarter == "2024Q1" and s.reason_codes == ""


def test_quoted_commas_survive_parsing():
    result = clean([row(address="Apt 2, Block B, Main St, Letterkenny")])
    assert by_address(result, "Apt 2, Block B, Main St, Letterkenny").disposition == "eligible"


def test_unparseable_rows_are_quarantined_not_dropped():
    rows = [row(date_="29/02/2023", address="a"), row(price="€0.00", address="b"),
            row(price="", address="c"), row(price="343000", address="d"),
            row(nfmp="Maybe", address="e"), row(vat="", address="f"),
            row(county="Antrim", address="g"), row(address="   "),
            row(desc="Bungalow", address="h"), row(date_="01/01/2009", address="i"),
            row(date_="01/01/2027", address="j"), row(address="ok")]
    result = clean(rows)
    assert len(result.sales) == 1 and len(result.quarantine) == 11
    codes = dict(zip(result.quarantine.raw_row_id, result.quarantine.reason_codes, strict=True))
    occ = occurrences(rows)
    expected = ["DATE_INVALID", "PRICE_NOT_POSITIVE", "PRICE_MISSING", "PRICE_MALFORMED",
                "MARKET_FLAG_UNKNOWN", "VAT_FLAG_UNKNOWN", "COUNTY_UNKNOWN", "ADDRESS_MISSING",
                "DESCRIPTION_UNKNOWN", "DATE_BEFORE_SCOPE", "DATE_AFTER_CUTOFF"]
    for raw_id, code in zip(occ.raw_row_id, expected, strict=False):
        assert codes[raw_id] == code
    assert (result.quarantine.resolution_state == "open").all()


def test_missing_optional_fields_are_allowed():
    s = by_address(clean([row(eircode="", size="")]), "1 Main St, Letterkenny")
    assert pd.isna(s.eircode) and pd.isna(s.size_band) and s.disposition == "eligible"


def test_non_market_sale_is_kept_but_excluded():
    s = by_address(clean([row(nfmp="Yes")]), "1 Main St, Letterkenny")
    assert s.disposition == "excluded" and s.reason_codes == "NON_MARKET_SALE"
    assert s.parse_valid and s.linkage_eligible and not s.index_eligible


def test_vat_exclusive_new_dwelling_is_grossed_up_once():
    result = clean([row(desc=NEW, vat="Yes", date_="01/05/2024")])
    s = by_address(result, "1 Main St, Letterkenny")
    assert s.reported_price_cents == 20_000_000 and s.gross_price_cents == 22_700_000
    assert s.analysis_price_cents == 22_700_000 and s.price_basis == "gross_from_vat_exclusive"
    assert s.vat_rule_id == "VAT_RESIDENTIAL_13_5" and s.index_eligible
    event = result.events[result.events.rule_id == "VAT_ADJUSTED"].iloc[0]
    assert (event.value_before, event.value_after) == ("20000000", "22700000")


def test_new_dwelling_after_9_percent_regime_is_unresolved():
    s = by_address(clean([row(desc=NEW, vat="Yes", date_="15/01/2026")]),
                   "1 Main St, Letterkenny")
    assert s.disposition == "excluded" and "VAT_RULE_UNRESOLVED" in s.reason_codes
    assert s.gross_price_cents is pd.NA or pd.isna(s.gross_price_cents)
    assert not s.index_eligible


def test_vat_conflicts():
    result = clean([row(vat="Yes", address="second-hand but VAT exclusive"),
                    row(desc=NEW, vat="No", address="new but not VAT exclusive")])
    conflict = by_address(result, "second-hand but VAT exclusive")
    assert conflict.disposition == "pending_review" and conflict.reason_codes == "VAT_FLAG_CONFLICT"
    uncertain = by_address(result, "new but not VAT exclusive")
    assert uncertain.disposition == "excluded" and uncertain.price_basis == "uncertain"


def test_legitimate_high_value_is_flagged_for_review_not_invalidated():
    s = by_address(clean([row(price="€6,250,000.00")]), "1 Main St, Letterkenny")
    assert s.parse_valid and s.disposition == "pending_review"
    assert s.reason_codes == "PRICE_REVIEW_HIGH" and s.reported_price_cents == 625_000_000


def test_bulk_filing_same_development_same_day_same_price():
    rows = [row(address=f"{i} Ashley Hall, Palmerstown", price="€191,000.00",
                date_="19/12/2016", county="Dublin") for i in range(1, 4)]
    rows.append(row(address="7 Elsewhere, Palmerstown", price="€191,000.00",
                    date_="19/12/2016", county="Dublin"))
    result = clean(rows)
    flagged = result.sales[result.sales.reason_codes.str.contains("POSSIBLE_BULK_FILING")]
    assert set(flagged.address_raw) == {"1 Ashley Hall, Palmerstown", "2 Ashley Hall, Palmerstown",
                                        "3 Ashley Hall, Palmerstown"}


@pytest.mark.parametrize("address,flagged", [
    ("25-40 Rathard Lawn, Ballyvolane", True),
    ("6 and 8 The Grove, Hollystown", True),
    ("1  2 and 9 to 16 inclusive, Railway View", True),
    ("APT 10, 18-21 MOUNT ST, DUBLIN 2", False),
    ("12A Main Street", False),
])
def test_multi_unit_addresses(address, flagged):
    s = by_address(clean([row(address=address)]), address)
    assert ("MULTI_UNIT_ADDRESS" in s.reason_codes) is flagged
    assert s.linkage_eligible is not flagged


def test_identical_rows_are_both_kept_and_noted():
    result = clean([row(), row()])
    assert len(result.sales) == 2
    assert (result.sales.duplicate_group_size == 2).all()
    assert (result.events.rule_id == "DUPLICATE_OCCURRENCE").sum() == 2


def test_multiple_flags_do_not_double_count_dispositions():
    rows = [row(nfmp="Yes", price="€6,000,000.00", address="both"), row(address="plain"),
            row(price="", address="bad")]
    summary = clean(rows).summary
    assert summary["total_occurrences"] == 3
    assert summary["by_disposition"] == {"eligible": 1, "excluded": 1, "quarantine": 1}
    assert summary["by_reason"]["NON_MARKET_SALE"] == 1
    assert summary["by_reason"]["PRICE_REVIEW_HIGH"] == 1
    assert all(summary["checks"].values())


def test_rule_set_version_changes_when_rules_change(tmp_path):
    changed = dict(RULES.quality, price_review_bounds_eur={"low": 1, "high": 2})
    path = tmp_path / "quality.yml"
    path.write_text(yaml.safe_dump(changed, allow_unicode=True), encoding="utf-8")
    other = RuleConfig.load(path, ROOT / "config" / "vat_rules.yml")
    assert other.version != RULES.version
    assert other.version.startswith("qr-2026-10-06+")


def test_run_clean_writes_silver_tables_idempotently(tmp_path):
    rows = [row(), row(nfmp="Yes", address="nm"), row(price="", address="bad")]
    text = "\r\n".join([HEADER] + [",".join(f'"{v}"' for v in r) for r in rows]) + "\r\n"
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PPR-ALL.csv", text.encode("cp1252"))
    body = buf.getvalue()
    fetched = FetchResult("u", 200, {"content-length": str(len(body))}, body,
                          datetime(2026, 10, 6, tzinfo=UTC))
    tables = DuckDBTableStore()
    land_snapshot(fetched, source="psra_ppr", scope="ppr:national",
                  expected_members=["PPR-ALL.csv"], contracts=CONTRACTS,
                  objects=ObjectStore(tmp_path), tables=tables, run=RUN)
    stage, result = run_clean(tables, "ppr:national", RULES, RUN)
    assert stage.row_counts["sales_inserted"] == 2
    assert stage.row_counts["quarantine_inserted"] == 1
    again, _ = run_clean(tables, "ppr:national", RULES, RUN)
    assert again.row_counts["sales_inserted"] == 0 and again.row_counts["events_inserted"] == 0


def test_placeholder_eircodes_are_not_valid_identifiers():
    rows = [row(eircode="A123456", address="a"),
            row(eircode="F92X1Y2", county="Donegal", address="b1"),
            row(eircode="F92X1Y2", county="Cork", address="b2"),
            row(eircode="F92X1Y2", county="Kerry", address="b3"),   # 3 counties: not real
            row(eircode="F92AC4D", address="c")]
    result = clean(rows)
    valid = dict(zip(result.sales.address_raw, result.sales.eircode_valid, strict=True))
    assert valid == {"a": False, "b1": False, "b2": False, "b3": False, "c": True}
    assert (result.events.rule_id == "EIRCODE_PLACEHOLDER").sum() == 4
