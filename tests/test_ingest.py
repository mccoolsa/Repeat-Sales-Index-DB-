"""Acquisition validation, immutable landing, row identity and reconciliation."""

import io
import urllib.error
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ireland_rsi.contracts import RunContext, StageStatus, load_contracts
from ireland_rsi.ingest import (
    AcquisitionError,
    FetchResult,
    PromotionPolicy,
    active_sales,
    active_snapshot,
    fetch,
    land_snapshot,
    parse_member,
    raw_row_id,
    validate_archive,
    verify_rebuild,
)
from ireland_rsi.storage import DuckDBTableStore, ObjectStore, sha256_hex

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = load_contracts(ROOT / "config" / "source_contract.yml")
HEADER = ("Date of Sale (dd/mm/yyyy),Address,County,Eircode,Price (€),Not Full Market Price,"
          "VAT Exclusive,Description of Property,Property Size Description")
SH = "Second-Hand Dwelling house /Apartment"
NATIONAL = "ppr:national"
T0 = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)


def row(date="01/02/2024", address="1 Main St, Letterkenny", county="Donegal", eircode="",
        price="€200,000.00", nfmp="No", vat="No", desc=SH, size=""):
    return (date, address, county, eircode, price, nfmp, vat, desc, size)


BASE = [
    row(),
    row(address="2 Main St, Letterkenny", price="€150,000.00"),
    row(address="Apt 4, The Grove, Letterkenny", eircode="F92 AC4D", price="€120,000.00"),
    row(address="5 Braemor Drive, Churchtown", county="Dublin", price="€343,000.00"),
    row(address="5 Braemor Drive, Churchtown", county="Dublin", price="€343,000.00"),  # dup
]


def csv_bytes(rows, terminate=True):
    lines = [HEADER] + [",".join(f'"{v}"' for v in r) for r in rows]
    text = "\r\n".join(lines) + ("\r\n" if terminate else "")
    return text.encode("cp1252")


def zip_bytes(member: bytes, name="PPR-ALL.csv"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, member)
    return buf.getvalue()


def fetched(member: bytes, when=T0, headers=None, name="PPR-ALL.csv"):
    body = zip_bytes(member, name)
    hdrs = {"content-type": "application/x-zip", "content-length": str(len(body))}
    hdrs.update(headers or {})
    return FetchResult("https://example.test/PPR-ALL.zip", 200, hdrs, body, when)


@pytest.fixture
def env(tmp_path):
    return ObjectStore(tmp_path / "raw"), DuckDBTableStore()


def run_ctx(when=T0):
    return RunContext.new(["test"], code_hash="c", config_hash="k", seed=0, as_of=when)


def land(env, member, *, when=T0, scope=NATIONAL, **kw):
    objects, tables = env
    return land_snapshot(fetched(member, when), source="psra_ppr", scope=scope,
                         expected_members=["PPR-ALL.csv"], contracts=CONTRACTS,
                         objects=objects, tables=tables, run=run_ctx(when), **kw)


def active(env):
    return active_sales(env[1], NATIONAL)


# --- acquisition -----------------------------------------------------------


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body, self.status = body, status
        self.headers = headers or {"Content-Length": str(len(body))}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_retries_transient_errors_then_succeeds():
    calls, sleeps = [], []

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise urllib.error.URLError("temporary DNS failure")
        return FakeResponse(b"zipbytes")

    result = fetch("https://x.test/a.zip", opener=opener, sleep=sleeps.append)
    assert result.body == b"zipbytes" and len(calls) == 3
    assert sleeps == [2.0, 4.0]


def test_fetch_does_not_retry_client_errors():
    calls = []

    def opener(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    with pytest.raises(AcquisitionError, match="404"):
        fetch("https://x.test/a.zip", opener=opener, sleep=lambda s: None)
    assert len(calls) == 1


def test_fetch_gives_up_after_bounded_attempts():
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, None)

    with pytest.raises(AcquisitionError, match="3 attempts"):
        fetch("https://x.test/a.zip", opener=opener, sleep=lambda s: None)


@pytest.mark.parametrize("body,headers,match", [
    (b"<!DOCTYPE html><html>Service unavailable</html>", {"content-type": "text/html"}, "HTML"),
    (b"not a zip at all", {"content-type": "application/x-zip"}, "not a valid zip"),
])
def test_validate_rejects_non_archives(body, headers, match):
    with pytest.raises(AcquisitionError, match=match):
        validate_archive(FetchResult("u", 200, headers, body, T0), ["PPR-ALL.csv"])


def test_validate_rejects_incomplete_download():
    good = fetched(csv_bytes(BASE))
    short = FetchResult(good.url, 200, good.headers, good.body[:-100], T0)
    with pytest.raises(AcquisitionError, match="incomplete"):
        validate_archive(short, ["PPR-ALL.csv"])


def test_validate_rejects_unexpected_members():
    with pytest.raises(AcquisitionError, match="unexpected archive members"):
        validate_archive(fetched(csv_bytes(BASE), name="other.csv"), ["PPR-ALL.csv"])


