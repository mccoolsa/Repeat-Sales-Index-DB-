"""Land a PSRA register snapshot into bronze and reconcile it.

    python jobs/ingest_ppr.py --target local [--profile donegal] [--saved data/raw/ppr/2026-10-06]
    python ingest_ppr.py --target databricks --project-root <bundle files> --profile donegal

Without --saved the file is downloaded from the configured URL. After landing, the
snapshot is rebuilt from its stored bytes and compared with bronze (rebuild check).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ireland_rsi.contracts import RunContext, StageStatus, load_contracts
from ireland_rsi.ingest import (
    active_snapshot,
    code_hash,
    fetch,
    fetch_result_from_saved,
    land_snapshot,
    verify_rebuild,
)
from ireland_rsi.profiles import load_profile, open_stores
from ireland_rsi.storage import sha256_hex


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=["local", "databricks"], required=True)
    # Databricks runs this file via exec() without __file__, so the job passes the root.
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--profile", default="national", help="config/profiles/<name>.yml")
    parser.add_argument("--saved", type=Path, help="import a saved acquisition folder")
    parser.add_argument("--force-promote", action="store_true",
                        help="approve a snapshot that a reconciliation check held")
    args = parser.parse_args(argv)

    root = args.project_root or Path(__file__).resolve().parents[1]
    profile = load_profile(root, args.profile)
    contract_path = root / "config" / "source_contract.yml"
    contracts = load_contracts(contract_path)
    spec = profile.source
    objects, tables, _ = open_stores(profile, args.target)

    fetched = fetch_result_from_saved(args.saved) if args.saved else fetch(spec["url"])
    config_hash = sha256_hex(
        (root / "config" / "profiles" / f"{args.profile}.yml").read_bytes()
        + contract_path.read_bytes())
    run = RunContext.new([profile.scope], code_hash=code_hash(), config_hash=config_hash,
                         seed=0)
    print(f"[{profile.name}] run {run.run_id}: {len(fetched.body):,} bytes from {fetched.url} "
          f"retrieved {fetched.retrieved_utc.isoformat()}", flush=True)

    result = land_snapshot(
        fetched, source=spec["source"], scope=profile.scope,
        expected_members=spec["expected_members"], contracts=contracts, objects=objects,
        tables=tables, run=run, policy=profile.policy, force_promote=args.force_promote,
        county_filter=profile.county_filter)
    print("result", json.dumps({"status": result.status.value, "outputs": result.outputs,
                                "row_counts": result.row_counts,
                                "events": [e.reason for e in result.quality_events]},
                               default=str, indent=2), flush=True)

    snapshot_id = result.outputs.get("snapshot_id")
    if snapshot_id and result.outputs.get("decision") in ("promoted", "already_active"):
        report = verify_rebuild(objects, tables, snapshot_id, contracts,
                                county_filter=profile.county_filter)
        print("rebuild check", json.dumps(report, indent=2), flush=True)
        if not report["rows_identical"]:
            return 2
    print("active snapshot:", active_snapshot(tables, profile.scope), flush=True)
    return 0 if result.status is StageStatus.SUCCEEDED else 1


if __name__ == "__main__":
    # Databricks treats any SystemExit (even 0) as a task failure, so exit only on failure.
    if exit_code := main():
        sys.exit(exit_code)
