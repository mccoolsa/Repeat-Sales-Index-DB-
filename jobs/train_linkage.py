"""Generate candidates, train and score a Splink linkage model.

    python jobs/train_linkage.py --target local --track address_only [--profile donegal]
    python train_linkage.py --target databricks --project-root <bundle files> --track production
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ireland_rsi.contracts import RunContext
from ireland_rsi.ingest import code_hash
from ireland_rsi.linkage import TRACKS, run_linkage
from ireland_rsi.profiles import load_profile, mlflow_experiment_path, open_stores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=["local", "databricks"], required=True)
    # Databricks runs this file via exec() without __file__, so the job passes the root.
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--profile", default="national", help="config/profiles/<name>.yml")
    parser.add_argument("--track", choices=TRACKS, required=True)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args(argv)

    root = args.project_root or Path(__file__).resolve().parents[1]
    profile = load_profile(root, args.profile)
    objects, tables, spark = open_stores(profile, args.target)
    run = RunContext.new([profile.scope], code_hash=code_hash(), config_hash=args.track,
                         seed=20261006)
    report = {"profile": profile.name, "run_id": run.run_id,
              **run_linkage(tables, objects, profile.scope, args.track, run,
                            mlflow_experiment=mlflow_experiment_path(profile, spark))}
    print(json.dumps(report, indent=2, default=str), flush=True)
    if args.report_dir:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / f"linkage_{profile.name}_{args.track}.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    # Databricks treats any SystemExit (even 0) as a task failure, so exit only on failure.
    if exit_code := main():
        sys.exit(exit_code)
