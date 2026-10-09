"""Defensive parsing and comparability rules.

Source fields arrive as strings from bronze. Each named transformation below turns one
field into a typed value or a rule failure; clean_sales applies them to a snapshot and
assigns every occurrence exactly one primary disposition:

    quarantine      could not be parsed (kept in silver.quarantine with reasons)
    excluded        parsed, but not a comparable market price (non-market, VAT unresolved)
    pending_review  parsed, but flagged (extreme price, bulk or multi-unit filing)
    eligible        parsed, market price on a documented basis

All rule hits, not just the primary one, are written to silver.quality_events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ireland_rsi.contracts import TABLE_SCHEMAS, RunContext, StageResult, StageStatus
from ireland_rsi.storage import TableStore, sha256_hex

DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}$")
PRICE_PATTERN = re.compile(r"^€\d{1,3}(?:,\d{3})*\.\d{2}$")
EIRCODE_PATTERN = re.compile(r"^(?:[ACDEFHKNPRTVWXY]\d{2}|D6W)[0-9ACDEFHKNPRTVWXY]{4}$")
HTML_ENTITY = re.compile(r"&[a-z]+;", re.IGNORECASE)
DISPOSITION_ORDER = ("quarantine", "excluded", "pending_review")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleConfig:
    quality: dict
    vat: dict
    version: str

    @classmethod
    def load(cls, quality_path: str | Path, vat_path: str | Path) -> RuleConfig:
        quality_bytes = Path(quality_path).read_bytes()
        vat_bytes = Path(vat_path).read_bytes()
        quality = yaml.safe_load(quality_bytes.decode("utf-8"))
        vat = yaml.safe_load(vat_bytes.decode("utf-8"))
        digest = sha256_hex(quality_bytes + vat_bytes + Path(__file__).read_bytes())[:10]
        return cls(quality, vat, f"{quality['rule_set']}+{digest}")

    def rule(self, rule_id: str) -> dict:
        return self.quality["rules"][rule_id]


# ---------------------------------------------------------------------------
# Named transformations (one value in, typed value or None out)
# ---------------------------------------------------------------------------


def parse_sale_date(raw: str | None) -> date | None:
    """Strict dd/mm/yyyy. Impossible dates (31/02, 29/02 in a non-leap year) are None."""
    if raw is None or not DATE_PATTERN.match(raw):
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_price_eur(raw: str | None) -> tuple[int | None, str | None]:
    """'€343,000.00' -> (34300000, None). Returns (None, rule_id) on failure.

    Money is integer cents; binary floats are never used for stored prices.
    """
    if raw is None or raw.strip() == "":
        return None, "PRICE_MISSING"
    if not PRICE_PATTERN.match(raw):
        return None, "PRICE_MALFORMED"
    cents = int(raw[1:].replace(",", "").replace(".", ""))
    if cents <= 0:
        return None, "PRICE_NOT_POSITIVE"
    return cents, None


def _parse_flag(raw: str | None, flags: dict) -> bool | None:
    return flags.get(raw) if raw is not None else None


def parse_market_flag(raw: str | None, flags: dict) -> bool | None:
    """'Not Full Market Price' column: True means the price is NOT a full market price."""
    return _parse_flag(raw, flags)


def parse_vat_flag(raw: str | None, flags: dict) -> bool | None:
    """'VAT Exclusive' column: True means the reported price excludes VAT."""
    return _parse_flag(raw, flags)


def classify_source_description(raw: str | None, descriptions: dict) -> dict | None:
    """Map the source description to {'status': new|second_hand, 'language': en|ga}.

    The source does not distinguish houses from apartments, so neither does this.
    """
    return descriptions.get(raw) if raw is not None else None


@dataclass(frozen=True)
class VatResult:
    rule_id: str | None
    rate: str | None
    gross_cents: int | None

    @property
    def resolved(self) -> bool:
        return self.gross_cents is not None


def apply_vat_rule(net_cents: int, sale_date: date, vat_config: dict,
                   qualifying_apartment: bool | None = None) -> VatResult:
    """Gross up a VAT-exclusive new-dwelling price under the effective-dated rule.

    qualifying_apartment is external evidence about the 9% regime; the register itself
    never supplies it, so post-8-October-2025 sales are unresolved by default.
    """
    for rule in vat_config["rules"]:
        start, end = rule["from"], rule["to"]
        if not (start <= sale_date <= end):
            continue
        if "rate" in rule:
            rate = rule["rate"]
        elif qualifying_apartment is True:
            rate = rule["rate_if_qualifying"]
        elif qualifying_apartment is False:
            rate = rule["rate_otherwise"]
        else:
            return VatResult(rule["id"], None, None)
        gross = (Decimal(net_cents) * (1 + Decimal(rate))).quantize(
            Decimal(1), rounding=ROUND_HALF_UP)
        return VatResult(rule["id"], rate, int(gross))
    return VatResult(None, None, None)


def development_key(address: str) -> str:
    """Address text without numbers or unit words, used only to group bulk filings."""
    text = re.sub(r"\b(apt|apartment|unit|no|number|flat|house)\b\.?", " ", address.lower())
    return " ".join(re.sub(r"[^a-z]+", " ", text).split())


# ---------------------------------------------------------------------------
# Snapshot cleaning
# ---------------------------------------------------------------------------


@dataclass
class CleanResult:
    sales: pd.DataFrame
    events: pd.DataFrame
    quarantine: pd.DataFrame
    summary: dict


def _map_unique(series: pd.Series, func) -> pd.Series:
    """Apply a scalar transformation once per distinct value (dates, prices repeat a lot)."""
    uniques = series.drop_duplicates()
    lookup = dict(zip(uniques, (func(v) for v in uniques), strict=True))
    return series.map(lookup)


def clean_sales(occurrences: pd.DataFrame, rules: RuleConfig, run: RunContext,
                source_cutoff: date) -> CleanResult:
    """Parse one snapshot's occurrences and assign dispositions and eligibility."""
    q = rules.quality
    df = occurrences.reset_index(drop=True).copy()
    n = len(df)
    hits: dict[str, pd.Series] = {}
    detail_before: dict[str, pd.Series] = {}
    detail_after: dict[str, pd.Series] = {}

    def hit(rule_id: str, mask, before=None, after=None) -> None:
        rules.rule(rule_id)  # unknown rule ids are a configuration error
        mask = pd.Series(mask, index=df.index).fillna(False).astype(bool)
        hits[rule_id] = mask
        if before is not None:
            detail_before[rule_id] = before
        if after is not None:
            detail_after[rule_id] = after

    # --- parsing (blocking) ---
    df["sale_date"] = _map_unique(df.sale_date_raw, parse_sale_date)
    first = q["scope"]["first_sale_date"]
    has_date = df.sale_date.notna()
    hit("DATE_INVALID", ~has_date, before=df.sale_date_raw)
    hit("DATE_BEFORE_SCOPE", has_date & (df.sale_date.where(has_date, first) < first),
        before=df.sale_date_raw)
    hit("DATE_AFTER_CUTOFF", has_date & (df.sale_date.where(has_date, first) > source_cutoff),
        before=df.sale_date_raw)

    parsed_price = _map_unique(df.price_raw, parse_price_eur)
    df["reported_price_cents"] = pd.array(parsed_price.map(lambda p: p[0]).tolist(),
                                          dtype="Int64")
    price_fail = parsed_price.map(lambda p: p[1])
    for rule_id in ("PRICE_MISSING", "PRICE_MALFORMED", "PRICE_NOT_POSITIVE"):
        hit(rule_id, price_fail == rule_id, before=df.price_raw)

    hit("COUNTY_UNKNOWN", ~df.county_raw.isin(q["counties"]), before=df.county_raw)
    hit("ADDRESS_MISSING", df.address_raw.fillna("").str.strip() == "")

    df["not_full_market_price"] = _map_unique(
        df.not_full_market_price_raw, lambda v: parse_market_flag(v, q["flags"]))
    df["vat_exclusive"] = _map_unique(df.vat_exclusive_raw,
                                      lambda v: parse_vat_flag(v, q["flags"]))
    hit("MARKET_FLAG_UNKNOWN", df.not_full_market_price.isna(),
        before=df.not_full_market_price_raw)
    hit("VAT_FLAG_UNKNOWN", df.vat_exclusive.isna(), before=df.vat_exclusive_raw)

    described = _map_unique(df.description_raw,
                            lambda v: classify_source_description(v, q["descriptions"]))
    df["dwelling_status"] = described.map(lambda d: d["status"] if d else None)
    df["description_language"] = described.map(lambda d: d["language"] if d else None)
    hit("DESCRIPTION_UNKNOWN", df.dwelling_status.isna(), before=df.description_raw)

    blocking = [r for r, spec in q["rules"].items() if spec["effect"] == "quarantine"]
    quarantined = pd.concat([hits[r] for r in blocking if r in hits], axis=1).any(axis=1)
    parsed = ~quarantined

    # --- comparability (parsed rows only) ---
    is_new = df.dwelling_status == "new"
    vat_excl = df.vat_exclusive == True  # noqa: E712 - object column with None
    hit("NON_MARKET_SALE", parsed & (df.not_full_market_price == True))  # noqa: E712
    hit("VAT_FLAG_CONFLICT", parsed & vat_excl & (df.dwelling_status == "second_hand"))
    hit("NEW_PRICE_BASIS_UNCERTAIN", parsed & is_new & ~vat_excl)

    df["vat_rule_id"] = None
    df["vat_rate"] = None
    df["gross_price_cents"] = pd.array([None] * n, dtype="Int64")
    to_gross = parsed & is_new & vat_excl
    if to_gross.any():
        keys = df.loc[to_gross, ["reported_price_cents", "sale_date"]]
        results = {
            k: apply_vat_rule(int(k[0]), k[1], rules.vat)
            for k in keys.drop_duplicates().itertuples(index=False, name=None)
        }
        vat = [results[k] for k in keys.itertuples(index=False, name=None)]
        df.loc[to_gross, "vat_rule_id"] = [v.rule_id for v in vat]
        df.loc[to_gross, "vat_rate"] = [v.rate for v in vat]
        df.loc[to_gross, "gross_price_cents"] = pd.array(
            [v.gross_cents for v in vat], dtype="Int64")
    resolved = to_gross & df.gross_price_cents.notna()
    hit("VAT_RULE_UNRESOLVED", to_gross & ~resolved)
    hit("VAT_ADJUSTED", resolved, before=df.reported_price_cents.astype(str),
        after=df.gross_price_cents.astype(str))

    bounds = q["price_review_bounds_eur"]
    hit("PRICE_REVIEW_LOW", parsed & (df.reported_price_cents < bounds["low"] * 100),
        before=df.price_raw)
    hit("PRICE_REVIEW_HIGH", parsed & (df.reported_price_cents > bounds["high"] * 100),
        before=df.price_raw)

    bulk = q["bulk_filing"]
    df["_dev"] = _map_unique(df.address_raw.fillna(""), development_key)
    base = ["sale_date_raw", "county_raw", "price_raw"]
    same_dev = df.groupby([*base, "_dev"], dropna=False).raw_row_id.transform("size")
    same_day = df.groupby(base, dropna=False).raw_row_id.transform("size")
    is_bulk = (same_dev >= bulk["same_development_min_rows"]) | (
        same_day >= bulk["same_day_price_min_rows"])
    hit("POSSIBLE_BULK_FILING", parsed & is_bulk,
        after="same_development=" + same_dev.astype(str) + ";same_day=" + same_day.astype(str))

    multi = q["multi_unit_address"]
    lower = df.address_raw.fillna("").str.lower()
    is_multi = lower.str.contains(multi["range_pattern"], regex=True) & ~lower.str.contains(
        multi["unit_prefix_pattern"], regex=True)
    hit("MULTI_UNIT_ADDRESS", parsed & is_multi, before=df.address_raw)

    eircode = df.eircode_raw.fillna("").str.replace(" ", "", regex=False).str.upper()
    df["eircode"] = eircode.where(eircode != "", None)
    df["eircode_valid"] = eircode.str.match(EIRCODE_PATTERN.pattern) & (eircode != "")
    hit("EIRCODE_INVALID", parsed & (eircode != "") & ~df.eircode_valid, before=df.eircode_raw)
    syntactic = df.eircode_valid & parsed
    counties_per_code = df[syntactic].groupby("eircode").county_raw.transform("nunique")
    placeholder = syntactic & (
        df.eircode.isin(q["eircode_placeholders"])
        | (counties_per_code.reindex(df.index, fill_value=0) > q["eircode_max_counties"]))
    hit("EIRCODE_PLACEHOLDER", placeholder, before=df.eircode_raw)
    df["eircode_valid"] = df.eircode_valid & ~placeholder

    size_map = q["size_bands"]
    size_raw = df.size_description_raw.fillna("")
    df["size_band"] = size_raw.map(lambda v: size_map.get(v))
    hit("SIZE_UNKNOWN", parsed & ~size_raw.isin(list(size_map)), before=df.size_description_raw)
    hit("HTML_ENTITY_IN_ADDRESS", parsed & df.address_raw.fillna("").str.contains(HTML_ENTITY))

    df["duplicate_group_size"] = df.groupby("raw_content_fp").raw_row_id.transform("size")
    hit("DUPLICATE_OCCURRENCE", parsed & (df.duplicate_group_size > 1))

    # --- dispositions ---
    effects = {r: rules.rule(r)["effect"] for r in hits}
    disposition = pd.Series("eligible", index=df.index, dtype=object)
    for level in reversed(DISPOSITION_ORDER):  # highest precedence written last
        level_rules = [r for r, e in effects.items() if e == level]
        if level_rules:
            mask = pd.concat([hits[r] for r in level_rules], axis=1).any(axis=1)
            disposition[mask] = level
    df["disposition"] = disposition

    codes = np.full(n, "", dtype=object)
    for rule_id in sorted(hits):
        mask = hits[rule_id].to_numpy()
        codes = np.where(mask, np.where(codes == "", rule_id, codes + "," + rule_id), codes)
    df["reason_codes"] = codes

    second_hand_basis = parsed & (df.dwelling_status == "second_hand") & ~vat_excl
    df["price_basis"] = None
    df.loc[parsed, "price_basis"] = "uncertain"
    df.loc[second_hand_basis, "price_basis"] = "reported"
    df.loc[resolved, "price_basis"] = "gross_from_vat_exclusive"
    df["analysis_price_cents"] = pd.array([None] * n, dtype="Int64")
    df.loc[second_hand_basis, "analysis_price_cents"] = df.loc[
        second_hand_basis, "reported_price_cents"]
    df.loc[resolved, "analysis_price_cents"] = df.loc[resolved, "gross_price_cents"]

    df["parse_valid"] = parsed
    df["linkage_eligible"] = parsed & ~hits["MULTI_UNIT_ADDRESS"]
    df["index_eligible"] = (df.disposition == "eligible") & df.analysis_price_cents.notna()
    df["sale_quarter"] = df.sale_date.map(
        lambda d: f"{d.year}Q{(d.month - 1) // 3 + 1}" if isinstance(d, date) else None)
    df["county"] = df.county_raw
    df["rule_set_version"] = rules.version
    df["run_id"] = run.run_id

    sales_cols = TABLE_SCHEMAS["silver.sales"].names
    sales = df.loc[parsed, sales_cols].reset_index(drop=True)
    quarantine = df.loc[quarantined].assign(resolution_state="open")[
        TABLE_SCHEMAS["silver.quarantine"].names].reset_index(drop=True)

    event_frames = []
    for rule_id, mask in hits.items():
        if not mask.any():
            continue
        spec = rules.rule(rule_id)
        before = detail_before.get(rule_id)
        after = detail_after.get(rule_id)
        event_frames.append(pd.DataFrame({
            "snapshot_id": df.snapshot_id[mask], "raw_row_id": df.raw_row_id[mask],
            "rule_set_version": rules.version, "rule_id": rule_id,
            "sale_id": df.sale_id[mask], "severity": spec["severity"],
            "effect": spec["effect"],
            "value_before": None if before is None else pd.Series(before)[mask].astype(str),
            "value_after": None if after is None else pd.Series(after, index=df.index)[mask],
            "run_id": run.run_id,
        }))
    events = (pd.concat(event_frames, ignore_index=True) if event_frames else
              pd.DataFrame(columns=TABLE_SCHEMAS["silver.quality_events"].names))
    events = events[TABLE_SCHEMAS["silver.quality_events"].names]

    summary = summarise(df, hits, quarantined)
    return CleanResult(sales, events, quarantine, summary)


