"""Copy human labels from Databricks into the local warehouse (insert-only, idempotent).

    python jobs/pull_labels.py --profile donegal

Labels are written on Databricks by the labelling app; local validation needs the same rows
to reproduce the Databricks validation version.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from ireland_rsi.contracts import TABLE_SCHEMAS
from ireland_rsi.profiles import load_profile, open_stores


def main() -> int:
    from databricks.sdk import WorkspaceClient

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="donegal")
    parser.add_argument("--cli-profile", default="ireland-rsi")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    profile = load_profile(root, args.profile)
    _, tables, _ = open_stores(profile, "local")
    w = WorkspaceClient(profile=args.cli_profile)
    warehouse = next(iter(w.warehouses.list())).id
    cols = TABLE_SCHEMAS["silver.link_labels"].names
    # Timestamps as UTC text with microseconds: the default JSON rendering drops them.
    select = ", ".join("date_format(to_utc_timestamp(labelled_at, current_timezone()), "
                       "'yyyy-MM-dd HH:mm:ss.SSSSSS') AS labelled_at" if c == "labelled_at"
                       else c for c in cols)
    r = w.statement_execution.execute_statement(
        statement=f"SELECT {select} FROM {profile.catalog}.silver.link_labels",
        warehouse_id=warehouse, wait_timeout="50s")
    if r.status.error:
        raise RuntimeError(r.status.error.message)
    df = pd.DataFrame(r.result.data_array or [], columns=cols)
    df["labelled_at"] = pd.to_datetime(df.labelled_at, utc=True)
    inserted = tables.insert_new("silver.link_labels", df)
    print(f"{len(df)} labels on Databricks, {inserted} new locally")
    return 0


if __name__ == "__main__":
    if exit_code := main():
        sys.exit(exit_code)
