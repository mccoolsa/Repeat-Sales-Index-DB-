"""Acquisition, immutable bronze landing and snapshot reconciliation.

Flow for one acquisition:

    fetch (bounded retries) -> validate archive -> store bytes by content address
    -> parse members under a SourceContract -> bronze.ppr_rows (one row per occurrence)
    -> reconcile against the active snapshot of the same scope -> promote / hold / reject

Identity rules:
* raw_row_id   = SHA256(member_sha256 | logical CSV record ordinal). Quoted records that span
  physical lines are one record.
* raw_content_fp = SHA256 of the length-delimited raw field strings.
* txn_fp       = SHA256 of length-delimited canonical fields (whitespace-collapsed, casefolded);
  versioned so the canonicalisation can change without silently changing identity.
* sale_id      is assigned by reconciliation and never derived from content alone.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import io
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from ireland_rsi.contracts import (
    PPR_RAW_FIELDS,
    TABLE_SCHEMAS,
    ContractError,
    QualityEvent,
    RunContext,
    Severity,
    SnapshotDecision,
    SourceContract,
    StageResult,
    StageStatus,
    select_contract,
)
from ireland_rsi.storage import ObjectStore, TableStore, sha256_hex

TXN_FP_VERSION = "txfp_v1"
USER_AGENT = "ireland-rsi/0.1 (research; repeat-sales index)"


class AcquisitionError(Exception):
    """The download is missing, incomplete or not the expected archive."""


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    retrieved_utc: datetime


def fetch(
    url: str,
    *,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    timeout: float = 120.0,
    opener: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FetchResult:
    """GET with bounded retries for transient failures. 4xx responses are not retried."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        retrieved = now()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener(request, timeout=timeout) as response:
                body = response.read()
                headers = {k.lower(): v for k, v in response.headers.items()}
                return FetchResult(url, response.status, headers, body, retrieved)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise AcquisitionError(f"HTTP {exc.code} for {url}") from exc
            last_error = exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError) as exc:
            last_error = exc
        if attempt < attempts:
            sleep(backoff_seconds * 2 ** (attempt - 1))
    raise AcquisitionError(f"{url} failed after {attempts} attempts: {last_error}")


def fetch_result_from_saved(directory: str | Path) -> FetchResult:
    """Rebuild a FetchResult from an acquisition saved by hand (manifest + headers file)."""
    import json

    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    lines = (directory / "headers.txt").read_text(encoding="latin-1").splitlines()
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    body = (directory / manifest["archive"]["path"]).read_bytes()
    retrieved = datetime.fromisoformat(manifest["retrieved_utc"].replace("Z", "+00:00"))
    return FetchResult(manifest["source_url"], status, headers, body, retrieved)


def validate_archive(fetched: FetchResult, expected_members: Iterable[str]) -> dict[str, bytes]:
    """Return member bytes, or raise AcquisitionError for anything but the expected zip."""
    body = fetched.body
    declared = fetched.headers.get("content-length")
    if declared is not None and int(declared) != len(body):
        raise AcquisitionError(f"incomplete download: {len(body)} of {declared} bytes")
    content_type = fetched.headers.get("content-type", "").lower()
    if "html" in content_type or body.lstrip()[:1] == b"<":
        raise AcquisitionError("response is an HTML page, not an archive")
    if not body or not zipfile.is_zipfile(io.BytesIO(body)):
        raise AcquisitionError("response is not a valid zip archive")
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = sorted(zf.namelist())
        if names != sorted(expected_members):
            raise AcquisitionError(f"unexpected archive members {names}")
        bad = zf.testzip()
        if bad is not None:
            raise AcquisitionError(f"archive member {bad} failed its CRC check")
        return {name: zf.read(name) for name in names}


# ---------------------------------------------------------------------------
# Parsing and fingerprints
# ---------------------------------------------------------------------------


def _length_delimited(values: Iterable[str | None]) -> bytes:
    parts = ("-1:" if v is None else f"{len(v)}:{v}" for v in values)
    return "|".join(parts).encode("utf-8")


def _canon(value: str | None) -> str | None:
    return None if value is None else " ".join(value.split()).casefold()


