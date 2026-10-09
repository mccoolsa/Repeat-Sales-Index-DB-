"""Constrained property histories from scored links.

High pairwise scores do not guarantee a valid cluster: if A-B and B-C are accepted, plain
connected components merge A and C even when they are different apartments. Here accepted
edges are added strongest first, and an edge is refused if it would put conflicting facts
into one property:

* different unit IDs or primary numbers,
* different valid Eircodes,
* two sales on the same day at different prices,
* more sales than a single home plausibly has.

Refused edges are kept for review. Property IDs are generated (not official identifiers):
a property is named after its earliest sale, so it keeps its ID as later sales join.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from ireland_rsi.storage import sha256_hex


@dataclass(frozen=True)
class EntityConfig:
    raw: dict
    digest: str

    @classmethod
    def load(cls, path: str | Path) -> EntityConfig:
        # The version covers the config and all identity/pairing logic (entities.py and
        # pairs.py, which holds the address-specificity rule).
        data = Path(path).read_bytes()
        logic = Path(__file__).read_bytes() + (Path(__file__).parent / "pairs.py").read_bytes()
        return cls(yaml.safe_load(data.decode("utf-8")), sha256_hex(data + logic)[:8])

    def version(self, model_version: str) -> str:
        return f"ent-{sha256_hex(f'{self.digest}|{model_version}'.encode())[:12]}"


@dataclass
class _Cluster:
    members: list[str]
    numbers: set = field(default_factory=set)
    eircodes: set = field(default_factory=set)
    day_prices: dict = field(default_factory=dict)  # sale_date -> set of prices


class ConstrainedClusters:
    """Union-find with cannot-link constraints checked on every merge."""

    def __init__(self, sales: pd.DataFrame, constraints: dict) -> None:
        self.c = constraints
        self.parent: dict[str, str] = {}
        self.clusters: dict[str, _Cluster] = {}
        for rec in sales.itertuples(index=False):
            cl = _Cluster([rec.sale_id])
            if isinstance(rec.primary_number, str):
                cl.numbers.add(rec.primary_number)
            if isinstance(rec.eircode, str):
                cl.eircodes.add(rec.eircode)
            if rec.sale_date is not None and pd.notna(rec.sale_date):
                cl.day_prices[rec.sale_date] = {rec.price_key}
            self.parent[rec.sale_id] = rec.sale_id
            self.clusters[rec.sale_id] = cl

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def conflict(self, a: _Cluster, b: _Cluster) -> str | None:
        c = self.c
        if len(a.members) + len(b.members) > c["max_sales_per_property"]:
            return "too_many_sales"
        if c["forbid_unit_conflict"] and a.numbers and b.numbers and a.numbers != b.numbers:
            return "unit_or_number_conflict"
        if c["forbid_eircode_conflict"] and a.eircodes and b.eircodes and a.eircodes != b.eircodes:
            return "eircode_conflict"
        if c["forbid_same_day_price_conflict"]:
            for day, prices in a.day_prices.items():
                other = b.day_prices.get(day)
                if other and other != prices:
                    return "same_day_price_conflict"
        return None

    def try_union(self, x: str, y: str) -> str | None:
        """Merge the clusters of x and y unless that breaks a constraint; return the reason."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return None
        a, b = self.clusters[rx], self.clusters[ry]
        reason = self.conflict(a, b)
        if reason:
            return reason
        root, child = (rx, ry) if rx < ry else (ry, rx)
        keep, gone = self.clusters[root], self.clusters.pop(child)
        keep.members += gone.members
        keep.numbers |= gone.numbers
        keep.eircodes |= gone.eircodes
        for day, prices in gone.day_prices.items():
            keep.day_prices.setdefault(day, set()).update(prices)
        self.parent[child] = root
        return None


