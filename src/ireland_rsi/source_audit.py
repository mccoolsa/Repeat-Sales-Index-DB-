"""Source reconnaissance: profile a landed PPR snapshot against its contract.

Reads the archive in place (never extracts or rewrites it), verifies checksums against
the acquisition manifest, and writes a JSON profile used by docs/source_audit.md.

    python -m ireland_rsi.source_audit data/raw/ppr/2026-10-06 --out reports/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from ireland_rsi.contracts import ContractError, SourceContract, load_contracts, select_contract

# Routing key (letter + 2 digits, or D6W) then a 4-character unique identifier.
EIRCODE_STRICT = re.compile(r"^(?:[ACDEFHKNPRTVWXY]\d{2}|D6W) ?[0-9ACDEFHKNPRTVWXY]{4}$")
PRICE_FORMAT = re.compile(r"^€\d{1,3}(?:,\d{3})*\.\d{2}$")
LEADING_NUMBER = re.compile(r"\d+[a-z]?")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm_address(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _price_band(cents: int) -> str:
    euros = cents / 100
    if euros < 10_000:
        return "<10k"
    if euros < 50_000:
        return "10k-50k"
    if euros < 10_000_000:
        return "50k-10m"
    return ">=10m"


def profile_bytes(raw: bytes, contracts: tuple[SourceContract, ...]) -> dict:
    """Profile one CSV member. Raises on undecodable bytes or an unknown header."""
    newline_lf = raw.count(b"\n")
    newline_crlf = raw.count(b"\r\n")
    non_ascii = Counter(b for b in raw if b > 127)

    # Contract selection needs the decoded header; try each declared encoding strictly.
    header_line = raw.split(b"\r\n", 1)[0] if newline_crlf else raw.split(b"\n", 1)[0]
    ordered = sorted(contracts, key=lambda c: c.version, reverse=True)
    for encoding in dict.fromkeys(c.encoding for c in ordered):
        try:
            header = next(csv.reader([header_line.decode(encoding)]))
        except UnicodeDecodeError:
            continue
        contract, header_check = select_contract(
            header, [c for c in ordered if c.encoding == encoding]
        )
        break
    else:
        raise ContractError("header is not decodable under any contract encoding")

    text = raw.decode(contract.encoding)  # strict: undecodable bytes are a failure
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=contract.delimiter, quotechar=contract.quotechar
    )
    header = next(reader)
    rows = list(reader)
    col = {f.name: header.index(f.header) for f in contract.fields if f.header in header}

    def get(row: list[str], name: str) -> str:
        return row[col[name]] if name in col else ""

    field_counts = Counter(len(r) for r in rows)
    embedded_newlines = sum(1 for r in rows for v in r if "\n" in v or "\r" in v)
    edge_whitespace = Counter(
        name for r in rows for name in col if get(r, name) != get(r, name).strip()
    )

    dates: list[date | None] = []
    for r in rows:
        try:
            dates.append(datetime.strptime(get(r, "sale_date_raw"), contract.date_format).date())
        except ValueError:
            dates.append(None)
    valid_dates = [d for d in dates if d]
    latest_year = max(valid_dates).year if valid_dates else None

    prices_cents = []
    price_bad = 0
    for r in rows:
        p = get(r, "price_raw")
        if PRICE_FORMAT.match(p):
            prices_cents.append(int(p[1:].replace(",", "").replace(".", "")))
        else:
            price_bad += 1
    price_bands = Counter(_price_band(c) for c in prices_cents)

    # Eircode coverage and consistency.
    eircodes = [get(r, "eircode_raw").strip().upper() for r in rows]
    by_year = defaultdict(lambda: [0, 0])
    by_county_recent = defaultdict(lambda: [0, 0])
    for d, r, e in zip(dates, rows, eircodes, strict=True):
        if d:
            by_year[d.year][0] += 1
            by_year[d.year][1] += bool(e)
            if d.year >= 2022:
                by_county_recent[get(r, "county_raw")][0] += 1
                by_county_recent[get(r, "county_raw")][1] += bool(e)

    code_rows: dict[str, list[int]] = defaultdict(list)
    for i, e in enumerate(eircodes):
        if e:
            code_rows[e.replace(" ", "")].append(i)
    repeated = {k: v for k, v in code_rows.items() if len(v) > 1}
    multi_county = sum(
        1 for v in repeated.values() if len({get(rows[i], "county_raw") for i in v}) > 1
    )
    differing_address = 0
    differing_number = 0
    for v in repeated.values():
        addresses = {_norm_address(get(rows[i], "address_raw")) for i in v}
        if len(addresses) > 1:
            differing_address += 1
            numbers = {(LEADING_NUMBER.findall(a) or [""])[0] for a in addresses}
            if len(numbers) > 1:
                differing_number += 1

    occurrences = Counter(tuple(r) for r in rows)
    question_mark_irish = sum(
        1 for r in rows if "?" in get(r, "description_raw") + get(r, "size_description_raw")
    )

    return {
        "contract": {"contract_id": contract.contract_id, "version": contract.version,
                     "fingerprint": contract.fingerprint,
                     "missing_optional": list(header_check.missing_optional)},
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
        "utf8_bom": raw.startswith(b"\xef\xbb\xbf"),
        "line_endings": {"crlf": newline_crlf, "bare_lf": newline_lf - newline_crlf},
        "non_ascii_bytes": {f"0x{k:02X}": v for k, v in sorted(non_ascii.items())},
        "header": header,
        "records": len(rows),
        "field_count_distribution": {str(k): v for k, v in sorted(field_counts.items())},
        "fields_with_embedded_newlines": embedded_newlines,
        "fields_with_edge_whitespace": dict(edge_whitespace),
        "dates": {
            "unparseable": sum(1 for d in dates if d is None),
            "min": str(min(valid_dates)) if valid_dates else None,
            "max": str(max(valid_dates)) if valid_dates else None,
            "by_year": dict(sorted(Counter(d.year for d in valid_dates).items())),
            "latest_year_by_month": dict(sorted(
                Counter(d.month for d in valid_dates if d.year == latest_year).items())),
        },
        "counties": dict(sorted(Counter(get(r, "county_raw") for r in rows).items())),
        "price": {
            "nonconforming_format": price_bad,
            "min_eur": min(prices_cents) / 100 if prices_cents else None,
            "max_eur": max(prices_cents) / 100 if prices_cents else None,
            "bands": dict(price_bands),
        },
        "flags": {
            name: dict(Counter(get(r, name) for r in rows).most_common())
            for name in ("not_full_market_price_raw", "vat_exclusive_raw",
                         "description_raw", "size_description_raw")
        },
        "vat_exclusive_by_description": {
            f"{k[0]} | VAT exclusive={k[1]}": v for k, v in sorted(Counter(
                (get(r, "description_raw"), get(r, "vat_exclusive_raw")) for r in rows).items())
        },
        "eircode": {
            "populated": sum(1 for e in eircodes if e),
            "strict_valid": sum(1 for e in eircodes if e and EIRCODE_STRICT.match(e)),
            "with_space": sum(1 for e in eircodes if " " in e),
            "pct_populated_by_year": {
                y: round(100 * n_e / n, 1) for y, (n, n_e) in sorted(by_year.items())},
            "pct_populated_by_county_2022_on": {
                c: round(100 * n_e / n, 1) for c, (n, n_e) in sorted(by_county_recent.items())},
            "distinct_codes": len(code_rows),
            "codes_with_multiple_sales": len(repeated),
            "rows_in_repeated_codes": sum(len(v) for v in repeated.values()),
            "repeated_codes_spanning_counties": multi_county,
            "repeated_codes_with_differing_address_text": differing_address,
            "repeated_codes_with_differing_leading_number": differing_number,
        },
        "exact_duplicate_rows": {
            "extra_occurrences": sum(n - 1 for n in occurrences.values() if n > 1),
            "groups": sum(1 for n in occurrences.values() if n > 1),
            "max_multiplicity": max(occurrences.values()) if occurrences else 0,
        },
        "rows_with_question_mark_in_irish_text": question_mark_irish,
    }


def audit_snapshot(snapshot_dir: Path, contract_path: Path) -> dict:
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    archive_path = snapshot_dir / manifest["archive"]["path"]
    archive_bytes = archive_path.read_bytes()
    if sha256_bytes(archive_bytes) != manifest["archive"]["sha256"]:
        raise ValueError(f"archive checksum mismatch for {archive_path}")

    contracts = load_contracts(contract_path)
    members = []
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        for member in manifest["members"]:
            raw = zf.read(member["name"])
            if sha256_bytes(raw) != member["sha256"]:
                raise ValueError(f"member checksum mismatch for {member['name']}")
            members.append({"name": member["name"], **profile_bytes(raw, contracts)})
    return {"acquisition_id": manifest["acquisition_id"], "manifest": manifest,
            "members": members}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot_dir", type=Path)
    parser.add_argument("--contract", type=Path, default=Path("config/source_contract.yml"))
    parser.add_argument("--out", type=Path, default=Path("reports"))
    args = parser.parse_args()

    result = audit_snapshot(args.snapshot_dir, args.contract)
    args.out.mkdir(parents=True, exist_ok=True)
    out_file = args.out / f"source_audit_{result['acquisition_id']}.json"
    out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out_file}")


if __name__ == "__main__":
    main()
