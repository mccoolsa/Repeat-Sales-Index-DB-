"""Ordered pipeline execution with a persisted run log.

    acquire -> clean -> addresses -> linkage (both tracks) -> entities and pairs -> index
    -> uncertainty -> validation -> backtest -> publish

On Databricks the same order is the job's task graph (databricks.yml); locally this
module runs the job scripts in sequence. Each stage writes versioned, insert-only outputs
keyed by hashes of its inputs, code and configuration, so a repeated run with unchanged
inputs adds nothing, and a failed stage can be re-run without touching completed ones.
A failure stops the run before `publish`, so the previous release stays current.

Every refresh recomputes derived stages from the active snapshot (no incremental
scoring): at pilot scale a full Donegal refresh takes minutes, and a full rebuild cannot
miss neighbours of a corrected sale. Incremental scoring is a national scale-up item
(decision D-026).
"""

from __future__ import annotations

import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

STAGES: list[tuple[str, str, list[str]]] = [
    ("ingest", "ingest_ppr.py", []),
    ("clean", "clean_sales.py", []),
    ("addresses", "build_addresses.py", []),
    ("linkage_address_only", "train_linkage.py", ["--track", "address_only"]),
    ("linkage_production", "train_linkage.py", ["--track", "production"]),
    ("entities", "build_pairs.py", []),
    ("index", "fit_index.py", []),
    ("uncertainty", "index_uncertainty.py", []),
    ("validation", "validate_index.py", []),
    ("backtest", "backtest_lookup.py", []),
    ("publish", "publish_release.py", []),
]


def _load_job(path: Path):
    spec = importlib.util.spec_from_file_location(f"job_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_pipeline(root: Path, profile: str, target: str, tables, run_id: str,
                 start: str | None = None, stop: str | None = None,
                 extra: dict[str, list[str]] | None = None) -> dict:
    """Run stages in order from `start` to `stop` (inclusive); log each to
    silver.pipeline_runs. Returns per-stage status and timings."""
    from ireland_rsi.contracts import TABLE_SCHEMAS

    names = [s[0] for s in STAGES]
    lo = names.index(start) if start else 0
    hi = names.index(stop) if stop else len(names) - 1
    log, ok = [], True
    for stage, script, args in STAGES[lo:hi + 1]:
        argv = ["--target", target, "--project-root", str(root), "--profile", profile,
                *args, *(extra or {}).get(stage, [])]
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        status, detail = "succeeded", {}
        try:
            code = _load_job(root / "jobs" / script).main(argv)
            if code:
                status, detail = "failed", {"exit_code": code}
        except Exception as exc:   # recorded, then the run stops
            status, detail = "failed", {"error": f"{type(exc).__name__}: {exc}"[:2000]}
        seconds = time.perf_counter() - t0
        log.append({"run_id": run_id, "stage": stage, "status": status, "started_at": started,
                    "finished_at": datetime.now(UTC), "seconds": round(seconds, 2),
                    "output_version": None, "detail_json": json.dumps(detail)})
        tables.insert_new("silver.pipeline_runs", pd.DataFrame(
            [log[-1]])[TABLE_SCHEMAS["silver.pipeline_runs"].names])
        if status != "succeeded":
            ok = False
            break
    return {"run_id": run_id, "succeeded": ok,
            "stages": [{k: r[k] for k in ("stage", "status", "seconds")} | json.loads(
                r["detail_json"]) for r in log],
            "total_seconds": round(sum(r["seconds"] for r in log), 1)}
