"""Pipeline profiles: which source, scope, county filter and storage a run uses.

The same code serves the national pipeline and the Donegal pilot; a profile in
config/profiles/<name>.yml selects between them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ireland_rsi.ingest import PromotionPolicy
from ireland_rsi.storage import DeltaTableStore, DuckDBTableStore, ObjectStore


@dataclass(frozen=True)
class Profile:
    name: str
    root: Path
    source: dict
    scope: str
    county_filter: str | None
    policy: PromotionPolicy
    storage: dict
    labels_config: Path
    mlflow_experiment: str

    @property
    def catalog(self) -> str:
        return self.storage["databricks"]["catalog"]


def load_profile(root: str | Path, name: str = "national") -> Profile:
    root = Path(root)
    data = yaml.safe_load((root / "config" / "profiles" / f"{name}.yml").read_text(
        encoding="utf-8"))
    sources = yaml.safe_load((root / "config" / "sources.yml").read_text(encoding="utf-8"))
    return Profile(
        name=data["name"], root=root, source=sources["sources"][data["source"]],
        scope=data["scope"], county_filter=data.get("county_filter"),
        policy=PromotionPolicy(authoritative_scope=data["scope"], **data["promotion_policy"]),
        storage=data["storage"], labels_config=root / data["labels"],
        mlflow_experiment=data["mlflow_experiment"])


def open_stores(profile: Profile, target: str):
    """(objects, tables, spark) for a local or Databricks run of this profile."""
    if target == "local":
        paths = profile.storage["local"]
        warehouse = profile.root / paths["warehouse"]
        warehouse.parent.mkdir(parents=True, exist_ok=True)
        return ObjectStore(profile.root / paths["objects"]), DuckDBTableStore(warehouse), None
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    paths = profile.storage["databricks"]
    return (ObjectStore(paths["objects"]),
            DeltaTableStore(spark, paths["catalog"], paths["staging"]), spark)


def mlflow_experiment_path(profile: Profile, spark) -> str | None:
    if spark is None:
        return None
    user = spark.sql("SELECT current_user()").first()[0]
    return f"/Users/{user}/{profile.mlflow_experiment}"