def raw_row_id(member_sha256: str, ordinal: int) -> str:
    return sha256_hex(f"{member_sha256}|{ordinal}".encode())


def raw_content_fp(values: Iterable[str | None]) -> str:
    return sha256_hex(_length_delimited(values))


def txn_fp(fields: dict[str, str | None]) -> str:
    canonical = [_canon(fields[name]) for name in PPR_RAW_FIELDS]
    eircode_index = PPR_RAW_FIELDS.index("eircode_raw")
    if canonical[eircode_index] is not None:
        canonical[eircode_index] = canonical[eircode_index].replace(" ", "")
    return sha256_hex(_length_delimited([TXN_FP_VERSION, *canonical]))


@dataclass(frozen=True)
class ParsedMember:
    contract: SourceContract
    rows: pd.DataFrame
    record_count: int
    bad_field_count_ordinals: tuple[int, ...]
    ends_with_terminator: bool

    @property
    def structural_problems(self) -> list[str]:
        problems = []
        if self.record_count == 0:
            problems.append("no data records")
        if self.bad_field_count_ordinals:
            first = self.bad_field_count_ordinals[:5]
            problems.append(
                f"{len(self.bad_field_count_ordinals)} records with wrong field count "
                f"(first ordinals {list(first)})")
        if not self.ends_with_terminator:
            problems.append("file does not end with a record terminator (possible truncation)")
        return problems


def parse_member(member_bytes: bytes, member_sha256: str,
                 contracts: Iterable[SourceContract]) -> ParsedMember:
    """Parse one CSV member. Raises ContractError or UnicodeDecodeError on unusable input."""
    contracts = tuple(contracts)
    encodings = list(dict.fromkeys(c.encoding for c in contracts))
    text = None
    for encoding in encodings:
        try:
            text = member_bytes.decode(encoding)  # strict; no replacement characters
            break
        except UnicodeDecodeError:
            if encoding == encodings[-1]:
                raise
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:
        raise ContractError("empty file")
    contract, _ = select_contract(header, [c for c in contracts if c.encoding == encoding])
    position = {f.name: header.index(f.header) for f in contract.fields if f.header in header}
    width = len(header)

    records, bad = [], []
    for ordinal, record in enumerate(reader, start=1):
        if len(record) != width:
            bad.append(ordinal)
            continue
        fields = {name: (record[position[name]] if name in position else None)
                  for name in PPR_RAW_FIELDS}
        values = [fields[name] for name in PPR_RAW_FIELDS]
        records.append((
            raw_row_id(member_sha256, ordinal), member_sha256, ordinal, *values,
            raw_content_fp(values), txn_fp(fields), TXN_FP_VERSION,
        ))
    rows = pd.DataFrame.from_records(records, columns=TABLE_SCHEMAS["bronze.ppr_rows"].names)
    ends = text.endswith(("\r\n", "\n")) if text else False
    return ParsedMember(contract, rows, len(records) + len(bad), tuple(bad), ends)


# ---------------------------------------------------------------------------
# Snapshot reconciliation
# ---------------------------------------------------------------------------

CORRECTION_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("address_county_date", ("address_raw", "county_raw", "sale_date_raw")),
    ("address_county_price", ("address_raw", "county_raw", "price_raw")),
    ("eircode_county_date_price", ("eircode_raw", "county_raw", "sale_date_raw", "price_raw")),
)


def _key(row: dict, columns: tuple[str, ...]) -> tuple | None:
    values = []
    for column in columns:
        value = _canon(row.get(column))
        if column == "eircode_raw":
            if not value:
                return None  # an absent Eircode cannot identify anything
            value = value.replace(" ", "")
        values.append(value)
    return tuple(values)


def new_sale_id(snapshot_id: str, row_id: str) -> str:
    return "sale_" + sha256_hex(f"{snapshot_id}|{row_id}".encode())[:24]


@dataclass
class Reconciliation:
    membership: pd.DataFrame
    sale_versions: pd.DataFrame
    reviews: pd.DataFrame
    stats: dict[str, int] = field(default_factory=dict)


