"""Measure lookup latency against the SQL warehouse with the app's own service code (lookup check).

    python jobs/lookup_latency.py --catalog ireland_rsi_donegal --repeats 20

Uses the Databricks CLI profile for authentication. The first (cold) pass, which may wait
for the warehouse to start, is reported separately from the warm passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "lookup"))
import service  # noqa: E402

ADDRESSES = ["19 Ard Colmcille Letterkenny", "Apt 8 Burnside Park Letterkenny",
             "29 Amalfi Court Letterkenny", "Main Street Letterkenny", "Glencar Letterkenny"]


def main() -> int:
    from databricks import sql
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", default="ireland_rsi_donegal")
    parser.add_argument("--profile", default="ireland-rsi")
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    cfg = Config(profile=args.profile)
    warehouse = WorkspaceClient(config=cfg).warehouses.list()
    http_path = f"/sql/1.0/warehouses/{next(iter(warehouse)).id}"
    conn = sql.connect(server_hostname=cfg.host, http_path=http_path,
                       credentials_provider=lambda: cfg.authenticate)

    class Warehouse:
        def query(self, statement, params):
            with conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchall_arrow().to_pandas()

    db = Warehouse()

    def workflow(text):
        t0 = time.perf_counter()
        rel = service.current_release(db, args.catalog)
        hits = service.search_properties(db, args.catalog, text, None, rel.release_id)
        result = None
        if len(hits):
            pid = hits.property_id.iloc[0]
            service.get_history(db, args.catalog, pid, rel.release_id)
            result = service.estimate(db, args.catalog, pid, rel)
        return time.perf_counter() - t0, rel, hits, result

    cold, rel, _, _ = workflow(ADDRESSES[0])
    timings, outcomes = [], []
    for i in range(args.repeats):
        text = ADDRESSES[i % len(ADDRESSES)]
        seconds, rel, hits, result = workflow(text)
        timings.append(seconds)
        outcomes.append({"query": text, "seconds": round(seconds, 3), "hits": len(hits),
                         "status": None if result is None else result.status,
                         "reason": None if result is None else result.reason_code,
                         "amount": None if result is None or result.amount is None
                         else round(result.amount)})
    t = np.array(timings)
    report = {"release_id": rel.release_id, "cold_seconds": round(cold, 3),
              "warm_runs": len(t), "warm_p50": round(float(np.percentile(t, 50)), 3),
              "warm_p95": round(float(np.percentile(t, 95)), 3),
              "warm_max": round(float(t.max()), 3),
              "examples": pd.DataFrame(outcomes).drop_duplicates("query").to_dict("records")}
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if exit_code := main():
        sys.exit(exit_code)