def test_rejected_acquisition_is_recorded_and_promotes_nothing(env):
    objects, tables = env
    page = FetchResult("u", 200, {"content-type": "text/html"}, b"<html>busy</html>", T0)
    result = land_snapshot(page, source="psra_ppr", scope=NATIONAL,
                           expected_members=["PPR-ALL.csv"], contracts=CONTRACTS,
                           objects=objects, tables=tables, run=run_ctx())
    assert result.status is StageStatus.FAILED and result.blocked
    manifest = tables.query("SELECT status, status_reason FROM bronze.source_manifest")
    assert manifest.status.tolist() == ["rejected"]
    assert active_snapshot(tables, NATIONAL) is None


# --- row identity --------------------------------------------------------


def test_raw_row_id_uses_logical_record_ordinal_across_embedded_newlines():
    member = csv_bytes([row(address="Unit 1,\r\nThe Mill"), row(address="2 Main St")])
    sha = sha256_hex(member)
    parsed = parse_member(member, sha, CONTRACTS)
    assert parsed.record_count == 2 and not parsed.structural_problems
    assert parsed.rows.record_ordinal.tolist() == [1, 2]
    assert parsed.rows.raw_row_id.tolist() == [raw_row_id(sha, 1), raw_row_id(sha, 2)]
    assert parsed.rows.address_raw[0] == "Unit 1,\r\nThe Mill"


def test_txn_fingerprint_ignores_case_and_spacing_but_raw_fingerprint_does_not():
    a = parse_member(csv_bytes([row(address="1 Main St")]), "a", CONTRACTS).rows.iloc[0]
    b = parse_member(csv_bytes([row(address="1  MAIN st")]), "b", CONTRACTS).rows.iloc[0]
    assert a.txn_fp == b.txn_fp
    assert a.raw_content_fp != b.raw_content_fp


# --- landing and reconciliation -----------------------------------------------


def test_bytes_are_stored_untouched_by_content_address(env):
    objects, _ = env
    member = csv_bytes(BASE)
    land(env, member)
    assert objects.get(sha256_hex(member), ".csv") == member


def test_ingesting_the_same_file_twice_is_idempotent(env):
    member = csv_bytes(BASE)
    first = land(env, member)
    assert first.outputs["decision"] == "promoted"
    count = len(active(env))
    second = land(env, member, when=T0 + timedelta(days=1))  # fresh download, same bytes
    assert second.outputs["decision"] == "already_active"
    assert second.row_counts["rows_inserted"] == 0
    assert len(active(env)) == count == 5
    events = env[1].query("SELECT decision FROM bronze.snapshot_events")
    assert events.decision.tolist() == ["promoted"]


def test_identical_legitimate_rows_keep_their_multiplicity(env):
    land(env, csv_bytes(BASE))
    sales = active(env)
    dublin = sales[sales.county_raw == "Dublin"]
    assert len(dublin) == 2
    assert dublin.sale_id.nunique() == 2


def test_overlapping_national_snapshots_do_not_double_count(env):
    land(env, csv_bytes(BASE))
    first_ids = set(active(env).sale_id)
    extended = BASE + [row(address="9 New Road, Letterkenny", date="20/09/2026")]
    result = land(env, csv_bytes(extended), when=T0 + timedelta(days=30))
    assert result.outputs["decision"] == "promoted"
    sales = active(env)
    assert len(sales) == 6
    assert first_ids < set(sales.sale_id)
    assert result.row_counts["exact"] == 5 and result.row_counts["new_sales"] == 1


def test_cross_check_scope_never_adds_sales(env):
    land(env, csv_bytes(BASE))
    donegal = [r for r in BASE if r[2] == "Donegal"] + [row(address="Not in national file")]
    result = land(env, csv_bytes(donegal), scope="ppr:county=Donegal/year=2024")
    assert result.outputs["decision"] == "reference"
    assert result.row_counts == {**result.row_counts, "reference_matched": 3,
                                 "reference_unmatched": 1}
    assert len(active(env)) == 5


def test_reordered_rows_give_an_equivalent_active_multiset(env):
    land(env, csv_bytes(BASE))
    before = active(env)
    land(env, csv_bytes(list(reversed(BASE))), when=T0 + timedelta(days=30))
    after = active(env)
    assert Counter(after.raw_content_fp) == Counter(before.raw_content_fp)
    assert set(after.sale_id) == set(before.sale_id)
    assert (after.match_type == "exact").all()


def test_corrected_price_is_a_traceable_new_version(env):
    land(env, csv_bytes(BASE))
    old = active(env)
    old_id = old[old.address_raw == "2 Main St, Letterkenny"].sale_id.item()
    corrected = list(BASE)
    corrected[1] = row(address="2 Main St, Letterkenny", price="€155,000.00")
    result = land(env, csv_bytes(corrected), when=T0 + timedelta(days=30))
    assert result.row_counts["corrections"] == 1
    new = active(env)
    fixed = new[new.address_raw == "2 Main St, Letterkenny"]
    assert fixed.sale_id.item() == old_id
    assert fixed.sale_version.item() == 2 and fixed.match_type.item() == "correction"
    versions = env[1].query(
        f"SELECT sale_version, supersedes_version, change_type, correction_key "
        f"FROM silver.sale_versions WHERE sale_id = '{old_id}' ORDER BY sale_version")
    assert versions.sale_version.tolist() == [1, 2]
    assert versions.supersedes_version.iloc[1] == 1
    assert versions.correction_key.iloc[1] == "address_county_date"