def reconcile(snapshot_id: str, new_rows: pd.DataFrame, previous: pd.DataFrame | None,
              max_versions: dict[str, int]) -> Reconciliation:
    """Assign sale identities to every occurrence in a new snapshot.

    1. Exact raw-content matches with the previous snapshot keep their sale and version,
       pairing occurrences one-to-one so multiplicity is preserved.
    2. Remaining occurrences are matched to remaining previous ones when exactly one
       candidate exists on each side under a correction key; that adds a new version.
    3. Anything else is a new sale; if it had ambiguous candidates it is also queued for
       review. Previous occurrences left unmatched are absent, not deleted.
    """
    new = new_rows.sort_values("record_ordinal").to_dict("records")
    prev = [] if previous is None else previous.sort_values(
        ["sale_id", "raw_row_id"]).to_dict("records")

    membership, versions, reviews = [], [], []
    matched_new: set[str] = set()
    stats = dict(previous_occurrences=len(prev), new_occurrences=len(new), exact=0,
                 corrections=0, new_sales=0, reviews=0, absent_from_new=0)

    # 1. Exact content matches, one-to-one per occurrence.
    prev_by_fp: dict[str, list[dict]] = defaultdict(list)
    for row in prev:
        prev_by_fp[row["raw_content_fp"]].append(row)
    for row in new:
        bucket = prev_by_fp.get(row["raw_content_fp"])
        if bucket:
            old = bucket.pop(0)
            membership.append((snapshot_id, row["raw_row_id"], old["sale_id"],
                               int(old["sale_version"]), "exact"))
            matched_new.add(row["raw_row_id"])
            stats["exact"] += 1
    prev_left = [row for bucket in prev_by_fp.values() for row in bucket]
    new_left = [row for row in new if row["raw_row_id"] not in matched_new]

    # 2. Unique one-to-one correction candidates, strongest key first.
    candidates: dict[str, set[str]] = defaultdict(set)
    for key_name, columns in CORRECTION_KEYS:
        old_groups: dict[tuple, list[dict]] = defaultdict(list)
        new_groups: dict[tuple, list[dict]] = defaultdict(list)
        for row in prev_left:
            if (key := _key(row, columns)) is not None:
                old_groups[key].append(row)
        for row in new_left:
            if (key := _key(row, columns)) is not None:
                new_groups[key].append(row)
        used_old, used_new = set(), set()
        for key, new_group in new_groups.items():
            old_group = old_groups.get(key, [])
            if len(old_group) == 1 and len(new_group) == 1:
                old, row = old_group[0], new_group[0]
                version = max(max_versions.get(old["sale_id"], 0), int(old["sale_version"])) + 1
                max_versions[old["sale_id"]] = version
                versions.append((old["sale_id"], version, row["raw_content_fp"], row["txn_fp"],
                                 snapshot_id, row["raw_row_id"], int(old["sale_version"]),
                                 "correction", key_name))
                membership.append((snapshot_id, row["raw_row_id"], old["sale_id"], version,
                                   "correction"))
                used_old.add(old["raw_row_id"])
                used_new.add(row["raw_row_id"])
                stats["corrections"] += 1
            elif old_group:
                for row in new_group:
                    candidates[row["raw_row_id"]].update(o["sale_id"] for o in old_group)
        prev_left = [r for r in prev_left if r["raw_row_id"] not in used_old]
        new_left = [r for r in new_left if r["raw_row_id"] not in used_new]

    # 3. New sales, with ambiguous ones queued for review.
    for row in new_left:
        sale_id = new_sale_id(snapshot_id, row["raw_row_id"])
        ambiguous = candidates.get(row["raw_row_id"])
        versions.append((sale_id, 1, row["raw_content_fp"], row["txn_fp"], snapshot_id,
                         row["raw_row_id"], None, "new", None))
        membership.append((snapshot_id, row["raw_row_id"], sale_id, 1,
                           "new_review" if ambiguous else "new"))
        stats["new_sales"] += 1
        if ambiguous:
            reviews.append((snapshot_id, row["raw_row_id"],
                            "ambiguous correction candidates", ",".join(sorted(ambiguous))))
            stats["reviews"] += 1
    stats["absent_from_new"] = len(prev_left)

    return Reconciliation(
        pd.DataFrame(membership, columns=TABLE_SCHEMAS["silver.snapshot_membership"].names),
        pd.DataFrame(versions, columns=TABLE_SCHEMAS["silver.sale_versions"].names),
        pd.DataFrame(reviews, columns=TABLE_SCHEMAS["silver.reconciliation_reviews"].names),
        dict(stats),
    )


