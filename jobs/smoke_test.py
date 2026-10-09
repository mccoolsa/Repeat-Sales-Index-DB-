"""Databricks serverless smoke test (decisions D-009, D-013).

Checks, each reported independently:
  1. outbound HTTPS to the PSRA, CSO and PyPI hosts
  2. Splink + DuckDB linkage on a toy dataset
  3. Delta write/read round trip in Unity Catalog (skipped when Spark is unavailable)
  4. file write/read in the bronze raw volume (skipped when the volume is not mounted)

Runs locally (checks 1-2) or as a serverless job task (all checks). Exits non-zero if any
attempted check fails.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

RESULTS: dict[str, dict] = {}

HOSTS = {
    "psra": "https://www.propertypriceregister.ie/website/npsra/pprweb.nsf/page/ppr-home-en",
    "cso": "https://ws.cso.ie/public/api.restful/PxStat.Data.Cube_API.ReadMetadata/HPQ01/JSON-stat/2.0/en",
    "pypi": "https://pypi.org/simple/splink/",
}
CATALOG = os.environ.get("RSI_CATALOG", "ireland_rsi")
VOLUME_DIR = f"/Volumes/{CATALOG}/bronze/raw"


def record(name: str, ok: bool | None, **detail) -> None:
    RESULTS[name] = {"ok": ok, **detail}
    status = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    print(f"[{status}] {name} {json.dumps(detail, default=str)}", flush=True)


def check_hosts() -> None:
    for name, url in HOSTS.items():
        start = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ireland-rsi-smoke"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                record(f"http_{name}", True, status=resp.status,
                       seconds=round(time.monotonic() - start, 2))
        except Exception as exc:  # noqa: BLE001 - report every failure mode
            record(f"http_{name}", False, error=f"{type(exc).__name__}: {exc}")


def check_splink() -> None:
    try:
        import duckdb
        import pandas as pd
        import splink
        import splink.comparison_library as cl
        from splink import Linker, SettingsCreator, block_on
        from splink.backends.duckdb import DuckDBAPI

        df = pd.DataFrame(
            [
                (1, "apt 4 the grove", "letterkenny", "donegal"),
                (2, "apartment 4 the grove", "letterkenny", "donegal"),
                (3, "apt 14 the grove", "letterkenny", "donegal"),
                (4, "11 glendale manor", "letterkenny", "donegal"),
                (5, "11 glendale manor", "letterkenny", "donegal"),
                (6, "2 main street", "buncrana", "donegal"),
            ],
            columns=["unique_id", "street_part", "locality", "county"],
        )
        settings = SettingsCreator(
            link_type="dedupe_only",
            comparisons=[
                cl.JaroWinklerAtThresholds("street_part", [0.95, 0.85]),
                cl.ExactMatch("locality"),
            ],
            blocking_rules_to_generate_predictions=[block_on("county")],
        )
        # Splink 5: tables are registered with the backend before building the Linker.
        linker = Linker(DuckDBAPI().register(df), settings)
        linker.training.estimate_u_using_random_sampling(max_pairs=1e4)
        predictions = linker.inference.predict().as_pandas_dataframe()
        record("splink_duckdb", len(predictions) == 15, splink=splink.__version__,
               duckdb=duckdb.__version__, candidate_pairs=len(predictions))
    except Exception as exc:  # noqa: BLE001
        record("splink_duckdb", False, error=f"{type(exc).__name__}: {exc}")


def get_spark():
    try:
        from pyspark.sql import SparkSession

        return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
    except Exception:  # noqa: BLE001 - absent locally
        return None


def check_delta(spark) -> None:
    if spark is None:
        record("delta_roundtrip", None, reason="no Spark session")
        return
    table = f"{CATALOG}.default.smoke_test"
    try:
        spark.createDataFrame([(1, "a"), (2, "b")], "id INT, v STRING").write.mode(
            "overwrite").saveAsTable(table)
        n = spark.table(table).count()
        fmt = spark.sql(f"DESCRIBE DETAIL {table}").collect()[0]["format"]
        spark.sql(f"DROP TABLE {table}")
        record("delta_roundtrip", n == 2 and fmt == "delta", rows=n, format=fmt,
               spark=spark.version)
    except Exception as exc:  # noqa: BLE001
        record("delta_roundtrip", False, error=f"{type(exc).__name__}: {exc}")


def check_volume() -> None:
    if not os.path.isdir(VOLUME_DIR):
        record("volume_io", None, reason=f"{VOLUME_DIR} not mounted")
        return
    path = f"{VOLUME_DIR}/_smoke_test.txt"
    try:
        with open(path, "wb") as fh:
            fh.write(b"\x80 cp1252 euro\r\n")
        with open(path, "rb") as fh:
            same = fh.read() == b"\x80 cp1252 euro\r\n"
        os.remove(path)
        record("volume_io", same, path=path, bytes_preserved=same)
    except Exception as exc:  # noqa: BLE001
        record("volume_io", False, error=f"{type(exc).__name__}: {exc}")


def main() -> None:
    print(f"python {sys.version.split()[0]}", flush=True)
    check_hosts()
    check_splink()
    check_delta(get_spark())
    check_volume()
    print("SMOKE_RESULTS " + json.dumps(RESULTS, default=str), flush=True)
    if any(r["ok"] is False for r in RESULTS.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
