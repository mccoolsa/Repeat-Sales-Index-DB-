"""Shared schemas, result types and reason-code enums.

Every pipeline stage accepts a RunContext and returns a StageResult. Source files are
checked against a versioned SourceContract before any row is ingested.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import pyarrow as pa
import yaml

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ContractError(Exception):
    """Base class for source-contract failures. Ingestion must stop."""


class ContractViolation(ContractError):
    """A required source field is absent."""


class SchemaChangeError(ContractError):
    """The source header differs from every known contract version."""


# ---------------------------------------------------------------------------
# Source contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceField:
    name: str
    header: str
    required: bool


@dataclass(frozen=True)
class HeaderCheck:
    contract_id: str
    version: int
    missing_optional: tuple[str, ...]


@dataclass(frozen=True)
class SourceContract:
    contract_id: str
    version: int
    encoding: str
    delimiter: str
    quotechar: str
    line_terminator: str
    header_rows: int
    date_format: str
    fields: tuple[SourceField, ...]
    observed_in: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError(f"contract version must be a positive integer, got {self.version!r}")
        if not self.fields:
            raise ValueError("contract must declare at least one field")
        for attr in ("name", "header"):
            values = [getattr(f, attr) for f in self.fields]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate field {attr} in contract {self.contract_id}")

    @classmethod
    def from_dict(cls, data: Mapping) -> SourceContract:
        fields = tuple(
            SourceField(name=f["name"], header=f["header"], required=bool(f["required"]))
            for f in data["fields"]
        )
        return cls(
            contract_id=data["contract_id"],
            version=data["version"],
            encoding=data["encoding"],
            delimiter=data["delimiter"],
            quotechar=data["quotechar"],
            line_terminator=data["line_terminator"],
            header_rows=data["header_rows"],
            date_format=data["date_format"],
            fields=fields,
            observed_in=data.get("observed_in"),
        )

    @property
    def fingerprint(self) -> str:
        """Stable SHA-256 of the contract content, for run provenance."""
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def check_header(self, header: Sequence[str]) -> HeaderCheck:
        """Validate a decoded header row.

        Missing required fields raise ContractViolation. Unknown, duplicated or
        reordered columns raise SchemaChangeError: a new contract version is needed.
        Missing optional fields are allowed and reported.
        """
        header = list(header)
        by_header = {f.header: f for f in self.fields}

        missing_required = [f.name for f in self.fields if f.required and f.header not in header]
        if missing_required:
            raise ContractViolation(
                f"{self.contract_id} v{self.version}: required fields absent: {missing_required}"
            )

        if len(header) != len(set(header)):
            raise SchemaChangeError(f"{self.contract_id} v{self.version}: duplicated column names")

        unknown = [h for h in header if h not in by_header]
        if unknown:
            raise SchemaChangeError(
                f"{self.contract_id} v{self.version}: unknown columns {unknown}; "
                "declare a new contract version"
            )

        expected_order = [f.header for f in self.fields if f.header in header]
        if header != expected_order:
            raise SchemaChangeError(
                f"{self.contract_id} v{self.version}: column order changed; "
                "declare a new contract version"
            )

        missing_optional = tuple(f.name for f in self.fields if f.header not in header)
        return HeaderCheck(self.contract_id, self.version, missing_optional)


def load_contracts(path: str | Path) -> tuple[SourceContract, ...]:
    """Load every contract version from a YAML file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    contracts = tuple(SourceContract.from_dict(c) for c in data["contracts"])
    keys = [(c.contract_id, c.version) for c in contracts]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate contract id/version in {path}")
    return contracts


def select_contract(
    header: Sequence[str], contracts: Iterable[SourceContract]
) -> tuple[SourceContract, HeaderCheck]:
    """Return the highest contract version that accepts this header.

    A required-field violation in every candidate is reported as ContractViolation;
    otherwise an unmatched header is a SchemaChangeError.
    """
    errors: list[ContractError] = []
    for contract in sorted(contracts, key=lambda c: c.version, reverse=True):
        try:
            return contract, contract.check_header(header)
        except ContractError as exc:
            errors.append(exc)
    if errors and all(isinstance(e, ContractViolation) for e in errors):
        raise ContractViolation("; ".join(str(e) for e in errors))
    raise SchemaChangeError("; ".join(str(e) for e in errors) or "no contracts supplied")