def match_reference(snapshot_id: str, new_rows: pd.DataFrame,
                    authoritative: pd.DataFrame | None) -> tuple[pd.DataFrame, dict[str, int]]:
    """Map a non-authoritative snapshot onto the active authoritative one by exact content.

    Unmatched occurrences get no sale_id: a cross-check file never creates sales.
    """
    pool: dict[str, list[dict]] = defaultdict(list)
    if authoritative is not None:
        for row in authoritative.sort_values(["sale_id", "raw_row_id"]).to_dict("records"):
            pool[row["raw_content_fp"]].append(row)
    membership, matched = [], 0
    for row in new_rows.sort_values("record_ordinal").to_dict("records"):
        bucket = pool.get(row["raw_content_fp"])
        if bucket:
            old = bucket.pop(0)
            membership.append((snapshot_id, row["raw_row_id"], old["sale_id"],
                               int(old["sale_version"]), "reference_exact"))
            matched += 1
        else:
            membership.append((snapshot_id, row["raw_row_id"], None, None, "reference_unmatched"))
    frame = pd.DataFrame(membership, columns=TABLE_SCHEMAS["silver.snapshot_membership"].names)
    return frame, {"reference_matched": matched, "reference_unmatched": len(frame) - matched}


# ---------------------------------------------------------------------------
# Snapshot state queries
# ---------------------------------------------------------------------------


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def active_snapshot(tables: TableStore, scope: str) -> str | None:
    if not tables.exists("bronze.snapshot_events"):
        return None
    df = tables.query(
        f"SELECT snapshot_id FROM {tables.qualify('bronze.snapshot_events')} "
        f"WHERE scope = {_lit(scope)} AND decision = 'promoted' "
        "ORDER BY decided_at DESC, event_id DESC LIMIT 1")
    return None if df.empty else str(df.iloc[0, 0])


def snapshot_occurrences(tables: TableStore, snapshot_id: str) -> pd.DataFrame:
    """Occurrences of a snapshot with their assigned sales and the raw fields used to match."""
    m = tables.qualify("silver.snapshot_membership")
    r = tables.qualify("bronze.ppr_rows")
    return tables.query(
        "SELECT m.raw_row_id, m.sale_id, m.sale_version, r.record_ordinal, r.raw_content_fp, "
        "r.txn_fp, r.address_raw, r.county_raw, r.sale_date_raw, r.price_raw, r.eircode_raw "
        f"FROM {m} m JOIN {r} r ON m.raw_row_id = r.raw_row_id "
        f"WHERE m.snapshot_id = {_lit(snapshot_id)} AND m.sale_id IS NOT NULL")


def max_sale_versions(tables: TableStore, excluding_snapshot: str) -> dict[str, int]:
    if not tables.exists("silver.sale_versions"):
        return {}
    df = tables.query(
        f"SELECT sale_id, max(sale_version) AS v FROM {tables.qualify('silver.sale_versions')} "
        f"WHERE created_in_snapshot <> {_lit(excluding_snapshot)} GROUP BY sale_id")
    return dict(zip(df["sale_id"], df["v"].astype(int), strict=True))


def active_sales(tables: TableStore, scope: str) -> pd.DataFrame:
    """The analytical view: every occurrence in the active snapshot of a scope."""
    snapshot = active_snapshot(tables, scope)
    if snapshot is None:
        return pd.DataFrame()
    m = tables.qualify("silver.snapshot_membership")
    r = tables.qualify("bronze.ppr_rows")
    return tables.query(
        f"SELECT m.snapshot_id, m.sale_id, m.sale_version, m.match_type, r.* "
        f"FROM {m} m JOIN {r} r ON m.raw_row_id = r.raw_row_id "
        f"WHERE m.snapshot_id = {_lit(snapshot)}")


