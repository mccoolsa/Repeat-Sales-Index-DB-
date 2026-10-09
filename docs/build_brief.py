"""Build docs/project_brief.pdf: project explanation, brief and stage-by-stage breakdown.

    python docs/build_brief.py        (needs reportlab and the charts in reports/)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "project_brief.pdf"
REPORTS = ROOT / "reports"

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#5f6b7a")
ACCENT = colors.HexColor("#0f766e")
ACCENT_LIGHT = colors.HexColor("#e6f2f1")
AMBER = colors.HexColor("#b45309")
AMBER_LIGHT = colors.HexColor("#fdf3e7")
RULE = colors.HexColor("#d9dee4")
PANEL = colors.HexColor("#f5f7f9")
BRONZE = colors.HexColor("#a8643a")
SILVER = colors.HexColor("#7b8794")
GOLD = colors.HexColor("#b8860b")

W = A4[0] - 36 * mm   # text width

S = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=26, leading=31,
                            textColor=INK),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=13, leading=18,
                               textColor=MUTED),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20,
                         textColor=ACCENT, spaceBefore=4, spaceAfter=6),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=15,
                         textColor=INK, spaceBefore=8, spaceAfter=3),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.6, leading=13.6,
                           textColor=INK, spaceAfter=5),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8.2, leading=11,
                            textColor=MUTED, spaceAfter=3),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.6, leading=13.4,
                             textColor=INK, leftIndent=12, bulletIndent=2, spaceAfter=2.5),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.3, leading=10.6,
                           textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.3, leading=10.6,
                            textColor=INK),
    "head": ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=8.3, leading=10.6,
                           textColor=colors.white),
    "kpi_v": ParagraphStyle("kpi_v", fontName="Helvetica-Bold", fontSize=17, leading=20,
                            textColor=ACCENT, alignment=TA_CENTER),
    "kpi_l": ParagraphStyle("kpi_l", fontName="Helvetica", fontSize=7.8, leading=9.8,
                            textColor=MUTED, alignment=TA_CENTER),
    "callout": ParagraphStyle("callout", fontName="Helvetica", fontSize=9.4, leading=13.2,
                              textColor=INK),
    "caption": ParagraphStyle("caption", fontName="Helvetica-Oblique", fontSize=8, leading=10.5,
                              textColor=MUTED, spaceBefore=2, spaceAfter=8),
}


def P(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, S[style])


def bullets(items: list[str]) -> list[Paragraph]:
    return [Paragraph(t, S["bullet"], bulletText="•") for t in items]


def table(rows: list[list], widths: list[float], header: bool = True,
          zebra: bool = True, bold_first: bool = False) -> Table:
    data = []
    for i, row in enumerate(rows):
        style = "head" if header and i == 0 else "cell"
        data.append([c if not isinstance(c, str) else Paragraph(
            c, S["cellb"] if (bold_first and j == 0 and not (header and i == 0)) else S[style])
            for j, c in enumerate(row)])
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE)]
    if header:
        cmds.append(("BACKGROUND", (0, 0), (-1, 0), ACCENT))
    if zebra:
        for r in range(1 if header else 0, len(rows)):
            if r % 2 == 0:
                cmds.append(("BACKGROUND", (0, r), (-1, r), PANEL))
    t.setStyle(TableStyle(cmds))
    return t


def callout(text: str, colour=ACCENT_LIGHT, edge=ACCENT) -> Table:
    t = Table([[Paragraph(text, S["callout"])]], colWidths=[W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colour),
                           ("LINEBEFORE", (0, 0), (0, -1), 3, edge),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def kpis(items: list[tuple[str, str]], per_row: int = 4) -> Table:
    cells = [[Paragraph(v, S["kpi_v"]), Paragraph(label, S["kpi_l"])] for v, label in items]
    rows = []
    for i in range(0, len(cells), per_row):
        chunk = cells[i:i + per_row]
        chunk += [["", ""]] * (per_row - len(chunk))
        rows.append([Table([[c[0]], [c[1]]], colWidths=[W / per_row - 8]) for c in chunk])
    t = Table(rows, colWidths=[W / per_row] * per_row)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PANEL),
                           ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                           ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


def chart(name: str, width: float = W, caption: str | None = None) -> list:
    path = REPORTS / name
    if not path.exists():
        return [P(f"[chart {name} not found: run jobs/report_charts.py]", "small")]
    from reportlab.lib.utils import ImageReader

    iw, ih = ImageReader(str(path)).getSize()
    out = [Image(str(path), width=width, height=width * ih / iw)]
    if caption:
        out.append(P(caption, "caption"))
    return out


# ---------------------------------------------------------------------------
# Architecture diagram
# ---------------------------------------------------------------------------


def _box(d, x, y, w, h, title, lines, fill, edge, title_colour=colors.white):
    d.add(Rect(x, y, w, h, rx=4, ry=4, fillColor=colors.white, strokeColor=edge,
               strokeWidth=1))
    d.add(Rect(x, y + h - 15, w, 15, rx=4, ry=4, fillColor=fill, strokeColor=fill))
    d.add(Rect(x, y + h - 15, w, 6, fillColor=fill, strokeColor=fill))
    d.add(String(x + w / 2, y + h - 11, title, fontName="Helvetica-Bold", fontSize=8,
                 fillColor=title_colour, textAnchor="middle"))
    for i, line in enumerate(lines):
        d.add(String(x + 5, y + h - 27 - i * 9.5, line, fontName="Helvetica", fontSize=6.9,
                     fillColor=INK))


def _arrow(d, x1, y1, x2, y2, colour=MUTED):
    d.add(Line(x1, y1, x2, y2, strokeColor=colour, strokeWidth=1.1))
    import math

    a = math.atan2(y2 - y1, x2 - x1)
    s = 4.5
    d.add(Polygon([x2, y2, x2 - s * math.cos(a - 0.45), y2 - s * math.sin(a - 0.45),
                   x2 - s * math.cos(a + 0.45), y2 - s * math.sin(a + 0.45)],
                  fillColor=colour, strokeColor=colour))


def architecture() -> Drawing:
    w, h = W, 300
    d = Drawing(w, h)
    col = (w - 4 * 12) / 5
    xs = [i * (col + 12) for i in range(5)]
    top = 150
    _box(d, xs[0], top, col, 120, "Sources", [
        "PSRA Property Price", "Register (national zip,", "cp1252 CSV, 809k rows)",
        "CSO RPPI (JSON-stat)", "Tailte Éireann townlands", "CSO built-up areas",
        "Human labels (app)"], MUTED, MUTED)
    _box(d, xs[1], top, col, 120, "Bronze", [
        "Untouched bytes in a", "content-addressed volume", "Row occurrences with IDs",
        "Snapshot reconciliation", "and promotion events", "Source manifests"],
         BRONZE, BRONZE)
    _box(d, xs[2], top, col, 120, "Silver", [
        "Typed sales, dispositions,", "VAT basis, quarantine", "Address features, towns",
        "Splink candidates/scores", "Constrained properties", "Repeat pairs, index fits,",
        "bootstrap draws, support"], SILVER, SILVER)
    _box(d, xs[3], top, col, 120, "Gold", [
        "Validation results", "Backtest predictions", "Interval calibration",
        "Releases + pointer", "Index vintages", "Lookup history",
        "Revisions", "Dashboard views"], GOLD, GOLD)
    _box(d, xs[4], top, col, 120, "Serving", [
        "Lookup app (Streamlit,", "Databricks Apps)", "Labelling app",
        "AI/BI dashboard (5 pages)", "SQL warehouse", "Least-privilege grants"],
         ACCENT, ACCENT)
    for i in range(4):
        _arrow(d, xs[i] + col, top + 60, xs[i + 1], top + 60)
    # platform strip
    d.add(Rect(0, 78, w, 52, rx=4, ry=4, fillColor=ACCENT_LIGHT, strokeColor=ACCENT,
               strokeWidth=0.8))
    d.add(String(8, 117, "Databricks Free Edition: what runs where", fontName="Helvetica-Bold",
                 fontSize=8, fillColor=ACCENT))
    plat = ["Unity Catalog: 2 catalogs (national, Donegal), bronze/silver/gold schemas, volumes",
            "Serverless Jobs: 11-task DAG, Asset Bundle with dev and donegal targets, monthly "
            "schedule, failure email",
            "Delta tables: insert-only MERGE with retry, MLflow experiments for linkage models"]
    for i, line in enumerate(plat):
        d.add(String(8, 104 - i * 10, line, fontName="Helvetica", fontSize=7, fillColor=INK))
    # pipeline strip
    stages = ["ingest", "clean", "addresses", "linkage x2", "entities+pairs", "index",
              "uncertainty", "validation", "backtest", "publish"]
    sw = (w - (len(stages) - 1) * 5) / len(stages)
    for i, st in enumerate(stages):
        x = i * (sw + 5)
        fill = AMBER_LIGHT if i >= 7 else PANEL
        d.add(Rect(x, 30, sw, 24, rx=3, ry=3, fillColor=fill, strokeColor=RULE))
        d.add(String(x + sw / 2, 39, st, fontName="Helvetica", fontSize=6.6, fillColor=INK,
                     textAnchor="middle"))
        if i:
            _arrow(d, x - 5, 42, x, 42, RULE)
    d.add(String(0, 14, "Pipeline tasks (each versioned by a hash of its inputs, code and "
                 "config; shaded = validation, backtest and release)",
                 fontName="Helvetica-Oblique", fontSize=7,
                 fillColor=MUTED))
    return d


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def cover() -> list:
    return [
        Spacer(1, 30 * mm),
        P("Ireland Repeat-Sales<br/>House Price Index", "title"),
        Spacer(1, 6),
        P("Project brief: what was built, how it works, and what it shows", "subtitle"),
        Spacer(1, 14),
        callout(
            "<b>In one sentence:</b> a reproducible, uncertainty-aware house price index "
            "built from Ireland's public Property Price Register on Databricks. It matches "
            "sales of the same home without any property identifier, estimates how prices "
            "moved, publishes only what the data can support, and lets a user see a home's "
            "sale history with an index-adjusted estimate."),
        Spacer(1, 14),
        kpis([("809k", "national register rows<br/>landed and reconciled"),
              ("21,452", "Donegal pilot rows<br/>(19,520 index-eligible)"),
              ("1,220", "repeat-sale pairs<br/>from 1,109 homes"),
              ("255.5", "Donegal index 2026<br/>(2015 = 100, provisional)"),
              ("+0.3 pp", "bias vs CSO regional<br/>YoY growth"),
              ("25.6%", "lookup median error<br/>vs 37.8% unadjusted"),
              ("98.0%", "property-link precision<br/>(260 human labels)"),
              ("14 / 14", "pipeline stages and<br/>quality gates passed")]),
        Spacer(1, 18),
        table([["", ""],
               ["Author", "Conor McCool"],
               ["Date", date(2026, 10, 7).strftime("%d %B %Y")],
               ["Platform", "Databricks Free Edition (serverless), Unity Catalog, Delta, "
                            "Asset Bundles, MLflow, Apps, AI/BI dashboards"],
               ["Stack", "Python 3.12, pandas, NumPy, SciPy, statsmodels, Splink 5, DuckDB, "
                         "Streamlit"],
               ["Release described", "rel-229d61affb29 (published 7 October 2026; later "
                                       "monthly releases are in the release report)"]],
              [32 * mm, W - 32 * mm], header=False, zebra=False, bold_first=True),
        PageBreak(),
    ]


def summary() -> list:
    return [
        P("1. Executive summary", "h1"),
        P("<b>The question.</b> How much have Irish house prices changed, locally and "
          "reliably? And what is a particular home's last sale price worth in today's "
          "market? Official indices (CSO RPPI) are regional and mix-adjusted. They cannot "
          "say how much homes in a given town changed, and they cannot adjust one home's "
          "price."),
        P("<b>The approach.</b> A <i>repeat-sales</i> index compares each home with itself: "
          "the change between two sales of the same property measures the market, with no "
          "need to model size or quality. The method is the Case-Shiller method behind "
          "the S&amp;P CoreLogic indices. The hard part in Ireland is that the Property "
          "Price Register has <b>no property identifier</b>: the same home appears under "
          "differently typed addresses, and rural homes often have only a townland name. "
          "Most of this project is about linking sales to homes <i>correctly</i>, and about "
          "being honest where that is uncertain."),
        P("<b>What exists now.</b>"),
        *bullets([
            "A versioned medallion pipeline (bronze → silver → gold) on Databricks, run as an "
            "11-task serverless job from an Asset Bundle, with a national and a Donegal "
            "profile. Local runs on DuckDB give <b>identical version IDs</b>.",
            "Probabilistic record linkage (Splink) with constrained clustering, human "
            "labelling in a Databricks App, and MLflow-tracked models.",
            "Weighted repeat-sales index with a property-history bootstrap, calibrated "
            "publication gates and an explicit fallback chain.",
            "Validation against the CSO, a predeclared sensitivity grid, and a temporal "
            "cross-fitted backtest of the property estimate with calibrated 90% intervals.",
            "Immutable releases behind a single current-release pointer, with revisions, "
            "rollback and failure drills; a lookup app and a five-page AI/BI dashboard read "
            "the same release.",
        ]),
        Spacer(1, 4),
        callout(
            "<b>Headline findings (Donegal pilot).</b> Prices rose about 2.4× between 2015 "
            "and 2025 (log growth 0.88). Over published years, growth tracks the CSO Border "
            "houses series with almost no bias (+0.3 pp a year). The result is robust: link "
            "thresholds from 0.5 to 0.97 move 2015-2025 growth by at most 0.04, and estimators "
            "agree within 0.015. Index adjustment cuts the typical error of a stale sale "
            "price from 38% to 26%. A town index for Letterkenny did <i>not</i> beat the "
            "county index for property estimates, so the lookup uses Donegal, as the gate "
            "rules require."),
        Spacer(1, 6),
        callout(
            "<b>Linkage, measured.</b> 260 hand-labelled pairs show that 98.0% of the property "
            "links the index uses are correct (95% CI 95.6-99.7%), with 75% recall. That beats "
            "a rule-based linker (95.5%) and falls just short of the 99% target. "
            "The remaining errors are generic apartment addresses in different towns; a fix "
            "is proposed for evaluation on fresh labels rather than tuned on these. The "
            "threshold is frozen, and releases are <i>published</i>.", AMBER_LIGHT, AMBER),
        PageBreak(),
    ]


def problem() -> list:
    return [
        P("2. Why this is harder than it looks", "h1"),
        P("The register is a public record of every residential sale filed for stamp duty "
          "since 2010: date, price, the address as typed, a new/second-hand flag, a "
          "market-price flag and, since about 2021, an Eircode. It was not built for "
          "statistics, and each limitation needs a deliberate response:"),
        table([
            ["Reality of the data", "Consequence", "Response in this project"],
            ["No property ID; free-text addresses", "Cannot simply group sales by home",
             "Address parsing, blocking, Splink linkage, constrained clusters, human labels"],
            ["Rural addresses are often a townland only", "Neighbours look identical",
             "Non-specific addresses link only on a shared Eircode (rural under-coverage is "
             "reported)"],
            ["Eircodes rare before 2021", "Cannot be the main key; biased if used as truth",
             "Used as weak evidence and an audit subset, masked from labellers"],
            ["New-build prices may exclude VAT", "Spurious jumps between legs",
             "Effective-dated VAT rules; unresolved cases excluded from pairs"],
            ["Bulk and non-market filings", "Portfolio prices are not one home's price",
             "Disposition rules, quarantine, history breaks at non-comparable sales"],
            ["Thin local data", "Noisy town series look plausible",
             "Bootstrap, calibrated gates, suppression and fallback"],
            ["Late filings and corrections", "Old periods change", "Immutable vintages, "
             "revisions tagged by cause, full rebuild per refresh"],
        ], [W * 0.31, W * 0.27, W * 0.42]),
        Spacer(1, 8),
        P("<b>Why Donegal.</b> The national pipeline was built first: 809k rows, 1.54M "
          "candidate pairs, with 96% of Eircode-matched pairs recovered by blocking. It is "
          "preserved and paused. The end-to-end pilot runs on Donegal because it mixes a town "
          "(Letterkenny) with a large rural hinterland, which is the hardest linkage case. "
          "It also keeps the human labelling workload manageable (260 pairs). The same code "
          "runs both, chosen by a profile file."),
        PageBreak(),
    ]


def architecture_section() -> list:
    return [
        P("3. Architecture on Databricks", "h1"),
        P("Every stage reads versioned inputs and writes insert-only outputs keyed by a hash "
          "of its inputs, code and configuration. Re-running with nothing changed adds "
          "nothing; changing one rule produces a new version downstream, and the old one is "
          "kept."),
        architecture(),
        Spacer(1, 6),
        table([
            ["Databricks capability", "How it is used"],
            ["Unity Catalog", "Catalogs <font face='Courier'>ireland_rsi</font> and "
             "<font face='Courier'>ireland_rsi_donegal</font>; bronze/silver/gold schemas; "
             "volumes for raw bytes and staging; grants per app service principal"],
            ["Delta Lake", "39 tables written with insert-only MERGE through a Parquet "
             "staging volume; retries on concurrent-write conflicts from parallel tasks"],
            ["Serverless Jobs + Asset Bundles", "One bundle, two targets (national, Donegal); "
             "11-task DAG; wheel built and deployed by the bundle; schedule and alerts as code"],
            ["MLflow", "Linkage experiments: parameters, blocking report, model JSON per track"],
            ["Databricks Apps", "Labelling app (writes labels as the signed-in reviewer) and "
             "lookup app (read-only, parameterised SQL, release-pinned sessions)"],
            ["SQL warehouse + AI/BI", "Governed views over the current release; five-page "
             "dashboard deployed from the bundle"],
        ], [W * 0.27, W * 0.73]),
        PageBreak(),
    ]


TASKS = [
    ("1", "Source contract and pilot",
     "Audited the national CSV (cp1252, 9 fields); pinned CSO benchmark series; provisioned "
     "Databricks; recorded licence and pilot decisions.", "Source-contract check ✓"),
    ("2", "Immutable bronze layer",
     "Content-addressed raw files, occurrence-aware row IDs, snapshot reconciliation and "
     "promotion; verified rebuild.", "Rebuild check ✓"),
    ("3", "Parsing and comparability",
     "Typed parsing, one disposition per row, VAT rules by date, quarantine; every row "
     "accounted for.", "Accounting check ✓"),
    ("4", "Addresses and localities",
     "Unit/number/street/townland parsing; town assignment calibrated against Eircode "
     "routing keys (99.3% exact-name accuracy).", "Address-confidence check ✓"),
    ("5", "Labels and evaluation design",
     "Stratified 260-pair Donegal sample; Eircode weak labels; labelling app on Databricks.",
     "Label design ✓"),
    ("6", "Candidates and Splink",
     "Blocking rules (96% weak-match recall nationally); EM on match-rich blocks; two model "
     "tracks; MLflow logging; threshold frozen at 0.5 from 260 labels (98.0% precision).",
     "Link-threshold gate ✓"),
    ("7", "Property histories and pairs",
     "Constrained clustering (2,984 conflicting links refused); 1,220 headline pairs; pair "
     "audit with zero violations.", "Pair audit ✓"),
    ("8", "Index estimation",
     "OLS, Case-Shiller WRS, Huber, trimmed; identifiability checks; synthetic recovery "
     "tests.", "Identifiability check ✓"),
    ("9", "Uncertainty and publication",
     "Property-history bootstrap (1,000 draws, 20× faster path); gates calibrated by "
     "downsampling; fallback chain.", "Publication gates ✓"),
    ("10", "Validation and sensitivity",
     "CSO growth comparison; 16-run sensitivity grid; label-based threshold precision; "
     "selection profile.", "Validation check ✓"),
    ("11", "Backtest and intervals",
     "Temporal, 5-fold cross-fitted backtest vs 3 baselines; 90% intervals calibrated and "
     "tested; town index rejected.", "Backtest gate ✓"),
    ("12", "Releases and orchestration",
     "Immutable releases, pointer, revisions, rollback; refresh, gate-failure and rollback "
     "drills on Databricks.", "Release drills ✓"),
    ("13", "Lookup app",
     "Search, history, estimate with reason codes; release-pinned; least privilege; warm "
     "p95 2.98 s.", "Lookup check ✓"),
    ("14", "Dashboard and evidence",
     "Governed views with small-cell rule; 5-page AI/BI dashboard; methodology, runbook, "
     "demo, this brief.", "Dashboard check ✓"),
]


def tasks_section() -> list:
    rows = [["#", "Stage", "What was built", "Quality gate"]]
    for n, name, what, gate in TASKS:
        g = (f"<font color='#0f766e'><b>{escape(gate)}</b></font>" if "✓" in gate
             else f"<font color='#b45309'><b>{escape(gate)}</b></font>")
        rows.append([n, f"<b>{escape(name)}</b>", escape(what), g])
    return [
        P("4. Stage-by-stage breakdown", "h1"),
        P("The pipeline has 14 stages, each closed by a quality gate: evidence that must "
          "exist before the next stage may rely on it. Every gate is passed for Donegal. "
          "The last to close was the link-threshold gate, frozen once 260 pairs had been "
          "labelled by hand."),
        table(rows, [8 * mm, 34 * mm, W - 8 * mm - 34 * mm - 30 * mm, 30 * mm]),
        PageBreak(),
    ]


def methods_section() -> list:
    return [
        P("5. How it works, step by step", "h1"),
        P("5.1 Linking sales into homes", "h2"),
        *bullets([
            "<b>Blocking:</b> only plausible pairs are compared (same number and street, "
            "same townland, same Eircode, …). For Donegal this gives 47k candidate pairs "
            "instead of 230 million.",
            "<b>Scoring (Splink, Fellegi-Sunter):</b> each pair gets a match probability from "
            "agreement on unit, number, street, town, townland and Eircode. Prices are "
            "<i>never</i> used, so the linkage cannot be tuned to produce a nicer index.",
            "<b>Constraints:</b> links are added strongest first. A merge is refused if it "
            "would put two different flat numbers, two Eircodes or two same-day prices into "
            "one home. This stops chains such as A-B-C from merging a whole apartment block.",
            "<b>Human labels:</b> a stratified sample of pairs is labelled in a Databricks "
            "App. Precision is estimated with sampling weights and confidence bounds, and "
            "drives the threshold (link-threshold gate).",
        ]),
        P("5.2 From pairs to an index", "h2"),
        P("For each pair, y = log(price<sub>2</sub> / price<sub>1</sub>) is regressed on "
          "period indicators (−1 at the first sale's period, +1 at the second's). "
          "The coefficients are the log index. "
          "Pairs held for longer are noisier, so a second stage models residual variance "
          "against holding time and the third stage reweights (Case-Shiller weighted "
          "repeat sales). The index is rebased so that 2015 = 100."),
        P("5.3 How sure are we?", "h2"),
        P("Homes, not pairs, are resampled 1,000 times (a home with three sales contributes "
          "two dependent pairs), and the full estimator is refitted each time. A series is "
          "published only if it has at least 300 homes, 95% successful replicates, no "
          "dominant home and an identified base year. A year is shown only if it has 20 or "
          "more pairs and a 95% band narrower than ±15%. These thresholds came from "
          "downsampling Donegal, not from guesswork."),
        *chart("donegal_release_index.png", W,
               "Published Donegal and Letterkenny annual indices with 95% bootstrap bands. "
               "Crosses are suppressed years; the grey line is the CSO Border houses index."),
        P("5.4 The property estimate", "h2"),
        P("estimate = anchor sale price × index[target] ÷ index[anchor]. The 90% "
          "prediction interval comes from <i>out-of-sample</i> errors in a calibration "
          "window, by holding period. If any input is unsupported, the app returns a reason "
          "code (for example <font face='Courier'>UNSUPPORTED_PERIOD</font> or "
          "<font face='Courier'>NO_ELIGIBLE_ANCHOR</font>) instead of a number."),
        PageBreak(),
    ]


def results_section() -> list:
    return [
        P("6. Results", "h1"),
        P("6.1 Agreement with the official index", "h2"),
        *chart("donegal_validation_cso.png", W,
               "Levels and year-on-year growth: Donegal repeat-sales and CSO RPPI Border houses. "
               "Over published years: bias +0.3 pp a year, mean absolute difference 4.2 pp, "
               "same direction in 92% of years. The CSO series is five counties, houses only "
               "and hedonic, so close but imperfect agreement is the expected outcome."),
        P("6.2 Do the modelling choices matter?", "h2"),
        *chart("donegal_sensitivity.png", W,
               "Each variant rebuilds histories, pairs and the index with one choice changed. "
               "Red bars exceed the 0.05 investigation flag: the loosest threshold (0.3), and "
               "the Eircode-confirmed subset, which can only be measured for 2021-2025."),
        CondPageBreak(90 * mm),
        P("6.3 Does the property estimate work?", "h2"),
        *chart("donegal_backtest.png", W,
               "Left: median absolute error on 446 test sales (2023-2025), each predicted from "
               "an index that never saw that home or any later data. Right: share of test "
               "sales inside the 90% interval, with 95% confidence bars."),
        table([
            ["Method (test 2023-2025)", "Median abs. error", "Bias (log)", "Comment"],
            ["Unchanged last price", "37.8%", "+0.515", "Ignores a decade of growth"],
            ["CSO national index", "26.7%", "+0.151", "Final data, so it has hindsight; misses "
                                                       "local growth"],
            ["<b>Donegal repeat-sales index</b>", "<b>25.6%</b>", "<b>−0.007</b>",
             "Used by the lookup"],
            ["Letterkenny town index (diagnostic)", "28.8%*", "", "*On Letterkenny homes; "
                                                                  "Donegal 24.2%. Not used."],
        ], [W * 0.33, W * 0.17, W * 0.13, W * 0.37]),
        Spacer(1, 6),
        P("The remaining ~25% error is property-specific: renovation, condition, the "
          "particular buyer. No area index can see it. That is why the 90% interval runs "
          "from roughly half to double the estimate, and why the app labels the figure an "
          "<i>index-adjusted estimate</i>, not a valuation.", "small"),
        PageBreak(),
    ]


def engineering_section() -> list:
    return [
        P("7. Engineering and governance", "h1"),
        table([
            ["Property", "Evidence"],
            ["Reproducible", "Version IDs are hashes of inputs, code and config. Local "
                             "(DuckDB) and Databricks (Delta) produce identical entity, fit, "
                             "uncertainty, validation, backtest and release IDs."],
            ["Idempotent", "Insert-only MERGE on declared keys; a repeated run inserts 0 rows "
                           "and republishing the same inputs is a no-op."],
            ["Safe publication", "Serving tables are written first and the pointer moves "
                                 "last, in one insert. Drills on Databricks: forced gate "
                                 "failure (pointer unchanged), rollback and roll forward."],
            ["Traceable", "Every index point → fit → pairs → two sales → bronze row → raw "
                          "file checksum. Revisions are tagged source / method."],
            ["Tested", "265 automated tests in 19 files: synthetic truth recovery, bootstrap "
                       "equivalence, leakage (future-price perturbation), SQL injection as "
                       "literal input, release consistency, dashboard reconciliation."],
            ["Least privilege", "Lookup app: SELECT on 5 gold tables only. Labelling app: "
                                "read sample, append labels. No person is ever shown."],
            ["Honest outputs", "Suppressed periods are blank; unsupported estimates return "
                               "reason codes; research releases carry a banner; small "
                               "dashboard cells (<10) are suppressed."],
            ["Cost-aware", "Full Donegal refresh ≈ 14 minutes serverless task time; the "
                           "bootstrap was rewritten from 17.5 to 2.7 minutes."],
        ], [W * 0.2, W * 0.8]),
        Spacer(1, 8),
        P("Notable problems found and fixed", "h2"),
        *bullets([
            "<b>EM learned the wrong thing:</b> unrestricted training learned 'same street' "
            "instead of 'same home' (m for house number 0.012). Fixed by training only on "
            "match-rich blocks and fixing m for county and number by assumption.",
            "<b>Over-merging in the countryside:</b> 9-sale 'properties' such as a townland "
            "name. Fixed with the non-specific-address rule (link only on a shared Eircode).",
            "<b>Hidden version drift:</b> a version hash ignored one source file, and another "
            "included a random Splink ID. Both were caught because local and Databricks IDs "
            "disagreed.",
            "<b>A misleading subset:</b> the Eircode-only experiment first showed growth of "
            "0.28 from one 2015 pair. Comparisons now require 20 or more endpoint pairs.",
        ]),
        PageBreak(),
    ]


def limits_section() -> list:
    return [
        P("8. Limitations and next steps", "h1"),
        table([
            ["Limitation", "Effect", "Next step"],
            ["Link precision 98%, not the 99% aspiration", "About 2% of links join two "
             "homes", "Test a raw-town-word constraint on a fresh labelled sample"],
            ["Eircode-confirmed pairs grew 0.12 faster (2021-25)", "Unexplained; too large "
             "for 2% false links", "Investigate segment differences as Eircode data grows"],
            ["One labeller, 31 missed matches", "No agreement rate; recall uncertain "
             "(56-90%)", "Second reviewer on a subset; re-check flagged labels"],
            ["Rural homes under-represented", "Index skews urban and cheaper",
             "Report as a scope statement; explore Eircode-era rural linkage"],
            ["Retrospective backtest only", "No real-time accuracy claim",
             "Archive monthly snapshots from now on to enable an operational replay"],
            ["Intervals slightly under-cover (86-88%)", "Within tolerance",
             "Recalibrate as test data accumulates"],
            ["Full rebuild per refresh", "Fine for a county", "Incremental scoring before "
                                                               "national scale"],
        ], [W * 0.33, W * 0.27, W * 0.40]),
        Spacer(1, 10),
        P("Scope: Donegal", "h2"),
        P("The project is complete for Donegal, which is its chosen scope. The national "
          "pipeline (ingestion to linkage, 809k rows) is preserved but paused. Any other "
          "county could join only by passing the same gates, and national scale would "
          "first need incremental linkage."),
        P("Where to look", "h2"),
        table([
            ["Item", "Location"],
            ["Decisions (D-001 … D-030)", "docs/decisions.md"],
            ["Stage reports", "docs/*_report.md (source, quality, address, linkage, pairs, "
                             "index, uncertainty, validation, backtest)"],
            ["Methodology / runbook / demo", "docs/methodology.md, docs/runbook.md, "
                                             "docs/demo.md"],
            ["Pipeline code", "src/ireland_rsi/ (20 modules), jobs/ (one entry point per "
                              "stage)"],
            ["Apps and dashboard", "app/labelling, app/lookup, dashboards/"],
            ["Databricks definition", "databricks.yml (targets dev and donegal)"],
        ], [W * 0.3, W * 0.7]),
        Spacer(1, 10),
        P("Glossary", "h2"),
        table([
            ["Term", "Meaning"],
            ["Repeat-sales index", "Price change measured from homes that sold more than once"],
            ["WRS", "Weighted repeat sales: down-weights long-held, noisier pairs"],
            ["Bootstrap", "Refitting on resampled homes to measure sampling uncertainty"],
            ["Gate", "Evidence required before a later step may rely on an earlier one"],
            ["Release / pointer", "An immutable set of outputs / the record of which "
                                  "release is live"],
            ["Research release", "Passes every hard gate; link threshold not yet frozen"],
            ["MdAPE", "Median absolute percentage error"],
        ], [W * 0.25, W * 0.75]),
    ]


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    if doc.page > 1:
        canvas.drawString(18 * mm, 10 * mm, "Ireland Repeat-Sales House Price Index · project "
                                            "brief")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{doc.page}")
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(3)
    canvas.line(0, A4[1] - 1.5, A4[0], A4[1] - 1.5)
    canvas.restoreState()


def main() -> None:
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title="Ireland Repeat-Sales House Price Index: project brief",
                            author="Conor McCool")
    story = (cover() + summary() + problem() + architecture_section() + tasks_section()
             + methods_section() + results_section() + engineering_section()
             + limits_section())
    doc.build([KeepTogether(x) if isinstance(x, list) else x for x in story],
              onFirstPage=footer, onLaterPages=footer)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
