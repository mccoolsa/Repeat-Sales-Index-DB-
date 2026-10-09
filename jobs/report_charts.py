"""Charts for the validation and backtest reports and the project brief.

Drawn from the current local release.

    python jobs/report_charts.py --profile donegal
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from ireland_rsi.profiles import load_profile, open_stores  # noqa: E402
from ireland_rsi.publish import current_release  # noqa: E402

INK, MUTED, ACCENT, ALT, WARN = "#1f2933", "#7b8794", "#0f766e", "#b45309", "#b91c1c"
plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": INK,
                     "ytick.color": INK, "axes.titleweight": "bold", "axes.titlesize": 10})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="donegal")
    parser.add_argument("--out", type=Path, default=Path("reports"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    _, t, _ = open_stores(load_profile(root, args.profile), "local")
    rid = current_release(t)
    versions = json.loads(t.query(f"SELECT versions_json FROM gold.release_manifest "
                                  f"WHERE release_id = '{rid}'").iloc[0, 0])
    vv, bv = versions["validation"], versions["backtest"]
    args.out.mkdir(exist_ok=True)
    series = t.query(f"SELECT * FROM gold.sensitivity_series WHERE validation_version = '{vv}' "
                     "AND geography_id = 'scope' AND frequency = 'annual'")

    def s(exp):
        d = series[series.experiment == exp].sort_values("period")
        return d.period.astype(int), d.index_value

    # 1. Index vs CSO, levels and YoY growth
    fig, (a, b) = plt.subplots(1, 2, figsize=(9, 3.4))
    x, y = s("baseline")
    cx, cy = s("cso:border_houses")
    a.plot(x, y, color=ACCENT, lw=2, marker="o", ms=3, label="Repeat-sales, Donegal")
    a.plot(cx, cy, color=ALT, lw=1.6, ls="--", label="CSO RPPI, Border houses")
    a.axhline(100, color=MUTED, lw=0.6)
    a.set_title("Index levels (2015 = 100)")
    a.legend(frameon=False, fontsize=8)
    g1 = pd.Series(y.values, x.values).pct_change() * 100
    g2 = pd.Series(cy.values, cx.values).pct_change() * 100
    common = g1.index.intersection(g2.index)[1:]
    b.bar(common - 0.2, g1[common], 0.4, color=ACCENT, label="Repeat-sales")
    b.bar(common + 0.2, g2[common], 0.4, color=ALT, label="CSO")
    b.axhline(0, color=MUTED, lw=0.6)
    b.set_title("Year-on-year growth (%)")
    b.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(args.out / f"{args.profile}_validation_cso.png", dpi=180)
    plt.close(fig)

    # 2. Sensitivity
    sens = t.query(
        "SELECT experiment, max(CASE WHEN metric = 'growth_shift_vs_baseline' THEN value END) "
        "AS shift, max(CASE WHEN metric = 'log_growth' THEN segment END) AS win "
        f"FROM gold.validation_results WHERE validation_version = '{vv}' AND category = "
        "'sensitivity' AND geography_id = 'scope' GROUP BY experiment").dropna(subset=["shift"])
    sens = sens[sens.experiment != "baseline"].sort_values("shift")
    fig, (a, b) = plt.subplots(1, 2, figsize=(9, 3.6), gridspec_kw={"width_ratios": [1.1, 1]})
    for exp, colour, lw in [("baseline", ACCENT, 2.4), ("threshold_0.3", MUTED, 1),
                            ("threshold_0.9", MUTED, 1), ("threshold_0.99", MUTED, 1),
                            ("deterministic_links", ALT, 1.2), ("method_huber", INK, 1),
                            ("with_new_first_leg", "#6d28d9", 1)]:
        xx, yy = s(exp)
        a.plot(xx, yy, color=colour, lw=lw, label=exp.replace("_", " "))
    a.set_title("Donegal index under alternative choices")
    a.legend(frameon=False, fontsize=7, ncol=2)
    colours = [WARN if abs(v) > 0.05 else ACCENT for v in sens["shift"]]
    labels = [f"{e.replace('_', ' ')}" + ("" if w == "2015-2025" else f" ({w})")
              for e, w in zip(sens.experiment, sens.win, strict=True)]
    b.barh(labels, sens["shift"], color=colours)
    b.axvline(0, color=MUTED, lw=0.6)
    for v in (-0.05, 0.05):
        b.axvline(v, color=WARN, lw=0.6, ls=":")
    b.set_xticks([-0.05, 0, 0.05, 0.1])
    b.set_title("Shift in 2015-2025 log growth vs headline")
    b.tick_params(axis="y", labelsize=7)
    fig.tight_layout()
    fig.savefig(args.out / f"{args.profile}_sensitivity.png", dpi=180)
    plt.close(fig)

    # 3. Backtest and calibration
    bt = t.query(
        "SELECT experiment AS method, metric, value FROM gold.validation_results "
        f"WHERE validation_version = '{bv}' AND category = 'backtest' "
        "AND segment = 'test|common' AND experiment <> 'town_relaxed'")
    md = bt[bt.metric == "median_abs_pct_error"].set_index("method").value
    order = ["nominal", "national_cso", "county", "local"]
    names = {"nominal": "Unchanged\nprice", "national_cso": "CSO\nnational",
             "county": "Donegal\nindex", "local": "Lookup\n(chain)"}
    cal = t.query(f"SELECT * FROM gold.prediction_calibration WHERE backtest_version = '{bv}'")
    fig, (a, b) = plt.subplots(1, 2, figsize=(9, 3.2))
    a.bar([names[m] for m in order], [md[m] for m in order],
          color=[MUTED, ALT, ACCENT, ACCENT])
    for i, m in enumerate(order):
        a.text(i, md[m] + 0.6, f"{md[m]:.1f}%", ha="center", fontsize=8)
    a.set_title("Median absolute error, test 2023-2025")
    a.set_ylabel("%")
    b.bar(cal.horizon_band, cal.coverage_test * 100, color=ACCENT)
    b.errorbar(cal.horizon_band, cal.coverage_test * 100,
               yerr=[(cal.coverage_test - cal.coverage_lo) * 100,
                     (cal.coverage_hi - cal.coverage_test) * 100], fmt="none", ecolor=INK)
    b.axhline(90, color=WARN, ls="--", lw=1)
    b.set_ylim(60, 100)
    b.set_title("90% interval: test coverage by holding period")
    b.set_ylabel("% of test sales inside interval")
    fig.tight_layout()
    fig.savefig(args.out / f"{args.profile}_backtest.png", dpi=180)
    plt.close(fig)
    # 4. Published index with bands, from the release itself
    vint = t.query(f"SELECT * FROM gold.index_vintages WHERE release_id = '{rid}' "
                   "AND frequency = 'annual' ORDER BY period")
    cx, cy = s("cso:border_houses")
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6), sharey=True)
    for ax, (geo, title, colour) in zip(axes, [("scope", "Donegal (annual)", ACCENT),
                                               ("town:Letterkenny", "Letterkenny (annual)",
                                                ALT)], strict=True):
        v = vint[vint.geography_id == geo]
        x = v.period.astype(int)
        ax.fill_between(x, v.level_lo, v.level_hi, color=colour, alpha=0.15,
                        label="95% bootstrap band")
        ax.plot(x, v.index_value, color=colour, lw=0.8, ls=":")
        for state, marker, face in [("supported", "o", colour), ("provisional", "o", "white"),
                                    ("suppressed", "x", MUTED)]:
            m = v.period_state == state
            style = ({"color": MUTED} if marker == "x"
                     else {"facecolors": face, "edgecolors": colour})
            ax.scatter(x[m], v.index_value[m], marker=marker, s=22, zorder=3, **style,
                       label={"supported": "published year", "provisional": "provisional",
                              "suppressed": "suppressed (too uncertain or thin)"}[state])
        ax.plot(cx, cy, color=MUTED, lw=1.2, label="CSO RPPI Border houses")
        ax.axhline(100, color=MUTED, lw=0.5, ls=":")
        ax.set_title(title, loc="left")
    axes[0].set_ylabel("Index (2015 = 100)")
    axes[1].legend(frameon=False, fontsize=7, loc="upper left")
    fig.suptitle(f"Published release {rid}: annual repeat-sales indices", x=0.01, ha="left",
                 fontsize=10, fontweight="bold")
    fig.tight_layout()
    fig.savefig(args.out / f"{args.profile}_release_index.png", dpi=180)
    plt.close(fig)
    print(f"charts for {rid} in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