# ---------------------------------------------------------------------------
# Promotion policy and orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionPolicy:
    authoritative_scope: str = "ppr:national"
    max_record_drop: float = 0.005       # hold if the record count falls by more than this
    min_retained_share: float = 0.98     # hold if fewer previous occurrences are carried over


DEFAULT_POLICY = PromotionPolicy()


def _event(scope: str, snapshot_id: str, decision: SnapshotDecision, previous: str | None,
           reason: str, records: int, run: RunContext) -> pd.DataFrame:
    event_id = sha256_hex(f"{scope}|{snapshot_id}|{decision}|{previous}".encode())[:32]
    return pd.DataFrame([(event_id, scope, snapshot_id, decision.value, previous, reason,
                          records, run.as_of, run.run_id)],
                        columns=TABLE_SCHEMAS["bronze.snapshot_events"].names)


def land_snapshot(
    fetched: FetchResult,
    *,
    source: str,
    scope: str,
    expected_members: Iterable[str],
    contracts: Iterable[SourceContract],
    objects: ObjectStore,
    tables: TableStore,
    run: RunContext,
    policy: PromotionPolicy = DEFAULT_POLICY,
    force_promote: bool = False,
    county_filter: str | None = None,
) -> StageResult:
    """Land one acquisition and decide whether its snapshot becomes active for its scope.

    With county_filter, the whole file is still stored untouched and validated, but only
    that county's records enter bronze. Row IDs keep their ordinal in the full file, so
    every row traces back to the original download.
    """
    contracts = tuple(contracts)
    acquisition_id = (f"{source}-{fetched.retrieved_utc:%Y%m%dT%H%M%SZ}-"
                      f"{sha256_hex(fetched.body)[:12]}")
    manifest_rows: list[dict] = []
    events: list[QualityEvent] = []
    counts: dict[str, int] = {}
    outputs: dict[str, str] = {"acquisition_id": acquisition_id}

    def manifest(member: str, **values) -> None:
        row = dict.fromkeys(TABLE_SCHEMAS["bronze.source_manifest"].names)
        row.update(acquisition_id=acquisition_id, member_name=member, source=source,
                   scope=scope, url=fetched.url, retrieved_utc=fetched.retrieved_utc,
                   http_status=fetched.status,
                   http_last_modified=fetched.headers.get("last-modified"),
                   http_etag=fetched.headers.get("etag"), archive_bytes=len(fetched.body),
                   run_id=run.run_id, code_hash=run.code_hash, **values)
        manifest_rows.append(row)

    def finish(status: StageStatus) -> StageResult:
        tables.insert_new("bronze.source_manifest", pd.DataFrame(
            manifest_rows, columns=TABLE_SCHEMAS["bronze.source_manifest"].names))
        return StageResult("land_snapshot", run.run_id, status, outputs, counts, tuple(events))

    try:
        members = validate_archive(fetched, expected_members)
    except AcquisitionError as exc:
        manifest("", status=SnapshotDecision.REJECTED.value, status_reason=str(exc))
        events.append(QualityEvent("ACQUISITION_VALID", Severity.BLOCKING, str(exc)))
        outputs["decision"] = SnapshotDecision.REJECTED.value
        return finish(StageStatus.FAILED)

    archive_sha, _, _ = objects.put(fetched.body, ".zip")
    member_shas = {name: objects.put(data, Path(name).suffix.lower())[0]
                   for name, data in members.items()}
    # Download facts only: the same download may serve several scopes (national, a county).
    objects.put_record(f"acquisitions/{source}/{acquisition_id}.json", {
        "acquisition_id": acquisition_id, "source": source,
        "url": fetched.url, "retrieved_utc": fetched.retrieved_utc.isoformat(),
        "http_status": fetched.status, "headers": fetched.headers,
        "archive": {"sha256": archive_sha, "bytes": len(fetched.body)},
        "members": [{"name": n, "sha256": s, "bytes": len(members[n])}
                    for n, s in sorted(member_shas.items())],
    }, tolerate_extra=("scope",))  # records written before profiles also held the scope

    status = StageStatus.SUCCEEDED
    for name, data in sorted(members.items()):
        member_sha = member_shas[name]
        snapshot_id = f"{scope}:{member_sha[:16]}"
        common = dict(archive_sha256=archive_sha, member_sha256=member_sha,
                      member_bytes=len(data), snapshot_id=snapshot_id)
        outputs["snapshot_id"] = snapshot_id

        try:
            parsed = parse_member(data, member_sha, contracts)
        except (ContractError, UnicodeDecodeError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            manifest(name, **common, status=SnapshotDecision.REJECTED.value, status_reason=reason)
            events.append(QualityEvent("SOURCE_CONTRACT", Severity.BLOCKING, reason))
            outputs["decision"] = SnapshotDecision.REJECTED.value
            status = StageStatus.FAILED
            continue

        contract_info = dict(contract_id=parsed.contract.contract_id,
                             contract_version=parsed.contract.version,
                             encoding=parsed.contract.encoding, record_count=parsed.record_count)
        counts["records"] = parsed.record_count
        if parsed.structural_problems:
            reason = "; ".join(parsed.structural_problems)
            manifest(name, **common, **contract_info,
                     status=SnapshotDecision.REJECTED.value, status_reason=reason)
            tables.insert_new("bronze.snapshot_events", _event(
                scope, snapshot_id, SnapshotDecision.REJECTED, active_snapshot(tables, scope),
                reason, parsed.record_count, run))
            events.append(QualityEvent("SNAPSHOT_STRUCTURE", Severity.BLOCKING, reason))
            outputs["decision"] = SnapshotDecision.REJECTED.value
            status = StageStatus.FAILED
            continue

        if county_filter:
            parsed = filter_county(parsed, county_filter)
            counts["records_in_scope"] = parsed.record_count
            contract_info["record_count"] = parsed.record_count
        counts["rows_inserted"] = tables.insert_new("bronze.ppr_rows", parsed.rows)
        decision, reason = _decide_and_reconcile(
            snapshot_id, scope, parsed, tables, run, policy, force_promote, counts)
        if county_filter:
            reason = f"{county_filter} only ({parsed.record_count} records); {reason}"
        manifest(name, **common, **contract_info, status=decision.value, status_reason=reason)
        outputs["decision"] = decision.value
        if decision is SnapshotDecision.HELD:
            events.append(QualityEvent("SNAPSHOT_RECONCILIATION", Severity.BLOCKING, reason))
            status = StageStatus.FAILED

    return finish(status)


def _decide_and_reconcile(snapshot_id: str, scope: str, parsed: ParsedMember,
                          tables: TableStore, run: RunContext, policy: PromotionPolicy,
                          force_promote: bool, counts: dict[str, int]
                          ) -> tuple[SnapshotDecision, str]:
    if scope != policy.authoritative_scope:
        parent = active_snapshot(tables, policy.authoritative_scope)
        authoritative = None if parent is None else snapshot_occurrences(tables, parent)
        membership, stats = match_reference(snapshot_id, parsed.rows, authoritative)
        counts.update(stats)
        counts["membership_inserted"] = tables.insert_new("silver.snapshot_membership", membership)
        reason = (f"cross-check against {parent}: {stats['reference_matched']} matched, "
                  f"{stats['reference_unmatched']} unmatched")
        tables.insert_new("bronze.snapshot_events", _event(
            scope, snapshot_id, SnapshotDecision.REFERENCE, parent, reason,
            parsed.record_count, run))
        return SnapshotDecision.REFERENCE, reason

    current = active_snapshot(tables, scope)
    if current == snapshot_id:
        return SnapshotDecision.ALREADY_ACTIVE, "identical content to the active snapshot"

    previous = None if current is None else snapshot_occurrences(tables, current)
    result = reconcile(snapshot_id, parsed.rows, previous,
                       max_sale_versions(tables, excluding_snapshot=snapshot_id))
    counts.update(result.stats)

    reason = (f"{result.stats.get('exact', 0)} exact, {result.stats.get('corrections', 0)} "
              f"corrections, {result.stats.get('new_sales', 0)} new, "
              f"{result.stats.get('absent_from_new', 0)} absent, "
              f"{result.stats.get('reviews', 0)} for review")
    if previous is not None and not force_promote:
        prev_n = len(previous)
        retained = result.stats.get("exact", 0) + result.stats.get("corrections", 0)
        problems = []
        if parsed.record_count < prev_n * (1 - policy.max_record_drop):
            problems.append(f"record count fell from {prev_n} to {parsed.record_count}")
        if prev_n and retained / prev_n < policy.min_retained_share:
            problems.append(f"only {retained / prev_n:.1%} of previous occurrences retained")
        if problems:
            held_reason = "; ".join(problems) + f" ({reason})"
            tables.insert_new("bronze.snapshot_events", _event(
                scope, snapshot_id, SnapshotDecision.HELD, current, held_reason,
                parsed.record_count, run))
            return SnapshotDecision.HELD, held_reason

    # Versions first, then membership, then the promotion event that makes it visible.
    tables.insert_new("silver.sale_versions", result.sale_versions)
    counts["membership_inserted"] = tables.insert_new(
        "silver.snapshot_membership", result.membership)
    tables.insert_new("silver.reconciliation_reviews", result.reviews)
    if force_promote:
        reason = "manually approved; " + reason
    tables.insert_new("bronze.snapshot_events", _event(
        scope, snapshot_id, SnapshotDecision.PROMOTED, current, reason,
        parsed.record_count, run))
    return SnapshotDecision.PROMOTED, reason


# ---------------------------------------------------------------------------
# Rebuild check: rebuild from saved bytes
# ---------------------------------------------------------------------------


def filter_county(parsed: ParsedMember, county: str) -> ParsedMember:
    """Keep one county's records; structural checks have already run on the whole file."""
    from dataclasses import replace

    rows = parsed.rows[parsed.rows.county_raw == county].reset_index(drop=True)
    return replace(parsed, rows=rows, record_count=len(rows))


def verify_rebuild(objects: ObjectStore, tables: TableStore, snapshot_id: str,
                   contracts: Iterable[SourceContract],
                   county_filter: str | None = None) -> dict[str, object]:
    """Re-parse a snapshot from stored bytes and compare with the bronze rows and manifest."""
    manifest = tables.query(
        f"SELECT DISTINCT member_sha256, member_name, record_count "
        f"FROM {tables.qualify('bronze.source_manifest')} "
        f"WHERE snapshot_id = {_lit(snapshot_id)}")
    if len(manifest) != 1:
        raise ValueError(f"expected one member for {snapshot_id}, found {len(manifest)}")
    member_sha, member_name, record_count = manifest.iloc[0]
    data = objects.get(member_sha, Path(member_name).suffix.lower())
    parsed = parse_member(data, member_sha, contracts)
    rebuilt = (filter_county(parsed, county_filter) if county_filter else parsed).rows
    stored = tables.query(
        f"SELECT raw_row_id, raw_content_fp, txn_fp FROM {tables.qualify('bronze.ppr_rows')} "
        f"WHERE member_sha256 = {_lit(member_sha)}")
    members = tables.query(
        f"SELECT count(*) AS n FROM {tables.qualify('silver.snapshot_membership')} "
        f"WHERE snapshot_id = {_lit(snapshot_id)}")
    rebuilt_set = set(zip(rebuilt.raw_row_id, rebuilt.raw_content_fp, rebuilt.txn_fp,
                          strict=True))
    stored_set = set(zip(stored.raw_row_id, stored.raw_content_fp, stored.txn_fp, strict=True))
    return {
        "snapshot_id": snapshot_id,
        "member_sha256_verified": True,
        "manifest_record_count": int(record_count),
        "rebuilt_rows": len(rebuilt),
        "stored_rows": len(stored),
        "membership_rows": int(members.iloc[0, 0]),
        "rows_identical": rebuilt_set == stored_set,
    }


def code_hash(package_dir: str | Path | None = None) -> str:
    """SHA-256 over the package source files, for run provenance."""
    root = Path(package_dir or Path(__file__).parent)
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
