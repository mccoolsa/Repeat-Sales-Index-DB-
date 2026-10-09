"""Publish a release, roll back, or run a failure drill.

    python jobs/publish_release.py --target local --profile donegal
    python jobs/publish_release.py --target local --profile donegal --rollback-to rel-...
    python jobs/publish_release.py --target local --profile donegal --drill gate
    python jobs/publish_release.py --target local --profile donegal --drill gold.lookup_history
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

from ireland_rsi.contracts import RunContext
from ireland_rsi.ingest import code_hash
from ireland_rsi.profiles import load_profile, open_stores
from ireland_rsi.publish import ReleaseError, current_release, rollback, run_publish


def config_hash(root: Path, profile: str) -> str:
    """Hash of every configuration file a release depends on."""
    digest = hashlib.sha256()
    files = sorted((root / "config").glob("*.yml")) + [
        root / "config" / "profiles" / f"{profile}.yml"]
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=["local", "databricks"], required=True)
    # Databricks runs this file via exec() without __file__, so the job passes the root.
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--profile", default="national", help="config/profiles/<name>.yml")
    parser.add_argument("--rollback-to", help="point the current release at this release")
    parser.add_argument("--reason", default="manual rollback")
    parser.add_argument("--drill", help="'gate' or a gold table name to fail after writing")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args(argv)

    root = args.project_root or Path(__file__).resolve().parents[1]
    profile = load_profile(root, args.profile)
    entity_cfg = yaml.safe_load((root / "config" / "entities.yml").read_text(encoding="utf-8"))
    policy = yaml.safe_load((root / "config" / "publication.yml").read_text(encoding="utf-8"))
    _, tables, _ = open_stores(profile, args.target)
    cfg_hash = config_hash(root, profile.name)
    run = RunContext.new([profile.scope], code_hash=code_hash(), config_hash=cfg_hash, seed=0)
    if args.rollback_to:
        try:
            rollback(tables, args.rollback_to, run.run_id, args.reason)
        except ReleaseError as exc:
            print(json.dumps({"rollback": "refused", "reason": str(exc),
                              "current_release": current_release(tables)}), flush=True)
            return 1
        report = {"rollback_to": args.rollback_to, "current_release": current_release(tables)}
    else:
        try:
            report = run_publish(tables, profile, entity_cfg, policy, run.code_hash, cfg_hash, run,
                                 simulate=args.drill)
        except ReleaseError as exc:   # a drill that fails mid-write
            report = {"drill": args.drill, "error": str(exc),
                      "current_release": current_release(tables)}
            print(json.dumps(report, indent=2, default=str), flush=True)
            return 0 if args.drill else 1
    report = {"profile": profile.name, "run_id": run.run_id, **report}
    print(json.dumps(report, indent=2, default=str), flush=True)
    if args.report_dir:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        name = "release" if not args.drill else f"drill_{args.drill.replace('.', '_')}"
        (args.report_dir / f"{name}_{profile.name}.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
    # A release that failed its gates is a failed task, so the job alerts.
    return 0 if report.get("published") or report.get("unchanged") or args.rollback_to \
        or args.drill else 1


if __name__ == "__main__":
    # Databricks treats any SystemExit (even 0) as a task failure, so exit only on failure.
    if exit_code := main():
        sys.exit(exit_code)
