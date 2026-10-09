"""Content-addressed object storage and insert-only table stores.

ObjectStore keeps untouched bytes under paths derived from their SHA-256, on a local folder
or a Unity Catalog volume (both are POSIX paths). TableStore implementations append rows
whose key is not already present, so re-running a stage cannot duplicate data:

* DuckDBTableStore: local development and tests.
* DeltaTableStore: Databricks, writing Delta tables in a Unity Catalog catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Protocol

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ireland_rsi.contracts import TABLE_KEYS, TABLE_SCHEMAS


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectStore:
    """Immutable, content-addressed byte storage."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def object_path(self, digest: str, suffix: str) -> Path:
        return self.root / "objects" / "sha256" / digest[:2] / f"{digest}{suffix}"

    def put(self, data: bytes, suffix: str) -> tuple[str, Path, bool]:
        """Store bytes; returns (sha256, path, newly_written). Existing objects are reused."""
        digest = sha256_hex(data)
        path = self.object_path(digest, suffix)
        if path.exists():
            if sha256_hex(path.read_bytes()) == digest:
                return digest, path, False
            path.unlink()  # damaged earlier write; the content address proves it is wrong
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if sha256_hex(path.read_bytes()) != digest:
            path.unlink()
            raise OSError(f"verification failed after writing {path}")
        return digest, path, True

    def get(self, digest: str, suffix: str) -> bytes:
        data = self.object_path(digest, suffix).read_bytes()
        if sha256_hex(data) != digest:
            raise OSError(f"stored object {digest}{suffix} does not match its checksum")
        return data

    def put_record(self, relative_path: str, record: dict,
                   tolerate_extra: tuple[str, ...] = ()) -> Path:
        """Write a JSON evidence record once. An existing record must agree on every key of
        `record`; keys listed in `tolerate_extra` may exist only in the older record."""
        path = self.root / relative_path
        payload = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            current = json.loads(payload)
            extra = set(existing) - set(current)
            if extra - set(tolerate_extra) or any(existing.get(k) != v
                                                 for k, v in current.items()):
                raise FileExistsError(f"{path} exists with different content")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return path


def to_arrow(table: str, df: pd.DataFrame) -> pa.Table:
    """Conform a frame to its declared schema; extra or missing columns are errors."""
    schema = TABLE_SCHEMAS[table]
    if list(df.columns) != schema.names:
        raise ValueError(f"{table}: columns {list(df.columns)} != schema {schema.names}")
    keys = list(TABLE_KEYS[table])
    if df.duplicated(subset=keys).any():
        raise ValueError(f"{table}: duplicate keys in incoming rows")
    return pa.Table.from_pandas(df, schema=schema, preserve_index=False)


class TableStore(Protocol):
    def qualify(self, table: str) -> str: ...
    def exists(self, table: str) -> bool: ...
    def insert_new(self, table: str, df: pd.DataFrame) -> int: ...
    def query(self, sql: str) -> pd.DataFrame: ...


class DuckDBTableStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        import duckdb

        self.con = duckdb.connect(str(path))

    def qualify(self, table: str) -> str:
        return table

    def exists(self, table: str) -> bool:
        schema, name = table.split(".")
        return bool(self.con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?", [schema, name]).fetchone()[0])

    def insert_new(self, table: str, df: pd.DataFrame) -> int:
        incoming = to_arrow(table, df)
        schema = table.split(".")[0]
        self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        self.con.register("_incoming", incoming)
        try:
            self.con.execute(
                f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM _incoming LIMIT 0")
            cond = " AND ".join(f"t.{k} IS NOT DISTINCT FROM i.{k}" for k in TABLE_KEYS[table])
            before = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            self.con.execute(
                f"INSERT INTO {table} SELECT i.* FROM _incoming i "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {cond})")
            return self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] - before
        finally:
            self.con.unregister("_incoming")

    def query(self, sql: str) -> pd.DataFrame:
        return self.con.execute(sql).df()


class DeltaTableStore:
    """Insert-only Delta tables in Unity Catalog, staged through Parquet in a volume.

    Staging through a volume file avoids sending large frames over Spark Connect.
    """

    def __init__(self, spark, catalog: str, staging_dir: str | Path) -> None:
        self.spark = spark
        self.catalog = catalog
        self.staging_dir = Path(staging_dir)

    def qualify(self, table: str) -> str:
        return f"{self.catalog}.{table}"

    def exists(self, table: str) -> bool:
        return self.spark.catalog.tableExists(self.qualify(table))

    def insert_new(self, table: str, df: pd.DataFrame) -> int:
        incoming = to_arrow(table, df)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        path = self.staging_dir / f"{uuid.uuid4().hex}.parquet"
        pq.write_table(incoming, path)
        target = self.qualify(table)
        try:
            staged = self.spark.read.parquet(str(path))
            if not self.exists(table):
                try:
                    staged.limit(0).write.format("delta").saveAsTable(target)
                except Exception as exc:  # a parallel task created it first
                    if "TABLE_OR_VIEW_ALREADY_EXISTS" not in str(exc):
                        raise
            view = f"_incoming_{uuid.uuid4().hex[:8]}"
            staged.createOrReplaceTempView(view)
            cond = " AND ".join(f"t.{k} <=> i.{k}" for k in TABLE_KEYS[table])
            merge = (f"MERGE INTO {target} t USING {view} i ON {cond} "
                     "WHEN NOT MATCHED THEN INSERT *")
            return self._merge_with_retry(merge)
        finally:
            os.remove(path)

    def _merge_with_retry(self, merge_sql: str, attempts: int = 5) -> int:
        """Run an insert-only MERGE, retrying Delta optimistic-concurrency conflicts from
        parallel tasks. Returns the inserted row count from the MERGE metrics."""
        import time

        for attempt in range(1, attempts + 1):
            try:
                row = self.spark.sql(merge_sql).collect()[0].asDict()
                return int(row.get("num_inserted_rows", 0))
            except Exception as exc:
                text = str(exc)
                conflict = "Concurrent" in text or "DELTA_CONCURRENT" in text
                if not conflict or attempt == attempts:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    def query(self, sql: str) -> pd.DataFrame:
        return self.spark.sql(sql).toPandas()


# ---------------------------------------------------------------------------
# Stage registry
# ---------------------------------------------------------------------------


def record_stage(tables: TableStore, stage: str, snapshot_id: str, output_version: str,
                 run_id: str, completed_at) -> None:
    tables.insert_new("silver.stage_runs", pd.DataFrame(
        [(stage, snapshot_id, output_version, run_id, completed_at)],
        columns=TABLE_SCHEMAS["silver.stage_runs"].names))


def latest_stage_version(tables: TableStore, stage: str, snapshot_id: str) -> str | None:
    if not tables.exists("silver.stage_runs"):
        return None
    df = tables.query(
        f"SELECT output_version FROM {tables.qualify('silver.stage_runs')} "
        f"WHERE stage = '{stage}' AND snapshot_id = '{snapshot_id}' "
        "ORDER BY completed_at DESC LIMIT 1")
    return None if df.empty else str(df.iloc[0, 0])