# ---------------------------------------------------------------------------
# Run and stage results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """Identity of one pipeline run. No stage reads an unversioned 'latest' input."""

    run_id: str
    snapshot_ids: tuple[str, ...]
    as_of: datetime
    code_hash: str
    config_hash: str
    seed: int

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must be non-empty")
        if not isinstance(self.snapshot_ids, tuple) or not self.snapshot_ids:
            raise ValueError("snapshot_ids must be a non-empty tuple of explicit snapshot IDs")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")

    @classmethod
    def new(
        cls,
        snapshot_ids: Sequence[str],
        code_hash: str,
        config_hash: str,
        seed: int,
        as_of: datetime | None = None,
    ) -> RunContext:
        return cls(
            run_id=uuid.uuid4().hex,
            snapshot_ids=tuple(snapshot_ids),
            as_of=as_of or datetime.now(UTC),
            code_hash=code_hash,
            config_hash=config_hash,
            seed=seed,
        )


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    BLOCKING = "blocking"


class StageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class QualityEvent:
    rule_id: str
    severity: Severity
    reason: str
    record_id: str | None = None


@dataclass(frozen=True)
class StageResult:
    stage: str
    run_id: str
    status: StageStatus
    outputs: Mapping[str, str] = field(default_factory=dict)
    row_counts: Mapping[str, int] = field(default_factory=dict)
    quality_events: tuple[QualityEvent, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.status is StageStatus.FAILED or any(
            e.severity is Severity.BLOCKING for e in self.quality_events
        )


# ---------------------------------------------------------------------------
# Release and estimate states
# ---------------------------------------------------------------------------


class ReleaseState(StrEnum):
    RESEARCH = "research"
    VALIDATED = "validated"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class PeriodState(StrEnum):
    SUPPORTED = "supported"
    PROVISIONAL = "provisional"
    SUPPRESSED = "suppressed"


class EstimateReason(StrEnum):
    NO_MATCH = "NO_MATCH"
    AMBIGUOUS_PROPERTY = "AMBIGUOUS_PROPERTY"
    NO_ELIGIBLE_ANCHOR = "NO_ELIGIBLE_ANCHOR"
    UNRESOLVED_VAT = "UNRESOLVED_VAT"
    UNSUPPORTED_PERIOD = "UNSUPPORTED_PERIOD"
    DISCONNECTED_SERIES = "DISCONNECTED_SERIES"
    SPARSE_GEOGRAPHY = "SPARSE_GEOGRAPHY"
    UNCALIBRATED_INTERVAL = "UNCALIBRATED_INTERVAL"
    SOURCE_STALE = "SOURCE_STALE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Bronze and snapshot table schemas
# ---------------------------------------------------------------------------
# Logical names are "<schema>.<table>"; a TableStore maps them to DuckDB or to Unity
# Catalog ("<catalog>.<schema>.<table>"). Every table is insert-only on its key.

PPR_RAW_FIELDS: tuple[str, ...] = (
    "sale_date_raw",
    "address_raw",
    "county_raw",
    "eircode_raw",
    "price_raw",
    "not_full_market_price_raw",
    "vat_exclusive_raw",
    "description_raw",
    "size_description_raw",
)

_TS = pa.timestamp("us", tz="UTC")

TABLE_SCHEMAS: dict[str, pa.Schema] = {
    # One row per acquisition event and archive member; failed acquisitions have member "".
    "bronze.source_manifest": pa.schema([
        ("acquisition_id", pa.string()),
        ("member_name", pa.string()),
        ("source", pa.string()),
        ("scope", pa.string()),
        ("url", pa.string()),
        ("retrieved_utc", _TS),
        ("http_status", pa.int32()),
        ("http_last_modified", pa.string()),
        ("http_etag", pa.string()),
        ("archive_sha256", pa.string()),
        ("archive_bytes", pa.int64()),
        ("member_sha256", pa.string()),
        ("member_bytes", pa.int64()),
        ("snapshot_id", pa.string()),
        ("contract_id", pa.string()),
        ("contract_version", pa.int32()),
        ("encoding", pa.string()),
        ("record_count", pa.int64()),
        ("status", pa.string()),
        ("status_reason", pa.string()),
        ("run_id", pa.string()),
        ("code_hash", pa.string()),
    ]),
    # One logical CSV record in one member file. Identical records stay separate rows.
    "bronze.ppr_rows": pa.schema([
        ("raw_row_id", pa.string()),
        ("member_sha256", pa.string()),
        ("record_ordinal", pa.int64()),
        *[(name, pa.string()) for name in PPR_RAW_FIELDS],
        ("raw_content_fp", pa.string()),
        ("txn_fp", pa.string()),
        ("txn_fp_version", pa.string()),
    ]),
    # Append-only promotion decisions; the active snapshot per scope is the latest promotion.
    "bronze.snapshot_events": pa.schema([
        ("event_id", pa.string()),
        ("scope", pa.string()),
        ("snapshot_id", pa.string()),
        ("decision", pa.string()),
        ("previous_snapshot_id", pa.string()),
        ("reason", pa.string()),
        ("record_count", pa.int64()),
        ("decided_at", _TS),
        ("run_id", pa.string()),
    ]),
    # Which sale (version) each row occurrence in a snapshot represents.
    "silver.snapshot_membership": pa.schema([
        ("snapshot_id", pa.string()),
        ("raw_row_id", pa.string()),
        ("sale_id", pa.string()),
        ("sale_version", pa.int32()),
        ("match_type", pa.string()),
    ]),
    # Immutable sale versions; a correction adds a version, never overwrites one.
    "silver.sale_versions": pa.schema([
        ("sale_id", pa.string()),
        ("sale_version", pa.int32()),
        ("raw_content_fp", pa.string()),
        ("txn_fp", pa.string()),
        ("created_in_snapshot", pa.string()),
        ("created_from_row", pa.string()),
        ("supersedes_version", pa.int32()),
        ("change_type", pa.string()),
        ("correction_key", pa.string()),
    ]),
    # Parsed sales: one row per occurrence in a snapshot, per rule-set version.
    # Quarantined occurrences are in silver.quarantine instead, never dropped.
    "silver.sales": pa.schema([
        ("snapshot_id", pa.string()),
        ("raw_row_id", pa.string()),
        ("rule_set_version", pa.string()),
        ("sale_id", pa.string()),
        ("sale_version", pa.int32()),
        ("sale_date", pa.date32()),
        ("sale_quarter", pa.string()),
        ("county", pa.string()),
        ("address_raw", pa.string()),
        ("eircode", pa.string()),
        ("eircode_valid", pa.bool_()),
        ("reported_price_cents", pa.int64()),
        ("not_full_market_price", pa.bool_()),
        ("vat_exclusive", pa.bool_()),
        ("dwelling_status", pa.string()),
        ("description_language", pa.string()),
        ("size_band", pa.string()),
        ("vat_rule_id", pa.string()),
        ("vat_rate", pa.string()),
        ("gross_price_cents", pa.int64()),
        ("analysis_price_cents", pa.int64()),
        ("price_basis", pa.string()),
        ("disposition", pa.string()),
        ("reason_codes", pa.string()),
        ("parse_valid", pa.bool_()),
        ("linkage_eligible", pa.bool_()),
        ("index_eligible", pa.bool_()),
        ("duplicate_group_size", pa.int32()),
        ("run_id", pa.string()),
    ]),
    # Every non-pass rule result, with before/after values for transformations.
    "silver.quality_events": pa.schema([
        ("snapshot_id", pa.string()),
        ("raw_row_id", pa.string()),
        ("rule_set_version", pa.string()),
        ("rule_id", pa.string()),
        ("sale_id", pa.string()),
        ("severity", pa.string()),
        ("effect", pa.string()),
        ("value_before", pa.string()),
        ("value_after", pa.string()),
        ("run_id", pa.string()),
    ]),
    # Occurrences that could not be parsed; kept with blocking reasons and a resolution state.
    "silver.quarantine": pa.schema([
        ("snapshot_id", pa.string()),
        ("raw_row_id", pa.string()),
        ("rule_set_version", pa.string()),
        ("sale_id", pa.string()),
        ("sale_version", pa.int32()),
        ("reason_codes", pa.string()),
        ("resolution_state", pa.string()),
        ("run_id", pa.string()),
    ]),
    # Structured address features and locality assignment.
    "silver.address_features": pa.schema([
        ("snapshot_id", pa.string()),
        ("raw_row_id", pa.string()),
        ("address_version", pa.string()),
        ("sale_id", pa.string()),
        ("address_display", pa.string()),
        ("address_norm", pa.string()),
        ("components_json", pa.string()),
        ("unit_type", pa.string()),
        ("unit_id", pa.string()),
        ("house_number", pa.string()),
        ("house_suffix", pa.string()),
        ("house_range", pa.string()),
        ("primary_number", pa.string()),
        ("street", pa.string()),
        ("street_core", pa.string()),
        ("locality_parts_json", pa.string()),
        ("postal_district", pa.string()),
        ("county", pa.string()),
        ("locality_id", pa.string()),
        ("locality_name", pa.string()),
        ("locality_method", pa.string()),
        ("locality_confidence", pa.string()),
        ("ambiguity_state", pa.string()),
        ("townland_id", pa.string()),
        ("run_id", pa.string()),
    ]),
    # Versioned, county-scoped place-name aliases (Tailte Éireann townlands, CSO BUAs).
    "silver.place_aliases": pa.schema([
        ("reference_version", pa.string()),
        ("county", pa.string()),
        ("alias_norm", pa.string()),
        ("alias", pa.string()),
        ("target_type", pa.string()),
        ("target_id", pa.string()),
        ("language", pa.string()),
        ("origin", pa.string()),
        ("n_targets", pa.int32()),
        ("licence", pa.string()),
    ]),
    # CSO Census 2022 Built Up Areas and the counties each belongs to.
    "silver.localities": pa.schema([
        ("reference_version", pa.string()),
        ("bua_id", pa.string()),
        ("county", pa.string()),
        ("name", pa.string()),
        ("population_2022", pa.int64()),
        ("itm_e", pa.float64()),
        ("itm_n", pa.float64()),
        ("n_counties", pa.int32()),
        ("licence", pa.string()),
    ]),
    # Which output version each stage produced for a snapshot, and when.
    "silver.stage_runs": pa.schema([
        ("stage", pa.string()),
        ("snapshot_id", pa.string()),
        ("output_version", pa.string()),
        ("run_id", pa.string()),
        ("completed_at", _TS),
    ]),
    # Frozen stratified sample of candidate pairs for human review.
    "silver.label_sample": pa.schema([
        ("sample_version", pa.string()),
        ("pair_id", pa.string()),
        ("sample_order", pa.int32()),
        ("stratum", pa.string()),
        ("era", pa.string()),
        ("cell_size", pa.int64()),
        ("cell_sampled", pa.int32()),
        ("sampling_prob", pa.float64()),
        ("split", pa.string()),
        ("group_id", pa.string()),
        ("double_review", pa.bool_()),
        ("eircode_status", pa.string()),
        ("weak_label", pa.string()),
        ("county", pa.string()),
        ("sale_id_a", pa.string()),
        ("sale_id_b", pa.string()),
        ("raw_row_id_a", pa.string()),
        ("raw_row_id_b", pa.string()),
        ("date_a", pa.date32()),
        ("date_b", pa.date32()),
        ("address_a", pa.string()),
        ("address_b", pa.string()),
        ("status_a", pa.string()),
        ("status_b", pa.string()),
        ("size_a", pa.string()),
        ("size_b", pa.string()),
    ]),
    # Human labels, append-only; the latest label per (pair, reviewer) is current.
    "silver.link_labels": pa.schema([
        ("sample_version", pa.string()),
        ("pair_id", pa.string()),
        ("reviewer", pa.string()),
        ("labelled_at", _TS),
        ("label", pa.string()),
        ("rationale", pa.string()),
        ("app_version", pa.string()),
    ]),
    # Eircode-derived weak labels: evidence, not ground truth.
    "silver.weak_labels": pa.schema([
        ("snapshot_id", pa.string()),
        ("rule_set_version", pa.string()),
        ("sale_id_a", pa.string()),
        ("sale_id_b", pa.string()),
        ("eircode", pa.string()),
        ("weak_label", pa.string()),
        ("conflict_reason", pa.string()),
        ("code_sales", pa.int32()),
    ]),
    # Every scored candidate pair for a trained linkage model.
    "silver.link_candidates": pa.schema([
        ("model_version", pa.string()),
        ("track", pa.string()),
        ("sale_id_l", pa.string()),
        ("sale_id_r", pa.string()),
        ("match_weight", pa.float64()),
        ("match_probability", pa.float64()),
        ("rules_hit", pa.string()),
        ("baseline_link", pa.bool_()),
        ("gamma_county", pa.int32()),
        ("gamma_primary_number", pa.int32()),
        ("gamma_unit_id", pa.int32()),
        ("gamma_street_core", pa.int32()),
        ("gamma_locality_id", pa.int32()),
        ("gamma_townland_id", pa.int32()),
        ("gamma_postal_district", pa.int32()),
        ("gamma_eircode", pa.int32()),
    ]),
    # Trained linkage models with their inputs, blocking report and parameters.
    "silver.link_models": pa.schema([
        ("model_version", pa.string()),
        ("track", pa.string()),
        ("trained_at", _TS),
        ("snapshot_id", pa.string()),
        ("rule_set_version", pa.string()),
        ("address_version", pa.string()),
        ("records", pa.int64()),
        ("candidate_pairs", pa.int64()),
        ("settings_sha256", pa.string()),
        ("settings_json", pa.string()),
        ("blocking_report_json", pa.string()),
        ("training_log_json", pa.string()),
        ("run_id", pa.string()),
    ]),
    # Property membership per entity version. Property IDs are generated, not official.
    "silver.entity_membership": pa.schema([
        ("entity_version", pa.string()),
        ("property_id", pa.string()),
        ("sale_id", pa.string()),
        ("n_sales", pa.int32()),
        ("attach_weight", pa.float64()),
    ]),
    # Accepted links refused because they would merge conflicting facts.
    "silver.entity_rejected_edges": pa.schema([
        ("entity_version", pa.string()),
        ("sale_id_l", pa.string()),
        ("sale_id_r", pa.string()),
        ("match_probability", pa.float64()),
        ("match_weight", pa.float64()),
        ("reason", pa.string()),
    ]),
    # How properties map between entity versions; history is never silently rewritten.
    "silver.entity_crosswalk": pa.schema([
        ("from_version", pa.string()),
        ("to_version", pa.string()),
        ("property_id_old", pa.string()),
        ("property_id_new", pa.string()),
        ("overlap_sales", pa.int64()),
        ("relation", pa.string()),
    ]),
    # Consecutive repeat-sale pairs within property histories.
    "silver.repeat_pairs": pa.schema([
        ("entity_version", pa.string()),
        ("pair_id", pa.string()),
        ("property_id", pa.string()),
        ("segment", pa.int32()),
        ("sale_id_1", pa.string()),
        ("sale_version_1", pa.int32()),
        ("sale_id_2", pa.string()),
        ("sale_version_2", pa.int32()),
        ("date_1", pa.date32()),
        ("date_2", pa.date32()),
        ("quarter_1", pa.string()),
        ("quarter_2", pa.string()),
        ("price_cents_1", pa.int64()),
        ("price_cents_2", pa.int64()),
        ("status_1", pa.string()),
        ("status_2", pa.string()),
        ("holding_days", pa.int32()),
        ("holding_years", pa.float64()),
        ("log_return", pa.float64()),
        ("total_return", pa.float64()),
        ("annualised_return", pa.float64()),
        ("short_hold", pa.bool_()),
        ("unusual_change", pa.bool_()),
        ("decision", pa.string()),
    ]),
    # Research index fits: one row per geography, frequency, method and population.
    "silver.index_fits": pa.schema([
        ("fit_id", pa.string()),
        ("fit_version", pa.string()),
        ("entity_version", pa.string()),
        ("geography_id", pa.string()),
        ("frequency", pa.string()),
        ("method", pa.string()),
        ("population", pa.string()),
        ("status", pa.string()),
        ("failure_reason", pa.string()),
        ("reference_period", pa.string()),
        ("base", pa.string()),
        ("base_status", pa.string()),
        ("pairs_input", pa.int64()),
        ("pairs_used", pa.int64()),
        ("properties_used", pa.int64()),
        ("excluded_same_period", pa.int64()),
        ("excluded_disconnected", pa.int64()),
        ("excluded_trimmed", pa.int64()),
        ("unidentified_periods", pa.string()),
        ("variance_coefficients", pa.string()),
        ("variance_floor", pa.float64()),
        ("variance_status", pa.string()),
        ("diagnostics_json", pa.string()),
        ("created_at", _TS),
        ("run_id", pa.string()),
    ]),
    # Index coefficients and levels per period for each fit.
    "silver.index_points": pa.schema([
        ("fit_id", pa.string()),
        ("period", pa.string()),
        ("beta", pa.float64()),
        ("se", pa.float64()),
        ("index_value", pa.float64()),
        ("endpoint_pairs", pa.int32()),
        ("is_reference", pa.bool_()),
    ]),
    # Per-pair residuals and weights (headline WRS fits) for anomaly review and bootstrap.
    "silver.index_pair_residuals": pa.schema([
        ("fit_id", pa.string()),
        ("pair_id", pa.string()),
        ("residual", pa.float64()),
        ("weight", pa.float64()),
    ]),
    # Bootstrap intervals and period states per fit.
    "silver.index_uncertainty": pa.schema([
        ("uncertainty_version", pa.string()),
        ("fit_id", pa.string()),
        ("period", pa.string()),
        ("index_value", pa.float64()),
        ("level_lo", pa.float64()),
        ("level_median", pa.float64()),
        ("level_hi", pa.float64()),
        ("log_width", pa.float64()),
        ("endpoint_pairs", pa.int32()),
        ("period_state", pa.string()),
        ("state_reason", pa.string()),
    ]),
    # Series-level support metrics and publication decision.
    "silver.series_support": pa.schema([
        ("uncertainty_version", pa.string()),
        ("fit_id", pa.string()),
        ("geography_id", pa.string()),
        ("frequency", pa.string()),
        ("supported", pa.bool_()),
        ("reasons", pa.string()),
        ("properties", pa.int64()),
        ("effective_sample_size", pa.float64()),
        ("bootstrap_attempted", pa.int32()),
        ("bootstrap_valid", pa.int32()),
        ("max_property_weight_share", pa.float64()),
        ("periods_supported", pa.int32()),
        ("periods_suppressed", pa.int32()),
        ("metrics_json", pa.string()),
        ("created_at", _TS),
        ("run_id", pa.string()),
    ]),
    # Joint bootstrap draws (log levels), for ratio intervals in the lookup.
    "silver.index_draws": pa.schema([
        ("uncertainty_version", pa.string()),
        ("fit_id", pa.string()),
        ("draw", pa.int32()),
        ("period", pa.string()),
        ("log_level", pa.float64()),
    ]),
    # Ambiguous reconciliation left for human review.
    "silver.reconciliation_reviews": pa.schema([
        ("snapshot_id", pa.string()),
        ("raw_row_id", pa.string()),
        ("reason", pa.string()),
        ("candidate_sale_ids", pa.string()),
    ]),
    # --- Gold: validation, backtests and published releases -----------
    # Every validation, backtest and sensitivity metric, keyed by the producing version.
    "gold.validation_results": pa.schema([
        ("validation_version", pa.string()),
        ("category", pa.string()),
        ("experiment", pa.string()),
        ("geography_id", pa.string()),
        ("frequency", pa.string()),
        ("metric", pa.string()),
        ("segment", pa.string()),
        ("value", pa.float64()),
        ("n", pa.int64()),
        ("detail", pa.string()),
    ]),
    # Index levels of each sensitivity experiment and the aligned CSO benchmark.
    "gold.sensitivity_series": pa.schema([
        ("validation_version", pa.string()),
        ("experiment", pa.string()),
        ("geography_id", pa.string()),
        ("frequency", pa.string()),
        ("period", pa.string()),
        ("index_value", pa.float64()),
    ]),
    # Out-of-sample lookup predictions: one row per evaluated pair and method.
    "gold.backtest_predictions": pa.schema([
        ("backtest_version", pa.string()),
        ("pair_id", pa.string()),
        ("method", pa.string()),
        ("property_id", pa.string()),
        ("fold", pa.int32()),
        ("window", pa.string()),
        ("target_year", pa.int32()),
        ("anchor_period", pa.string()),
        ("target_period", pa.string()),
        ("holding_years", pa.float64()),
        ("geography_id", pa.string()),
        ("fallback_level", pa.int32()),
        ("in_town", pa.bool_()),
        ("actual_eur", pa.float64()),
        ("predicted_eur", pa.float64()),
        ("log_error", pa.float64()),
        ("status", pa.string()),
    ]),
    # Property prediction intervals calibrated on the calibration window, tested on the test
    # window.
    "gold.prediction_calibration": pa.schema([
        ("backtest_version", pa.string()),
        ("horizon_band", pa.string()),
        ("min_years", pa.float64()),
        ("max_years", pa.float64()),
        ("level", pa.float64()),
        ("n_calibration", pa.int64()),
        ("q_lo", pa.float64()),
        ("q_hi", pa.float64()),
        ("n_test", pa.int64()),
        ("coverage_test", pa.float64()),
        ("coverage_lo", pa.float64()),
        ("coverage_hi", pa.float64()),
        ("median_width", pa.float64()),
        ("status", pa.string()),
    ]),
    # One immutable release: inputs, versions and gate results.
    "gold.release_manifest": pa.schema([
        ("release_id", pa.string()),
        ("release_state", pa.string()),
        ("created_at", _TS),
        ("profile", pa.string()),
        ("scope", pa.string()),
        ("snapshot_id", pa.string()),
        ("versions_json", pa.string()),
        ("gates_json", pa.string()),
        ("metrics_json", pa.string()),
        ("lookup_policy_json", pa.string()),
        ("code_hash", pa.string()),
        ("config_hash", pa.string()),
        ("previous_release_id", pa.string()),
        ("run_id", pa.string()),
    ]),
    # Append-only pointer history: the latest publish or rollback event is current.
    "gold.release_events": pa.schema([
        ("event_id", pa.string()),
        ("release_id", pa.string()),
        ("action", pa.string()),
        ("event_at", _TS),
        ("run_id", pa.string()),
        ("reason", pa.string()),
    ]),
    # Published index levels and states per release (never overwritten).
    "gold.index_vintages": pa.schema([
        ("release_id", pa.string()),
        ("geography_id", pa.string()),
        ("frequency", pa.string()),
        ("period", pa.string()),
        ("index_value", pa.float64()),
        ("level_lo", pa.float64()),
        ("level_hi", pa.float64()),
        ("log_width", pa.float64()),
        ("endpoint_pairs", pa.int32()),
        ("period_state", pa.string()),
        ("state_reason", pa.string()),
        ("series_supported", pa.bool_()),
        ("series_properties", pa.int64()),
        ("fit_id", pa.string()),
    ]),
    # Public sale facts per property for the lookup app.
    "gold.lookup_history": pa.schema([
        ("release_id", pa.string()),
        ("sale_id", pa.string()),
        ("property_id", pa.string()),
        ("property_sales", pa.int32()),
        ("sale_date", pa.date32()),
        ("address", pa.string()),
        ("address_search", pa.string()),
        ("county", pa.string()),
        ("locality_name", pa.string()),
        ("locality_confidence", pa.string()),
        ("reported_price_eur", pa.float64()),
        ("analysis_price_eur", pa.float64()),
        ("price_basis", pa.string()),
        ("dwelling_status", pa.string()),
        ("not_full_market_price", pa.bool_()),
        ("anchor_eligible", pa.bool_()),
        ("anchor_reason", pa.string()),
        ("link_strength", pa.string()),
    ]),
    # Calibrated property intervals in force for a release.
    "gold.release_calibration": pa.schema([
        ("release_id", pa.string()),
        ("horizon_band", pa.string()),
        ("min_years", pa.float64()),
        ("max_years", pa.float64()),
        ("level", pa.float64()),
        ("q_lo", pa.float64()),
        ("q_hi", pa.float64()),
        ("coverage_test", pa.float64()),
        ("n_test", pa.int64()),
        ("status", pa.string()),
    ]),
    # How each published level moved since the previous release, and why.
    "gold.revisions": pa.schema([
        ("release_id", pa.string()),
        ("previous_release_id", pa.string()),
        ("geography_id", pa.string()),
        ("frequency", pa.string()),
        ("period", pa.string()),
        ("previous_value", pa.float64()),
        ("value", pa.float64()),
        ("log_revision", pa.float64()),
        ("cause", pa.string()),
    ]),
    # Run log of the orchestrated pipeline: one row per stage per run.
    "silver.pipeline_runs": pa.schema([
        ("run_id", pa.string()),
        ("stage", pa.string()),
        ("status", pa.string()),
        ("started_at", _TS),
        ("finished_at", _TS),
        ("seconds", pa.float64()),
        ("output_version", pa.string()),
        ("detail_json", pa.string()),
    ]),
}

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "bronze.source_manifest": ("acquisition_id", "member_name"),
    "bronze.ppr_rows": ("raw_row_id",),
    "bronze.snapshot_events": ("event_id",),
    "silver.snapshot_membership": ("snapshot_id", "raw_row_id"),
    "silver.sale_versions": ("sale_id", "sale_version"),
    "silver.reconciliation_reviews": ("snapshot_id", "raw_row_id"),
    "silver.sales": ("snapshot_id", "raw_row_id", "rule_set_version"),
    "silver.quality_events": ("snapshot_id", "raw_row_id", "rule_set_version", "rule_id"),
    "silver.quarantine": ("snapshot_id", "raw_row_id", "rule_set_version"),
    "silver.address_features": ("snapshot_id", "raw_row_id", "address_version"),
    "silver.place_aliases": ("reference_version", "county", "alias_norm", "target_type",
                             "target_id"),
    "silver.localities": ("reference_version", "bua_id", "county"),
    "silver.stage_runs": ("stage", "snapshot_id", "output_version", "run_id"),
    "silver.label_sample": ("sample_version", "pair_id"),
    "silver.link_labels": ("sample_version", "pair_id", "reviewer", "labelled_at"),
    "silver.weak_labels": ("snapshot_id", "rule_set_version", "sale_id_a", "sale_id_b"),
    "silver.link_candidates": ("model_version", "track", "sale_id_l", "sale_id_r"),
    "silver.link_models": ("model_version", "track"),
    "silver.entity_membership": ("entity_version", "sale_id"),
    "silver.entity_rejected_edges": ("entity_version", "sale_id_l", "sale_id_r"),
    "silver.entity_crosswalk": ("from_version", "to_version", "property_id_old",
                                "property_id_new"),
    "silver.repeat_pairs": ("entity_version", "pair_id"),
    "silver.index_fits": ("fit_id",),
    "silver.index_points": ("fit_id", "period"),
    "silver.index_pair_residuals": ("fit_id", "pair_id"),
    "silver.index_uncertainty": ("uncertainty_version", "fit_id", "period"),
    "silver.series_support": ("uncertainty_version", "fit_id"),
    "silver.index_draws": ("uncertainty_version", "fit_id", "draw", "period"),
    "gold.validation_results": ("validation_version", "category", "experiment", "geography_id",
                                "frequency", "metric", "segment"),
    "gold.sensitivity_series": ("validation_version", "experiment", "geography_id", "frequency",
                                "period"),
    "gold.backtest_predictions": ("backtest_version", "pair_id", "method"),
    "gold.prediction_calibration": ("backtest_version", "horizon_band"),
    "gold.release_manifest": ("release_id",),
    "gold.release_events": ("event_id",),
    "gold.index_vintages": ("release_id", "geography_id", "frequency", "period"),
    "gold.lookup_history": ("release_id", "sale_id"),
    "gold.release_calibration": ("release_id", "horizon_band"),
    "gold.revisions": ("release_id", "geography_id", "frequency", "period"),
    "silver.pipeline_runs": ("run_id", "stage"),
}


class SnapshotDecision(StrEnum):
    PROMOTED = "promoted"          # became the active snapshot for its scope
    HELD = "held"                  # structurally valid but failed a reconciliation check
    REJECTED = "rejected"          # failed acquisition or structural validation
    REFERENCE = "reference"        # non-authoritative scope; kept for cross-checks only
    ALREADY_ACTIVE = "already_active"
