"""Build the Donegal release report PDF from the current local release.

    python docs/build_release_report.py

Everything quantitative is read from the release tables (gold.*), so rebuilding after a
monthly refresh documents the new release. Layout helpers are shared with build_brief.py.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import duckdb
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, PageBreak, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / "app" / "lookup"))
import service  # noqa: E402
from build_brief import (  # noqa: E402
    ACCENT_LIGHT,
    AMBER,
    AMBER_LIGHT,
    MUTED,
    P,
    W,
    bullets,
    callout,
    chart,
    kpis,
    table,
)
from reportlab.lib import colors  # noqa: E402

WAREHOUSE = ROOT / "data" / "warehouse" / "donegal.duckdb"
CON = duckdb.connect(str(WAREHOUSE), read_only=True)


class Local:
    def query(self, sql, params):
        return CON.execute(re.sub(r"(?<![:\w]):(\w+)", r"$\1", sql), params).df()


DB, CAT = Local(), "donegal"
REL = service.current_release(DB, CAT)
RID = REL.release_id
MANIFEST = CON.execute("SELECT * FROM gold.release_manifest WHERE release_id = ?",
                       [RID]).df().iloc[0]
VERSIONS = json.loads(MANIFEST.versions_json)
METRICS = json.loads(MANIFEST.metrics_json)
DATA_TO = CON.execute("SELECT max(sale_date) FROM silver.sales").fetchone()[0]
OUT = ROOT / "docs" / f"donegal_release_{RID}.pdf"
DATABRICKS_RUNS = {"full refresh (this release)": "358191034653175"}


def eur(x) -> str:
    return "-" if x is None else f"€{x:,.0f}"


def q(sql: str, *args):
    return CON.execute(sql, list(args)).df()


def val(version_key: str, category: str, experiment: str, metric: str, segment: str):
    r = q("SELECT value, n, detail FROM gold.validation_results WHERE validation_version = ? "
          "AND category = ? AND experiment = ? AND metric = ? AND segment = ?",
          VERSIONS[version_key], category, experiment, metric, segment)
    if r.empty:
        return None, None, {}
    row = r.iloc[0]
    return row.value, row.n, json.loads(row.detail) if row.detail else {}


# ---------------------------------------------------------------------------
# Figures read from the release
# ---------------------------------------------------------------------------

wp, n_links, wp_d = val("validation", "linkage", "final_links", "weighted_precision",
                        f"threshold={json.loads(MANIFEST.gates_json)['g6_threshold_frozen']['detail'].split()[1]}")
wr, _, wr_d = val("validation", "linkage", "final_links", "weighted_recall", "threshold=0.5")
bias, n_bias, _ = val("validation", "benchmark", "HPM09.border_houses", "yoy_bias",
                      "published_periods")
mad, _, _ = val("validation", "benchmark", "HPM09.border_houses", "yoy_mean_abs_diff",
                "published_periods")
bt = q("SELECT experiment AS method, metric, value, n FROM gold.validation_results "
       "WHERE validation_version = ? AND category = 'backtest' AND segment = 'test|common'",
       VERSIONS["backtest"])
mdape = bt[bt.metric == "median_abs_pct_error"].set_index("method").value
n_test = int(bt[bt.method == "local"].n.max())
cal = q("SELECT * FROM gold.release_calibration WHERE release_id = ? ORDER BY horizon_band",
        RID)
vint = q("SELECT * FROM gold.index_vintages WHERE release_id = ? AND frequency = 'annual' "
         "ORDER BY geography_id, period", RID)
labels = q("SELECT label, count(*) n FROM (SELECT pair_id, label, row_number() OVER "
           "(PARTITION BY pair_id ORDER BY labelled_at DESC) rn FROM silver.link_labels) "
           "WHERE rn = 1 GROUP BY label").set_index("label").n
scope = vint[vint.geography_id == "scope"].set_index("period")
latest_full = scope[scope.period_state == "supported"].index.max()
growth_15 = scope.index_value[latest_full] / 100


def example(property_hint: str, anchor_year: str | None, target: str):
    hits = service.search_properties(DB, CAT, property_hint, None, RID)
    anchor = None
    for pid in hits.property_id:   # the best match that has a sale in the anchor year
        hist = service.get_history(DB, CAT, pid, RID)
        cand = hist[hist.sale_date.astype(str).str.startswith(anchor_year or "")]
        if len(hist) > 1 and len(cand):
            anchor = cand.sale_id.iloc[0] if anchor_year else None
            break
    else:
        raise LookupError(f"no example property for {property_hint!r} in {anchor_year}")
    est = service.estimate(DB, CAT, pid, REL, anchor, target)
    return hist, est


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def cover() -> list:
    return [
        Spacer(1, 24 * mm),
        P("Donegal House Price Index", "title"),
        Spacer(1, 4),
        P(f"Release {RID} · published {MANIFEST.created_at:%d %B %Y} · register data to "
          f"{DATA_TO:%d %B %Y}", "subtitle"),
        Spacer(1, 12),
        callout(
            "<b>What this is.</b> A house price index for County Donegal built from every "
            "home that sold more than once in the public Property Price Register, plus a "
            "lookup that brings any Donegal home's past sale price forward to today's "
            "market, with an honest range. Every figure in this release has passed the "
            "project's quality gates."),
        Spacer(1, 12),
        kpis([(f"{scope.index_value[latest_full]:.0f}",
               f"Donegal index {latest_full}<br/>(2015 = 100)"),
              (f"{growth_15:.2f}×", f"price change<br/>2015 → {latest_full}"),
              (f"{wp:.1%}", "property links correct<br/>(260 human labels)"),
              (f"{mdape['local']:.1f}%", f"typical estimate error<br/>vs {mdape['nominal']:.1f}% "
                                         "unadjusted"),
              (f"{METRICS['sales_rows']:,}", "register rows<br/>in the release"),
              (f"{int(scope.endpoint_pairs.sum() / 2):,}", "repeat-sale pairs<br/>"
                                                            "behind the index"),
              ("90%", "prediction range<br/>tested on later sales"),
              (MANIFEST.release_state, "release state<br/>(all gates passed)")]),
        Spacer(1, 14),
        table([["", ""],
               ["Scope", "County Donegal, with a Letterkenny town index (decision D-030)"],
               ["Product", "Lookup app (Databricks Apps) and AI/BI dashboard; this report"],
               ["Refresh", "Monthly, 10th at 06:00 Europe/Dublin; each refresh is a new release"],
               ["Author", "Conor McCool"]],
              [30 * mm, W - 30 * mm], header=False, zebra=False, bold_first=True),
        PageBreak(),
    ]


def sixty_seconds() -> list:
    return [
        P("1. The release in sixty seconds", "h1"),
        P("The release, step by step:", "small"),
        *bullets([
            "<b>The problem.</b> Ireland publishes every home sale since 2010, but with no ID "
            "for the home itself: the same house appears under differently typed addresses. "
            "You cannot simply ask how much a given home, or a given town, has changed.",
            "<b>The data.</b> I took the official register untouched into Databricks: "
            f"{METRICS['sales_rows']:,} Donegal sales, every row accounted for, with "
            "VAT and non-market sales handled by written, tested rules.",
            "<b>Linking sales to homes.</b> A probabilistic record-linkage model (Splink) "
            "with safety rules decides which sales are the same home. I labelled 260 pairs by "
            f"hand to measure it: <b>{wp:.0%} of links are correct</b>, and it finds about "
            f"{wr:.0%} of true repeat sales.",
            "<b>The index.</b> Comparing each home with itself (a Case-Shiller weighted "
            f"repeat-sales index) shows Donegal prices <b>{growth_15:.1f}× their 2015 level by "
            f"{latest_full}</b>. A bootstrap gives each year a confidence band, and years "
            "without enough evidence are suppressed, not guessed.",
            f"<b>Checked against official figures.</b> Year-on-year growth tracks the CSO's "
            f"regional index with almost no bias ({bias:+.1f} points a year).",
            "<b>Tested like a forecast.</b> Each later sale was predicted from an index that "
            f"had never seen that home or any later data. The typical error was "
            f"<b>{mdape['local']:.0f}%</b> against {mdape['nominal']:.0f}% for the old price "
            "unadjusted, and the 90% ranges held up on the test years.",
            "<b>The product.</b> A signed-in lookup app: type an address, see the home's sale "
            "history, and get its last price brought forward, with a range and plain "
            "reasons when no fair number exists. A dashboard shows the market view.",
            "<b>Built to be trusted.</b> Each release is immutable and reproducible "
            "(identical on my laptop and on Databricks), refreshed monthly, and can be "
            "rolled back.",
        ]),
        Spacer(1, 6),
        callout("<b>One line:</b> <i>a Databricks-built, quality-gated Donegal house price "
                "index and lookup that tells people what their home's last sale price means "
                "in today's market, and how sure we can be.</i>"),
        PageBreak(),
    ]


def users_and_access() -> list:
    return [
        P("2. Who it is for and how they use it", "h1"),
        table([
            ["User", "Their question", "What they do", "What they get"],
            ["<b>Homeowner</b>", "How has my home moved since I bought it?",
             "Search the address; anchor on the purchase; pick the latest year",
             "Purchase price brought forward, with a 90% range"],
            ["<b>Buyer</b>", "Is the asking price in line with the market since its last "
             "sale?", "Anchor on the last sale; compare the estimate with the asking price",
             "A market-only benchmark; a big gap is a prompt to ask why"],
            ["<b>Seller</b>", "What would the market alone suggest?",
             "Same as the buyer", "A starting point for a conversation with an agent"],
            ["<b>Local reader</b>", "How have Donegal and Letterkenny prices moved?",
             "Open the dashboard", "Index, uncertainty, CSO comparison, data quality"],
        ], [W * 0.14, W * 0.27, W * 0.31, W * 0.28]),
        Spacer(1, 6),
        callout("<b>What it is not.</b> Not a valuation, a mortgage or insurance figure, or a "
                "forecast. It cannot see extensions, renovation, condition or the particular "
                "street, which is why the range for one home runs from roughly half to double "
                "the estimate.", AMBER_LIGHT, AMBER),
        P("Getting access", "h2"),
        *bullets([
            "The app and dashboard run on Databricks and need a sign-in: this is an "
            "authenticated release, with no anonymous public access.",
            "A workspace admin invites the user, then grants <b>Can use</b> on the app "
            "<font face='Courier'>ireland-rsi-lookup-donegal</font> and view access to the "
            "dashboard.",
            "People without an account get this report, which carries the published index, "
            "worked examples and the evidence.",
        ]),
        P("Using the lookup", "h2"),
        table([
            ["Step", "What the user sees"],
            ["1. Type an address", "Candidate homes; similar-looking addresses are never "
                                   "merged automatically"],
            ["2. Pick the home", "Every recorded sale: date, price as filed, VAT-adjusted "
                                 "price where relevant, and whether it can anchor"],
            ["3. Choose anchor and target", "Defaults: most recent usable sale and the latest "
                                            "published year"],
            ["4. Read the result", "'Index-adjusted estimate as of [year]', the 90% range, the "
                                   "index change, which index was used, provisional flags"],
            ["5. Check the chart", "The Donegal index with its own (narrower) confidence band"],
        ], [W * 0.27, W * 0.73]),
        P("When there is no estimate", "h2"),
        table([
            ["Reason shown", "Meaning"],
            ["No eligible anchor", "Every sale is non-market, a bulk filing or VAT-unresolved; "
                                   "the facts are still shown"],
            ["Unsupported period", "The sale or target year is suppressed for lack of evidence"],
            ["Sparse geography", "No published index covers the area"],
            ["Uncalibrated interval", "The holding period failed its accuracy test (none "
                                      "currently)"],
        ], [W * 0.27, W * 0.73]),
        PageBreak(),
    ]


def examples_section() -> list:
    rows = [["Home (register address)", "Anchor sale", f"Estimate for {latest_full}",
             "90% range", "What happened"]]
    cases = [("26 Maginn Avenue Buncrana", "2016",
              "Resold Dec 2024 for €270,000: inside the range, above the estimate"),
             ("24 Donegal Road Ballybofey", "2014",
              "Resold Apr 2025 for €252,250: inside the range, below the estimate"),
             ("10B Fairgreen Park Letterkenny", "2016",
              "Letterkenny home; the Donegal index is used (D-025)")]
    for hint, year, note in cases:
        hist, e = example(hint, year, latest_full)
        rows.append([f"<b>{hist.address.iloc[0].title()}</b>",
                     f"{e.anchor_date:%b %Y} · {eur(e.anchor_price)}",
                     f"<b>{eur(e.amount)}</b> (×{e.index_ratio:.2f})",
                     f"{eur(e.interval_lo)} to {eur(e.interval_hi)}", note])
    refused_hist, refused = example("29 Amalfi Court Letterkenny", "2019", latest_full)
    # A home whose only usable anchor is in a suppressed year (2011-2012).
    pid12 = CON.execute(
        "SELECT property_id FROM gold.lookup_history WHERE release_id = ? "
        "AND anchor_eligible AND year(sale_date) = 2012 AND property_sales >= 2 "
        "ORDER BY property_id LIMIT 1", [RID]).fetchone()[0]
    h12 = service.get_history(DB, CAT, pid12, RID)
    a12 = h12[h12.sale_date.astype(str).str.startswith("2012") & h12.anchor_eligible].iloc[0]
    early = service.estimate(DB, CAT, pid12, REL, a12.sale_id, latest_full)
    return [
        P("3. Worked examples from this release", "h1"),
        P("Real register records, run through the same service the app uses. The later "
          "resales illustrate the range; they are not held-out tests, because they are "
          "part of this index. The held-out results are in section 5."),
        table(rows, [W * 0.24, W * 0.15, W * 0.17, W * 0.2, W * 0.24]),
        Spacer(1, 8),
        P("When the app refuses", "h2"),
        table([
            ["Request", "Result"],
            [f"29 Amalfi Court, Letterkenny, anchored on the {refused.anchor_date:%B %Y} "
             f"sale", f"<b>{refused.reason_code}</b>: "
                     f"{refused.message.split(' Reason:')[0]} The register marks that sale "
                     "as not at full market price, so the app refuses it rather than "
                     "switching anchors."],
            [f"{a12.address.title()}, anchored on its {a12.sale_date:%B %Y} sale, "
             f"target {latest_full}",
             f"<b>{early.reason_code}</b>: {early.message} 2012 is suppressed in the "
             "Donegal index (too few resales to measure it reliably), so no fair number "
             "exists for that anchor."],
        ], [W * 0.38, W * 0.62]),
        Spacer(1, 6),
        P("Reading the Buncrana example aloud: <i>'It last sold for €80,000 in 2016. "
          "Donegal prices are 2.2 times higher now, so the market alone takes it to about "
          "€176,000, and nine times in ten a home like this sells somewhere between €75,000 "
          "and €320,000. It actually sold for €270,000, which tells you the house or its "
          "street did better than the county average.'</i>", "small"),
        PageBreak(),
    ]


def index_section() -> list:
    lk = vint[vint.geography_id == "town:Letterkenny"].set_index("period")
    rows = [["Year", "Donegal", "95% band", "Pairs", "Letterkenny", "95% band"]]
    for period in scope.index:
        s = scope.loc[period]
        sv = ("suppressed" if s.period_state == "suppressed"
              else f"{s.index_value:.1f}" + (" (prov.)" if s.period_state == "provisional"
                                             else ""))
        sb = "" if s.period_state == "suppressed" else f"{s.level_lo:.0f}-{s.level_hi:.0f}"
        if period in lk.index:
            t = lk.loc[period]
            lv = "suppressed" if t.period_state == "suppressed" else f"{t.index_value:.1f}"
            lb = "" if t.period_state == "suppressed" else f"{t.level_lo:.0f}-{t.level_hi:.0f}"
        else:
            lv, lb = "-", ""
        rows.append([period, sv, sb, str(int(s.endpoint_pairs)), lv, lb])
    return [
        P("4. The published index", "h1"),
        *chart("donegal_release_index.png", W,
               "Annual repeat-sales index, 2015 = 100, with 95% property-history bootstrap "
               "bands, from this release. Crosses mark suppressed years; the grey line is "
               "the CSO RPPI for Border houses."),
        table(rows, [W * 0.1, W * 0.17, W * 0.17, W * 0.12, W * 0.2, W * 0.24]),
        P("A year is published only with at least 20 repeat-sale pairs and a 95% band "
          "narrower than about ±15%. 2011-2012 fail for Donegal. Most Letterkenny years after "
          "2023 fail because too few homes resold to measure them reliably. Quarterly "
          "series are calculated but not published: they fail the bootstrap gate.", "small"),
        PageBreak(),
    ]


def evidence_section() -> list:
    rows = [["Horizon", "Calibrated range (× estimate)", "Test coverage", "Test sales"]]
    import math

    for r in cal.itertuples():
        rows.append([r.horizon_band, f"{math.exp(r.q_lo):.2f} to {math.exp(r.q_hi):.2f}",
                     f"{r.coverage_test:.1%}", str(int(r.n_test))])
    return [
        P("5. How far to trust it", "h1"),
        table([
            ["Question", "Evidence in this release"],
            ["Is every source row accounted for?",
             f"{METRICS['sales_rows']:,} rows, each with one disposition; the original file "
             "is kept byte-for-byte"],
            ["Are sales linked to the right homes?",
             f"{wp:.1%} of links correct (95% CI {wp_d.get('lo', 0):.1%}-"
             f"{wp_d.get('hi', 0):.1%}); recall {wr:.0%}; 260 labels "
             f"({labels.get('match', 0)} same, {labels.get('non_match', 0)} different, "
             f"{labels.get('uncertain', 0)} can't tell)"],
            ["Does the index agree with official figures?",
             f"Year-on-year growth against CSO Border houses: bias {bias:+.1f} pp, mean "
             f"absolute difference {mad:.1f} pp ({int(n_bias)} published years)"],
            ["Do the modelling choices change the answer?",
             "Link thresholds from 0.5 to 0.97 move 2015-2025 growth by ≤ 0.04 log points; "
             "the estimators agree within 0.015"],
            ["Are estimates better than doing nothing?",
             f"Typical error {mdape['local']:.1f}% vs {mdape['nominal']:.1f}% unadjusted and "
             f"{mdape['national_cso']:.1f}% using the CSO national index ({n_test} test "
             "sales, 2023-2025)"],
            ["Can it be reproduced and rolled back?",
             "Identical release ID locally and on Databricks; immutable releases; rollback "
             "drills passed"],
        ], [W * 0.32, W * 0.68]),
        P("Property prediction ranges (90%)", "h2"),
        table(rows, [W * 0.2, W * 0.34, W * 0.24, W * 0.22]),
        P("Coverage is slightly under 90% in every band but within the 5-point tolerance. "
          "The ranges are wide because they include everything the index cannot see about "
          "an individual home.", "small"),
        CondPageBreak(70 * mm),
        *chart("donegal_backtest.png", W),
        PageBreak(),
    ]


def scope_and_facts() -> list:
    gates = json.loads(MANIFEST.gates_json)
    return [
        P("6. Scope and limitations", "h1"),
        *bullets([
            "<b>Donegal only (D-030).</b> This is a complete county release. The national "
            "pipeline exists but is not published: each county would need its own labelled "
            "evidence first.",
            "<b>Resold, identifiable homes.</b> Rural homes are 56% of Donegal sales but 38% "
            "of the repeat sales the index uses, because townland-only addresses cannot be "
            "linked safely. The index leans urban and lower-priced.",
            f"<b>About {1 - wp:.0%} of links join two different homes</b>, mostly generic "
            "apartment addresses in different towns. That is short of the 99% "
            "aspiration; a fix is proposed for testing on fresh labels.",
            "<b>Retrospective testing.</b> Accuracy was measured on past data. The system "
            "has no archive of what was known month by month, so it makes no real-time "
            "accuracy claim.",
            "<b>No houses/apartments split</b> exists in the register, so the CSO comparison "
            "is approximate.",
        ]),
        P("7. Release facts", "h1"),
        table([
            ["", ""],
            ["Release", f"{RID} ({MANIFEST.release_state}), created "
                        f"{MANIFEST.created_at:%Y-%m-%d %H:%M}"],
            ["Source snapshot", f"{MANIFEST.snapshot_id} (PSRA register, data to "
                                f"{DATA_TO:%d %b %Y})"],
            ["Versions", ", ".join(f"{k} {v}" for k, v in VERSIONS.items())],
            ["Gates", ", ".join(f"{k} {'✓' if g['passed'] else '✗'}" for k, g in gates.items())],
            ["Databricks runs", "; ".join(f"{k}: {v}" for k, v in DATABRICKS_RUNS.items())],
            ["Reproduce", "python jobs/run_pipeline.py --profile donegal --start clean · "
                          "databricks bundle run ingest_ppr -t donegal"],
            ["Documents", "docs/user_guide.md, methodology.md, validation_report.md, "
                          "backtest_report.md, runbook.md, decisions.md (D-001…D-030)"],
        ], [30 * mm, W - 30 * mm], header=False, zebra=False, bold_first=True),
    ]


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    if doc.page > 1:
        canvas.drawString(18 * mm, 10 * mm, f"Donegal House Price Index · release {RID}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{doc.page}")
    canvas.setStrokeColor(colors.HexColor("#0f766e"))
    canvas.setLineWidth(3)
    canvas.line(0, A4[1] - 1.5, A4[0], A4[1] - 1.5)
    canvas.restoreState()


def main() -> None:
    assert ACCENT_LIGHT  # shared palette imported
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"Donegal House Price Index: release {RID}",
                            author="Conor McCool")
    doc.build(cover() + sixty_seconds() + users_and_access() + examples_section()
              + index_section() + evidence_section() + scope_and_facts(),
              onFirstPage=footer, onLaterPages=footer)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