def summarise(df: pd.DataFrame, hits: dict[str, pd.Series], quarantined: pd.Series) -> dict:
    """Counts by primary disposition and by every reason code, with partition checks."""
    total = len(df)
    parsed = int((~quarantined).sum())
    by_disposition = df.disposition.value_counts().to_dict()
    checks = {
        "total_equals_parsed_plus_quarantined": total == parsed + int(quarantined.sum()),
        "parsed_partitions_into_dispositions": parsed == sum(
            v for k, v in by_disposition.items() if k != "quarantine"),
        "every_quarantined_row_has_a_blocking_reason": bool(
            (df.loc[quarantined, "reason_codes"] != "").all()),
    }
    return {
        "total_occurrences": total,
        "parsed": parsed,
        "quarantined": int(quarantined.sum()),
        "by_disposition": {k: int(v) for k, v in sorted(by_disposition.items())},
        "by_reason": {r: int(m.sum()) for r, m in sorted(hits.items()) if m.any()},
        "index_eligible": int(df.index_eligible.sum()),
        "index_eligible_by_status": {k: int(v) for k, v in df.loc[
            df.index_eligible, "dwelling_status"].value_counts().items()},
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def snapshot_cutoff(tables: TableStore, snapshot_id: str) -> date:
    """Latest valid sale date: the first time this snapshot's bytes were acquired."""
    df = tables.query(
        f"SELECT min(retrieved_utc) AS t FROM {tables.qualify('bronze.source_manifest')} "
        f"WHERE snapshot_id = '{snapshot_id}' AND status IN ('promoted', 'already_active')")
    value = pd.Timestamp(df.iloc[0, 0])
    return value.date()


def run_clean(tables: TableStore, scope: str, rules: RuleConfig,
              run: RunContext) -> tuple[StageResult, CleanResult]:
    from ireland_rsi.ingest import active_sales, active_snapshot

    snapshot = active_snapshot(tables, scope)
    if snapshot is None:
        raise RuntimeError(f"no active snapshot for {scope}")
    occurrences = active_sales(tables, scope)
    result = clean_sales(occurrences, rules, run, snapshot_cutoff(tables, snapshot))
    counts = {
        "sales_inserted": tables.insert_new("silver.sales", result.sales),
        "events_inserted": tables.insert_new("silver.quality_events", result.events),
        "quarantine_inserted": tables.insert_new("silver.quarantine", result.quarantine),
        "total_occurrences": result.summary["total_occurrences"],
        "index_eligible": result.summary["index_eligible"],
    }
    ok = all(result.summary["checks"].values())
    if ok:
        from datetime import UTC, datetime

        from ireland_rsi.storage import record_stage

        record_stage(tables, "clean", snapshot, rules.version, run.run_id, datetime.now(UTC))
    stage = StageResult("clean_sales", run.run_id,
                        StageStatus.SUCCEEDED if ok else StageStatus.FAILED,
                        {"snapshot_id": snapshot, "rule_set_version": rules.version}, counts)
    return stage, result
