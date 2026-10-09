"""Constrained property histories and repeat-sale pairs.

    python jobs/build_pairs.py --target local --profile donegal [--report-dir reports]
    python build_pairs.py --target databricks --project-root <bundle files> --profile donegal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ireland_rsi.contracts import RunContext
from ireland_rsi.entities import EntityConfig
from ireland_rsi.ingest import code_hash
from ireland_rsi.pairs import run_histories
from ireland_rsi.profiles import load_profile, open_stores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=["local", "databricks"], required=True)
    # Databricks runs this file via exec() without __file__, so the job passes the root.
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--profile", default="national", help="config/profiles/<name>.yml")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args(argv)

    root = args.project_root or Path(__file__).resolve().parents[1]
    profile = load_profile(root, args.profile)
    config = EntityConfig.load(root / "config" / "entities.yml")
    _, tables, _ = open_stores(profile, args.target)
    run = RunContext.new([profile.scope], code_hash=code_hash(), config_hash=config.digest,
                         seed=0)
    report = {"profile": profile.name, "run_id": run.run_id,
              **run_histories(tables, profile.scope, config, run)}
    print(json.dumps(report, indent=2, default=str), flush=True)
    if args.report_dir:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / f"pairs_{profile.name}.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    # Databricks treats any SystemExit (even 0) as a task failure, so exit only on failure.
    if exit_code := main():
        sys.exit(exit_code)
