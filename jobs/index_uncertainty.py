"""Bootstrap index uncertainty and apply publication gates.

    python jobs/index_uncertainty.py --target local --profile donegal [--draws 1000]
    python index_uncertainty.py --target databricks --project-root <bundle files> --profile donegal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ireland_rsi.contracts import RunContext
from ireland_rsi.geography import run_uncertainty
from ireland_rsi.ingest import code_hash
from ireland_rsi.profiles import load_profile, open_stores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=["local", "databricks"], required=True)
    # Databricks runs this file via exec() without __file__, so the job passes the root.
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--profile", default="national", help="config/profiles/<name>.yml")
    parser.add_argument("--draws", type=int, help="default: bootstrap.draws_release")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args(argv)

    root = args.project_root or Path(__file__).resolve().parents[1]
    profile = load_profile(root, args.profile)
    index_cfg = yaml.safe_load((root / "config" / "index.yml").read_text(encoding="utf-8"))
    policy_bytes = (root / "config" / "publication.yml").read_bytes()
    policy = yaml.safe_load(policy_bytes.decode("utf-8"))
    draws = args.draws or policy["bootstrap"]["draws_release"]
    _, tables, _ = open_stores(profile, args.target)
    run = RunContext.new([profile.scope], code_hash=code_hash(), config_hash="uncertainty",
                         seed=policy["bootstrap"]["seed"])
    report = {"profile": profile.name, "run_id": run.run_id,
              **run_uncertainty(tables, profile.scope, index_cfg, policy, policy_bytes, run,
                                draws)}
    print(json.dumps(report, indent=2, default=str), flush=True)
    if args.report_dir:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / f"uncertainty_{profile.name}.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    # Databricks treats any SystemExit (even 0) as a task failure, so exit only on failure.
    if exit_code := main():
        sys.exit(exit_code)
