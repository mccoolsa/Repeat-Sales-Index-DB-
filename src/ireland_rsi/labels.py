"""Independent labels and a leakage-safe evaluation design.

* A frozen, stratified sample of candidate pairs for human review, with each pair's
  inclusion probability so precision can be weighted back to the population later.
* Eircode weak labels, kept apart from the human labels and never shown to reviewers.
* Splits assigned per property group (connected sales), with a later-period test set, so
  one property cannot appear in both development and test data.

Reviewers see addresses, dates and the coarse source description only. Prices and Eircodes
are hidden so that labels do not depend on price behaviour (which the index measures) or on
Eircodes (which the address-only evaluation must not use).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ireland_rsi.contracts import TABLE_SCHEMAS
from ireland_rsi.storage import TableStore, latest_stage_version, sha256_hex

EIRCODE_IN_TEXT = re.compile(
    r"\b(?:[ACDEFHKNPRTVWXY]\d{2}|D6W)\s?[0-9ACDEFHKNPRTVWXY]{4}\b", re.IGNORECASE)
STRATA_ORDER = ("exact_address", "same_number_street", "apartment_unit_conflict",
                "rural_unnumbered", "similar_street_same_number", "neighbour_numbers")


def mask_eircode(text: str | None) -> str | None:
    """Remove Eircode-shaped tokens from free text (address-only evaluation track)."""
    if not isinstance(text, str):
        return text
    return " ".join(EIRCODE_IN_TEXT.sub(" ", text).split()).strip(" ,") or None


@dataclass(frozen=True)
class LabelConfig:
    raw: dict
    digest: str

    @classmethod
    def load(cls, path: str | Path) -> LabelConfig:
        data = Path(path).read_bytes()
        return cls(yaml.safe_load(data.decode("utf-8")),
                   sha256_hex(data + Path(__file__).read_bytes())[:8])

    def sample_version(self, snapshot_id: str, address_version: str,
                       rule_set_version: str) -> str:
        data = sha256_hex(
            f"{self.digest}|{snapshot_id}|{address_version}|{rule_set_version}".encode())[:8]
        return f"{self.raw['sample_name']}+{data}"


# ---------------------------------------------------------------------------
# Linkage frame and candidate pairs
# ---------------------------------------------------------------------------


def linkage_frame(tables: TableStore, snapshot_id: str, address_version: str,
                  rule_set_version: str) -> pd.DataFrame:
    """One row per linkage-eligible occurrence with its address features."""
    s, a = tables.qualify("silver.sales"), tables.qualify("silver.address_features")
    df = tables.query(
        "SELECT s.sale_id, s.raw_row_id, s.sale_date, s.county, "
        "CASE WHEN s.eircode_valid THEN s.eircode END AS eircode, s.dwelling_status, "
        "s.size_band, a.address_display, a.address_norm, a.street_core, a.primary_number, "
        "a.unit_id, a.house_number, a.house_range, a.locality_id, a.townland_id "
        f"FROM {s} s JOIN {a} a ON s.snapshot_id = a.snapshot_id AND s.raw_row_id = a.raw_row_id "
        f"WHERE s.snapshot_id = '{snapshot_id}' AND s.rule_set_version = '{rule_set_version}' "
        f"AND a.address_version = '{address_version}' AND s.linkage_eligible")
    df["address_display"] = df.address_display.map(mask_eircode)
    df["address_norm"] = df.address_norm.map(mask_eircode)
    df["sale_date"] = pd.to_datetime(df.sale_date).dt.date
    return df.sort_values("sale_id").reset_index(drop=True)


def _block_pairs(df: pd.DataFrame, keys: list[str], max_block: int,
                 mask: pd.Series | None = None) -> tuple[np.ndarray, np.ndarray, int]:
    """All within-block index pairs (i < j); blocks above max_block are skipped."""
    sub = df if mask is None else df[mask]
    left, right, skipped = [], [], 0
    for idx in sub.groupby(keys, dropna=True, sort=False).indices.values():
        n = len(idx)
        if n < 2:
            continue
        if n > max_block:
            skipped += 1
            continue
        rows = sub.index.to_numpy()[idx]
        i, j = np.triu_indices(n, k=1)
        left.append(rows[i])
        right.append(rows[j])
    if not left:
        return np.empty(0, int), np.empty(0, int), skipped
    return np.concatenate(left), np.concatenate(right), skipped


def _jaccard(a: str, b: str) -> float:
    x, y = set(str(a).split()), set(str(b).split())
    return len(x & y) / len(x | y) if x | y else 0.0


def candidate_pairs(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """Mutually exclusive strata of candidate pairs (two different sales, different dates)."""
    max_block = cfg["max_block_size"]
    numbered = df.primary_number.notna()
    has_unit = df.unit_id.notna()
    unnumbered = df.primary_number.isna() & df.house_range.isna()
    house_only = df.house_number.notna() & ~has_unit
    specs = {
        "exact_address": (["county", "address_norm"], None),
        "same_number_street": (["county", "street_core", "primary_number"], numbered),
        "apartment_unit_conflict": (["county", "street_core"], has_unit),
        "rural_unnumbered": (["county", "street_core"], unnumbered),
        "similar_street_same_number": (["county", "locality_id", "primary_number"], numbered),
        "neighbour_numbers": (["county", "street_core"], house_only),
    }
    frames, skipped = [], {}
    dates = df.sale_date.to_numpy()
    for rank, name in enumerate(STRATA_ORDER):
        keys, mask = specs[name]
        i, j, skipped[name] = _block_pairs(df, keys, max_block, mask)
        keep = dates[i] != dates[j]
        if name == "same_number_street":
            keep &= df.address_norm.to_numpy()[i] != df.address_norm.to_numpy()[j]
        elif name == "apartment_unit_conflict":
            keep &= df.unit_id.to_numpy()[i] != df.unit_id.to_numpy()[j]
        elif name == "rural_unnumbered":
            keep &= df.address_norm.to_numpy()[i] != df.address_norm.to_numpy()[j]
        elif name == "similar_street_same_number":
            sa, sb = df.street_core.to_numpy()[i], df.street_core.to_numpy()[j]
            differ = sa != sb
            sim = np.array([_jaccard(x, y) >= cfg["similar_street_min_jaccard"] if d else False
                            for x, y, d in zip(sa, sb, differ, strict=True)], dtype=bool)
            keep &= sim
        elif name == "neighbour_numbers":
            na = pd.to_numeric(df.house_number.to_numpy()[i], errors="coerce")
            nb = pd.to_numeric(df.house_number.to_numpy()[j], errors="coerce")
            gap = np.abs(na - nb)
            keep &= (gap >= 1) & (gap <= 2)
        i, j = i[keep], j[keep]
        # Order each pair chronologically: a is the earlier sale.
        swap = dates[i] > dates[j]
        a, b = np.where(swap, j, i), np.where(swap, i, j)
        frames.append(pd.DataFrame({"ia": a, "ib": b, "stratum": name, "rank": rank}))
    pairs = pd.concat(frames, ignore_index=True)
    pairs = pairs.drop_duplicates(["ia", "ib"], keep="first").reset_index(drop=True)
    return pairs, {"skipped_large_blocks": skipped}


# ---------------------------------------------------------------------------
# Sampling, weak labels and splits
# ---------------------------------------------------------------------------


def _eircode_status(ea, eb) -> tuple[str, str | None]:
    has_a, has_b = isinstance(ea, str), isinstance(eb, str)
    if has_a and has_b:
        return "both", "eircode_same" if ea == eb else "eircode_differs"
    return ("one" if has_a or has_b else "none"), None


def _groups(sale_a: list[str], sale_b: list[str]) -> list[str]:
    """Connected components of sampled pairs over sale IDs (union-find)."""
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in zip(sale_a, sale_b, strict=True):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return ["g_" + sha256_hex(find(a).encode())[:16] for a in sale_a]


def draw_sample(df: pd.DataFrame, pairs: pd.DataFrame, cfg: dict,
                sample_version: str) -> pd.DataFrame:
    rng = np.random.default_rng(cfg["seed"])
    cutoff = cfg["era_cutoff"]
    dates_a = df.sale_date.to_numpy()[pairs.ia]
    pairs = pairs.assign(era=np.where(dates_a < cutoff, "pre2015", "from2015"))
    chosen = []
    for name in STRATA_ORDER:
        quota = cfg["strata"][name]["quota"]
        cells = {era: pairs[(pairs.stratum == name) & (pairs.era == era)]
                 for era in ("pre2015", "from2015")}
        want = {"pre2015": quota // 2, "from2015": quota - quota // 2}
        short = {e: max(0, want[e] - len(c)) for e, c in cells.items()}
        want = {"pre2015": want["pre2015"] + short["from2015"],
                "from2015": want["from2015"] + short["pre2015"]}
        for era, cell in cells.items():
            n = min(want[era], len(cell))
            if n == 0:
                continue
            take = cell.iloc[np.sort(rng.choice(len(cell), size=n, replace=False))]
            chosen.append(take.assign(cell_size=len(cell), cell_sampled=n,
                                      sampling_prob=n / len(cell)))
    sample = pd.concat(chosen, ignore_index=True)

    a, b = df.iloc[sample.ia].reset_index(drop=True), df.iloc[sample.ib].reset_index(drop=True)
    status = [_eircode_status(x, y) for x, y in zip(a.eircode, b.eircode, strict=True)]
    out = pd.DataFrame({
        "sample_version": sample_version,
        "pair_id": ["p_" + sha256_hex(f"{sample_version}|{x}|{y}".encode())[:16]
                    for x, y in zip(a.sale_id, b.sale_id, strict=True)],
        "stratum": sample.stratum, "era": sample.era, "cell_size": sample.cell_size,
        "cell_sampled": sample.cell_sampled, "sampling_prob": sample.sampling_prob,
        "eircode_status": [s[0] for s in status], "weak_label": [s[1] for s in status],
        "county": a.county,
        "sale_id_a": a.sale_id, "sale_id_b": b.sale_id,
        "raw_row_id_a": a.raw_row_id, "raw_row_id_b": b.raw_row_id,
        "date_a": a.sale_date, "date_b": b.sale_date,
        "address_a": a.address_display, "address_b": b.address_display,
        "status_a": a.dwelling_status, "status_b": b.dwelling_status,
        "size_a": a.size_band, "size_b": b.size_band,
    })
    out["group_id"] = _groups(out.sale_id_a.tolist(), out.sale_id_b.tolist())
    splits = cfg["splits"]
    temporal = out.groupby("group_id").date_b.transform("max") >= splits["temporal_test_from"]
    dev = out.group_id.map(lambda g: int(g[2:10], 16) / 0xFFFFFFFF < splits["dev_share"])
    out["split"] = np.where(temporal, "test_temporal", np.where(dev, "dev", "test"))
    out["double_review"] = rng.random(len(out)) < cfg["double_review_share"]
    out["sample_order"] = rng.permutation(len(out)).astype("int32")
    return out[TABLE_SCHEMAS["silver.label_sample"].names].sort_values(
        "sample_order").reset_index(drop=True)


def weak_labels(df: pd.DataFrame, snapshot_id: str, rule_set_version: str,
                max_code_sales: int = 10) -> pd.DataFrame:
    """Pairs of different sales sharing a valid Eircode, with consistency checks.

    A shared Eircode is weak positive evidence. Different Eircodes are never treated as
    negative evidence (codes can be wrong or change).
    """
    with_code = df[df.eircode.notna()]
    i, j, _ = _block_pairs(with_code, ["eircode"], max_block=10_000)
    a, b = df.loc[i].reset_index(drop=True), df.loc[j].reset_index(drop=True)
    n = a.eircode.map(with_code.eircode.value_counts()).to_numpy()
    reasons = []
    for pa_, pb, ca, cb, k in zip(a.primary_number, b.primary_number, a.county, b.county, n,
                                  strict=True):
        why = []
        if isinstance(pa_, str) and isinstance(pb, str) and pa_ != pb:
            why.append("number_conflict")
        if ca != cb:
            why.append("county_conflict")
        if k > max_code_sales:
            why.append("code_reused_widely")
        reasons.append(",".join(why) or None)
    swap = a.sale_date > b.sale_date
    first = np.where(swap, b.sale_id, a.sale_id)
    second = np.where(swap, a.sale_id, b.sale_id)
    return pd.DataFrame({
        "snapshot_id": snapshot_id, "rule_set_version": rule_set_version,
        "sale_id_a": first, "sale_id_b": second,
        "eircode": a.eircode, "weak_label": ["eircode_conflict" if r else "eircode_match"
                                             for r in reasons],
        "conflict_reason": reasons, "code_sales": n.astype("int32"),
    })


def sample_summary(pairs: pd.DataFrame, sample: pd.DataFrame, frame_info: dict) -> dict:
    return {
        "frame_pairs_by_stratum": pairs.stratum.value_counts().to_dict(),
        "skipped_large_blocks": frame_info["skipped_large_blocks"],
        "sampled_by_stratum_era": {f"{k[0]}|{k[1]}": int(v) for k, v in
                                   sample.groupby(["stratum", "era"]).size().items()},
        "split": sample.split.value_counts().to_dict(),
        "eircode_status": sample.eircode_status.value_counts().to_dict(),
        "double_review": int(sample.double_review.sum()),
        "groups": int(sample.group_id.nunique()),
        "groups_spanning_splits": int((sample.groupby("group_id").split.nunique() > 1).sum()),
    }


def run_label_sample(tables: TableStore, scope: str, config: LabelConfig) -> dict:
    from ireland_rsi.ingest import active_snapshot

    snapshot = active_snapshot(tables, scope)
    address_version = latest_stage_version(tables, "addresses", snapshot)
    rule_set = latest_stage_version(tables, "clean", snapshot)
    if not (address_version and rule_set):
        raise RuntimeError("address features and clean sales must exist for the snapshot")
    df = linkage_frame(tables, snapshot, address_version, rule_set)
    pairs, info = candidate_pairs(df, config.raw)
    version = config.sample_version(snapshot, address_version, rule_set)
    sample = draw_sample(df, pairs, config.raw, version)
    weak = weak_labels(df, snapshot, rule_set)
    counts = {
        "sample_inserted": tables.insert_new("silver.label_sample", sample),
        "weak_labels_inserted": tables.insert_new("silver.weak_labels", weak),
    }
    return {"snapshot_id": snapshot, "address_version": address_version,
            "rule_set_version": rule_set, "sample_version": version,
            "linkage_frame_rows": len(df), "row_counts": counts,
            "weak_labels": weak.weak_label.value_counts().to_dict(),
            "weak_conflicts": weak.conflict_reason.value_counts().to_dict(),
            "summary": sample_summary(pairs, sample, info)}


# ---------------------------------------------------------------------------
# Reading labels back
# ---------------------------------------------------------------------------

LABEL_VALUES = ("match", "non_match", "uncertain")


def current_labels(labels: pd.DataFrame) -> pd.DataFrame:
    """Latest label per (pair, reviewer); earlier labels stay in the table as history."""
    if labels.empty:
        return labels
    latest = labels.sort_values("labelled_at").groupby(
        ["sample_version", "pair_id", "reviewer"]).tail(1)
    bad = set(latest.label) - set(LABEL_VALUES)
    if bad:
        raise ValueError(f"unknown label values {bad}")
    return latest.reset_index(drop=True)


def summarise_labels(sample: pd.DataFrame, labels: pd.DataFrame) -> dict:
    """Progress and agreement. Uncertain labels are counted but are not binary outcomes."""
    cur = current_labels(labels)
    primary = cur.sort_values("labelled_at").groupby("pair_id").head(1)
    binary = primary[primary.label != "uncertain"]
    reviewers = cur.groupby("pair_id").reviewer.nunique()
    double = cur[cur.pair_id.isin(reviewers[reviewers > 1].index)]
    agreement = None
    if not double.empty:
        agreement = round(float((double.groupby("pair_id").label.nunique() == 1).mean()), 3)
    return {
        "sample_pairs": len(sample),
        "labelled_pairs": int(primary.pair_id.nunique()),
        "by_label": primary.label.value_counts().to_dict(),
        "binary_pairs": len(binary),
        "uncertain_pairs": int((primary.label == "uncertain").sum()),
        "double_reviewed_pairs": int(double.pair_id.nunique()),
        "reviewer_agreement": agreement,
    }