def resolve_entities(sales: pd.DataFrame, edges: pd.DataFrame, config: EntityConfig,
                     entity_version: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Property membership for every sale, and the accepted edges refused by constraints.

    `sales`: sale_id, sale_date, primary_number, eircode, price_key and optionally
    address_specific (one row per occurrence).
    `edges`: sale_id_l, sale_id_r, match_weight, match_probability (accepted links only).
    """
    clusters = ConstrainedClusters(sales, config.raw["constraints"])
    attach: dict[str, float] = {}
    refused = []
    ordered = edges.sort_values(["match_weight", "sale_id_l", "sale_id_r"],
                                ascending=[False, True, True])
    specific = (dict(zip(sales.sale_id, sales.address_specific, strict=True))
                if "address_specific" in sales else {})
    eircodes = dict(zip(sales.sale_id, sales.eircode, strict=True))
    require = config.raw["constraints"].get("require_specific_address", False)
    for e in ordered.itertuples(index=False):
        reason = None
        if require and not (specific.get(e.sale_id_l, True) and specific.get(e.sale_id_r, True)):
            ea, eb = eircodes.get(e.sale_id_l), eircodes.get(e.sale_id_r)
            if not (isinstance(ea, str) and ea == eb):
                reason = "non_specific_address"
        reason = reason or clusters.try_union(e.sale_id_l, e.sale_id_r)
        if reason:
            refused.append((entity_version, e.sale_id_l, e.sale_id_r, float(e.match_probability),
                            float(e.match_weight), reason))
        else:
            for s in (e.sale_id_l, e.sale_id_r):
                attach[s] = max(attach.get(s, float("-inf")), float(e.match_weight))

    dates = dict(zip(sales.sale_id, sales.sale_date, strict=True))
    rows = []
    for cl in clusters.clusters.values():
        earliest = min(cl.members, key=lambda s: (dates[s], s))
        property_id = "prop_" + sha256_hex(earliest.encode())[:16]
        for s in cl.members:
            rows.append((entity_version, property_id, s, len(cl.members),
                         attach.get(s) if len(cl.members) > 1 else None))
    membership = pd.DataFrame(rows, columns=["entity_version", "property_id", "sale_id",
                                             "n_sales", "attach_weight"])
    rejected = pd.DataFrame(refused, columns=["entity_version", "sale_id_l", "sale_id_r",
                                              "match_probability", "match_weight", "reason"])
    return membership, rejected


def crosswalk(old: pd.DataFrame, new: pd.DataFrame, from_version: str,
              to_version: str) -> pd.DataFrame:
    """How properties map between two entity versions (same / merge / split / new)."""
    j = old[["property_id", "sale_id"]].merge(new[["property_id", "sale_id"]], on="sale_id",
                                              how="outer", suffixes=("_old", "_new"))
    overlap = (j.dropna().groupby(["property_id_old", "property_id_new"]).size()
               .rename("overlap_sales").reset_index())
    old_fan = overlap.groupby("property_id_old").property_id_new.transform("nunique")
    new_fan = overlap.groupby("property_id_new").property_id_old.transform("nunique")
    overlap["relation"] = "same"
    overlap.loc[old_fan > 1, "relation"] = "split"
    overlap.loc[new_fan > 1, "relation"] = "merge"
    overlap.loc[(old_fan > 1) & (new_fan > 1), "relation"] = "merge_and_split"
    overlap.insert(0, "to_version", to_version)
    overlap.insert(0, "from_version", from_version)
    return overlap


def entity_summary(membership: pd.DataFrame, rejected: pd.DataFrame) -> dict:
    sizes = membership.groupby("property_id").size()
    return {
        "sales": len(membership),
        "properties": int(sizes.size),
        "properties_with_repeat_sales": int((sizes > 1).sum()),
        "sales_in_multi_sale_properties": int(sizes[sizes > 1].sum()),
        "size_distribution": {str(k): int(v) for k, v in
                              sizes.value_counts().sort_index().items()},
        "edges_refused": len(rejected),
        "refusal_reasons": rejected.reason.value_counts().to_dict(),
    }

