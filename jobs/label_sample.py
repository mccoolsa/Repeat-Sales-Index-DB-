"""Build the frozen labelling sample and Eircode weak labels.

    python jobs/label_sample.py --target local [--profile donegal]
    python label_sample.py --target databricks --project-root <bundle files> --profile donegal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ireland_rsi.labels import LabelConfig, run_label_sample
from ireland_rsi.profiles import load_profile, open_stores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=["local", "databricks"], required=True)
    # Databricks runs this file via exec() without __file__, so the job passes the root.
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--profile", default="national", help="config/profiles/<name>.yml")
    args = parser.parse_args(argv)

    root = args.project_root or Path(__file__).resolve().parents[1]
    profile = load_profile(root, args.profile)
    _, tables, _ = open_stores(profile, args.target)
    report = {"profile": profile.name,
              **run_label_sample(tables, profile.scope, LabelConfig.load(profile.labels_config))}
    print(json.dumps(report, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    # Databricks treats any SystemExit (even 0) as a task failure, so exit only on failure.
    if exit_code := main():
        sys.exit(exit_code)
