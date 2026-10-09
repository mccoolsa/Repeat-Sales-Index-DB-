"""Run the whole pipeline (or a slice of it) locally, logging every stage.

    python jobs/run_pipeline.py --profile donegal                      # full refresh
    python jobs/run_pipeline.py --profile donegal --start validation   # restart a slice

On Databricks the same order is the job's task graph; use `databricks bundle run`.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from ireland_rsi.pipeline import STAGES, run_pipeline
from ireland_rsi.profiles import load_profile, open_stores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="donegal")
    parser.add_argument("--start", choices=[s[0] for s in STAGES])
    parser.add_argument("--stop", choices=[s[0] for s in STAGES])
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    _, tables, _ = open_stores(load_profile(root, args.profile), "local")
    out = run_pipeline(root, args.profile, "local", tables, f"pipe-{uuid.uuid4().hex[:12]}",
                       args.start, args.stop)
    print(json.dumps(out, indent=2), flush=True)
    return 0 if out["succeeded"] else 1


if __name__ == "__main__":
    if exit_code := main():
        sys.exit(exit_code)