def test_ambiguous_correction_goes_to_review(env):
    rows = [row(address="Apt 1, The Grove", price="€100,000.00"),
            row(address="Apt 1, The Grove", price="€101,000.00")]
    land(env, csv_bytes(rows))
    changed = [row(address="Apt 1, The Grove", price="€102,000.00"),
               row(address="Apt 1, The Grove", price="€103,000.00")]
    result = land(env, csv_bytes(changed), when=T0 + timedelta(days=30),
                  policy=PromotionPolicy(min_retained_share=0.0))
    assert result.row_counts["reviews"] == 2 and result.row_counts["corrections"] == 0
    reviews = env[1].query("SELECT candidate_sale_ids FROM silver.reconciliation_reviews")
    assert all(len(ids.split(",")) == 2 for ids in reviews.candidate_sale_ids)


def test_file_truncated_mid_record_is_rejected_and_not_promoted(env):
    land(env, csv_bytes(BASE))
    good = active_snapshot(env[1], NATIONAL)
    member = csv_bytes(BASE)
    truncated = member[: member.rindex(b'"Dublin"') + 4]  # cut inside the last record
    result = land(env, truncated, when=T0 + timedelta(days=30))
    assert result.outputs["decision"] == "rejected" and result.blocked
    assert active_snapshot(env[1], NATIONAL) == good


def test_file_truncated_at_record_boundary_is_held(env):
    many = [row(address=f"{i} Long Road", price=f"€{100 + i},000.00") for i in range(300)]
    land(env, csv_bytes(many))
    good = active_snapshot(env[1], NATIONAL)
    result = land(env, csv_bytes(many[:250]), when=T0 + timedelta(days=30))
    assert result.outputs["decision"] == "held"
    assert active_snapshot(env[1], NATIONAL) == good
    # Held snapshots leave no silver trace until approved.
    held = result.outputs["snapshot_id"]
    n = env[1].query(f"SELECT count(*) FROM silver.snapshot_membership "
                     f"WHERE snapshot_id = '{held}'").iloc[0, 0]
    assert n == 0


def test_held_snapshot_can_be_approved_explicitly(env):
    many = [row(address=f"{i} Long Road") for i in range(300)]
    land(env, csv_bytes(many))
    result = land(env, csv_bytes(many[:250]), when=T0 + timedelta(days=30), force_promote=True)
    assert result.outputs["decision"] == "promoted"
    assert len(active(env)) == 250


def test_rebuild_from_saved_bytes_matches_bronze(env):
    objects, tables = env
    result = land(env, csv_bytes(BASE))
    report = verify_rebuild(objects, tables, result.outputs["snapshot_id"], CONTRACTS)
    assert report["rows_identical"] is True
    assert report["rebuilt_rows"] == report["stored_rows"] == report["manifest_record_count"] == 5
    assert report["membership_rows"] == 5


def test_county_profile_keeps_only_that_county_with_original_row_ids(env):
    objects, tables = env
    member = csv_bytes(BASE)
    result = land(env, member, scope="ppr:county=Donegal", county_filter="Donegal",
                  policy=PromotionPolicy(authoritative_scope="ppr:county=Donegal"))
    assert result.outputs["decision"] == "promoted"
    assert result.row_counts["records"] == 5 and result.row_counts["records_in_scope"] == 3
    rows = tables.query("SELECT county_raw, record_ordinal, raw_row_id FROM bronze.ppr_rows")
    assert set(rows.county_raw) == {"Donegal"} and sorted(rows.record_ordinal) == [1, 2, 3]
    assert set(rows.raw_row_id) == {raw_row_id(sha256_hex(member), i) for i in (1, 2, 3)}
    report = verify_rebuild(objects, tables, result.outputs["snapshot_id"], CONTRACTS,
                            county_filter="Donegal")
    assert report["rows_identical"] and report["rebuilt_rows"] == 3


def test_one_download_can_serve_national_and_county_scopes(env):
    member = csv_bytes(BASE)
    national = land(env, member)
    county = land(env, member, scope="ppr:county=Donegal", county_filter="Donegal",
                  policy=PromotionPolicy(authoritative_scope="ppr:county=Donegal"))
    assert national.outputs["acquisition_id"] == county.outputs["acquisition_id"]
    assert county.outputs["decision"] == "promoted"


def test_evidence_records_still_refuse_conflicting_facts(tmp_path):
    objects = ObjectStore(tmp_path)
    objects.put_record("a.json", {"x": 1, "scope": "old"})
    objects.put_record("a.json", {"x": 1}, tolerate_extra=("scope",))
    with pytest.raises(FileExistsError):
        objects.put_record("a.json", {"x": 2}, tolerate_extra=("scope",))
    with pytest.raises(FileExistsError):
        objects.put_record("a.json", {"x": 1})  # extra key not tolerated
