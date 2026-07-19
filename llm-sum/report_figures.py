"""
llm-sum/report_figures.py — Publication figures + VetHELM-style leaderboard
=============================================================================

WHY THIS MODULE EXISTS
-----------------------
``report_tables.py`` (Chunk 7) answers a paper's quantitative questions in
tables. This module answers the presentation question: what does a reader see
in five seconds, and what single artifact can be cited as "the leaderboard"?
It renders four offline PNG/SVG figures from the same
``data/evaluations.jsonl`` rows, and exports a named, taxonomy-keyed
leaderboard (JSON + Markdown) — per provider: jury score (both modes,
95% CI), cost, cost-per-quality-point, inter-judge reliability, and
human-validation correlation, wherever each is available.

DESIGN CONSTRAINTS (from CLAUDE.md)
-----------------------------------
- Offline only. Never calls a paid API; reads rows ``evaluator.py`` already
  wrote (plus, optionally, ``data/human_reviews.jsonl`` from Chunk 6).
- Reuses ``report_tables.build_publication_report`` for the provider
  comparison, significance context, and taxonomy header instead of
  re-deriving them — this module adds only what Chunk 7 does not already
  compute: the per-criterion breakdown, the rendered figures, and the
  per-provider reliability/human-validation slices the leaderboard needs.
- matplotlib renders headlessly via the "Agg" backend (set before importing
  ``pyplot``, and only inside the functions that need it, so importing this
  module has no side effects and no display server is required).

WHERE FIGURES ARE WRITTEN
--------------------------
Chunk 7's tables live under ``data/results/`` (git-ignored, alongside
``run_manifests/``); this module follows the same convention rather than
introducing a new top-level ``reports/`` folder — one gitignored output tree
for the whole reporting layer. Figures land in a timestamped
``data/results/figures_<ts>/`` subfolder next to that run's
``leaderboard_<ts>.json`` / ``.md``.

COLOR — dataviz skill, categorical palette (fixed order, never cycled)
------------------------------------------------------------------------
Providers are an identity axis, so each keeps the SAME categorical hue across
every chart in this module (and matches ``models_config.all_providers()``'s
canonical order): openai = blue, anthropic = aqua, gemini = yellow. The
per-criterion heatmap encodes magnitude (a score, not an identity), so it
uses a single-hue sequential ramp instead. The reliability plot encodes a
signed statistic (Krippendorff's alpha, where 0 = chance agreement), so it
uses the diverging blue/red pair with a neutral zero-line — the correct
mapping for a quantity with a meaningful, non-extreme midpoint. Figures are
static (PNG/SVG for a manuscript), so only the light-surface palette is used;
the interactive dark-mode variant in the skill does not apply here.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import report_tables as rt
import stats_engine
from human_review import HUMAN_REVIEWS_PATH, human_vs_jury_by_provider, iter_human_review_rows
from reliability import CRITERIA, compute_reliability
from eval_report import iter_evaluation_rows

EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
RESULTS_DIR = DATA_DIR / "results"

DEFAULT_FORMATS = ("png", "svg")

# ---------------------------------------------------------------------------
# Palette (dataviz skill reference palette — see module docstring)
# ---------------------------------------------------------------------------

# Categorical slots 1-3, fixed order, one per provider. Never reassigned by
# rank (e.g. "whoever's on top gets blue") — always keyed by provider name so
# a provider's color is stable across every chart and every run.
PROVIDER_COLORS: dict[str, str] = {
    "openai": "#2a78d6",     # slot 1: blue
    "anthropic": "#1baf7a",  # slot 2: aqua
    "gemini": "#eda100",     # slot 3: yellow
}
FALLBACK_COLOR = "#898781"  # muted ink, for any provider outside the fixed set

# Sequential ramp (single hue, light -> dark) for the per-criterion heatmap.
SEQUENTIAL_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#1c5cab", "#0d366b"]

# Diverging pair for the reliability plot (alpha is signed; 0 = chance agreement).
DIVERGING_POSITIVE = "#2a78d6"
DIVERGING_NEGATIVE = "#e34948"
DIVERGING_NEUTRAL = "#c3c2b7"

CHART_SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"


def _provider_color(provider: str) -> str:
    return PROVIDER_COLORS.get(provider, FALLBACK_COLOR)


def _lazy_pyplot():
    """Import matplotlib with the headless Agg backend, only when needed.

    Keeping this out of module scope means `import report_figures` alone
    (e.g. from a test that only checks the leaderboard) never touches
    matplotlib, and the backend is always set before pyplot is first
    imported anywhere in the process.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _strip_spines(ax) -> None:
    """Recessive chrome: no top/right border, muted gridline-colored axes."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRIDLINE)
    ax.tick_params(colors=INK_SECONDARY)


# ===========================================================================
# Per-criterion aggregation (the one thing report_tables.py does not compute)
# ===========================================================================

def aggregate_criterion_means(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """``{provider: {criterion: mean_score}}`` from raw evaluations.jsonl rows.

    A thin projection over ``report_tables.collect_item_scores`` — the same
    substrate the leaderboard composite is built from. That sharing is the
    point: this function used to pool judge rows flat, while the composite
    averaged judges within a paper first, so a single ``main()`` run could
    render a heatmap and a bar chart that disagreed about the identical data.
    Reading both from one collapse makes that contradiction impossible rather
    than merely fixed.

    Item-weighted throughout: judges are averaged within a paper, then papers
    are averaged, so a paper that happened to draw three judges does not count
    three times. Criteria a judge omitted are absent, never imputed.
    """
    item_scores = rt.collect_item_scores(rows)
    per_provider: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for by_provider in item_scores.values():
        for provider, agg in by_provider.items():
            for criterion, value in (agg.get("criteria") or {}).items():
                if value is not None:
                    per_provider[provider][criterion].append(value)
    return {
        provider: {c: round(sum(v) / len(v), 3) for c, v in crits.items() if v}
        for provider, crits in per_provider.items()
    }


# ===========================================================================
# Figures
# ===========================================================================

def build_provider_comparison_figure(provider_comparison: list[dict[str, Any]]):
    """Grouped bar chart: unweighted + weighted jury score with 95% CI error bars.

    Provider identity is already carried by the x-axis tick labels, so bar
    color encodes provider (fixed categorical slot) while a second,
    non-color channel — opacity — distinguishes unweighted vs. weighted. A
    color legend would be redundant with the x-axis; the alpha legend uses
    neutral gray swatches so it is never mistaken for a fourth provider.
    """
    if not provider_comparison:
        return None
    plt = _lazy_pyplot()
    from matplotlib.patches import Patch

    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=150)
    fig.patch.set_facecolor(CHART_SURFACE)
    ax.set_facecolor(CHART_SURFACE)

    providers = [row["provider"] for row in provider_comparison]
    colors = [_provider_color(p) for p in providers]
    x = list(range(len(providers)))
    width = 0.32

    for offset, (key, alpha) in enumerate((("quality_unweighted", 1.0), ("quality_weighted", 0.55))):
        means = [row[key]["mean"] for row in provider_comparison]
        lows = [row[key]["ci_low"] for row in provider_comparison]
        highs = [row[key]["ci_high"] for row in provider_comparison]
        err_low = [(m - l) if (m is not None and l is not None) else 0.0 for m, l in zip(means, lows)]
        err_high = [(h - m) if (m is not None and h is not None) else 0.0 for m, h in zip(means, highs)]
        bar_x = [xi + (offset - 0.5) * width for xi in x]
        ax.bar(
            bar_x, [m or 0.0 for m in means], width=width, color=colors, alpha=alpha,
            edgecolor=CHART_SURFACE, linewidth=1,
            yerr=[err_low, err_high], capsize=3,
            error_kw={"ecolor": INK_SECONDARY, "linewidth": 1},
        )

    ax.set_xticks(x)
    ax.set_xticklabels(providers)
    ax.set_ylim(0, 5.6)
    ax.set_ylabel("Jury score (1-5)")
    ax.set_title("Provider comparison — jury score, 95% bootstrap CI")
    handles = [
        Patch(facecolor=INK_SECONDARY, alpha=1.0, label="Unweighted"),
        Patch(facecolor=INK_SECONDARY, alpha=0.55, label="Weighted"),
    ]
    ax.legend(handles=handles, frameon=False, loc="upper right")
    _strip_spines(ax)
    ax.yaxis.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    return fig


def build_cost_quality_figure(provider_comparison: list[dict[str, Any]]):
    """Cost-quality Pareto scatter: one direct-labeled point per provider.

    Points are categorical entities (providers), not a series with many
    members, so each is direct-labeled instead of carrying a legend — the
    skill's guidance for a small, fully-labeled point set.
    """
    points = [
        row for row in provider_comparison
        if row["cost"]["available"] and row["quality_unweighted"]["mean"] is not None
    ]
    if not points:
        return None
    plt = _lazy_pyplot()

    fig, ax = plt.subplots(figsize=(6, 4.5), dpi=150)
    fig.patch.set_facecolor(CHART_SURFACE)
    ax.set_facecolor(CHART_SURFACE)

    for row in points:
        color = _provider_color(row["provider"])
        x = row["cost"]["mean_usd"]
        y = row["quality_unweighted"]["mean"]
        ax.scatter([x], [y], s=140, color=color, edgecolor=CHART_SURFACE, linewidth=1.5, zorder=3)
        ax.annotate(
            row["provider"], (x, y), textcoords="offset points", xytext=(8, 6),
            color=INK_PRIMARY, fontsize=10,
        )

    ax.set_xlabel("Mean cost per summary (USD)")
    ax.set_ylabel("Unweighted jury score (1-5)")
    ax.set_title("Cost vs. quality")
    _strip_spines(ax)
    ax.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    return fig


def build_criterion_heatmap_figure(criterion_means: dict[str, dict[str, float]], providers: list[str]):
    """Provider x criterion heatmap: one sequential hue, value-labeled cells.

    Magnitude (a 1-5 score), not identity, so this deliberately breaks from
    the categorical provider colors used elsewhere: sequential encoding is
    the skill's rule for continuous magnitude. Every cell is also labeled
    with its numeric value, since color alone cannot carry the precision a
    reader needs to compare 4.1 vs. 4.3.
    """
    ordered_providers = [p for p in providers if p in criterion_means]
    if not ordered_providers:
        return None
    plt = _lazy_pyplot()
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("vet_sequential_blue", SEQUENTIAL_RAMP)
    matrix = [
        [criterion_means[p].get(c, float("nan")) for c in CRITERIA]
        for p in ordered_providers
    ]

    fig, ax = plt.subplots(figsize=(7.5, 1.4 + 0.6 * len(ordered_providers)), dpi=150)
    fig.patch.set_facecolor(CHART_SURFACE)
    im = ax.imshow(matrix, cmap=cmap, vmin=1, vmax=5, aspect="auto")
    ax.set_xticks(range(len(CRITERIA)))
    ax.set_xticklabels([c.replace("_", " ") for c in CRITERIA], rotation=20, ha="right")
    ax.set_yticks(range(len(ordered_providers)))
    ax.set_yticklabels(ordered_providers)
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            if value == value:  # not NaN
                text_color = INK_PRIMARY if value < 3.4 else "#ffffff"
                ax.text(j, i, f"{value:.2f}", ha="center", va="center", color=text_color, fontsize=9)
    ax.set_title("Mean score per criterion")
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Score (1-5)")
    fig.tight_layout()
    return fig


def build_reliability_figure(reliability: dict[str, Any]):
    """Horizontal bar chart of Krippendorff's alpha, overall + per-criterion.

    Diverging blue/red around zero: alpha has a meaningful non-extreme
    midpoint (0 = chance-level agreement, negative = systematic
    disagreement), the textbook case for a diverging scale rather than a
    sequential one. Returns None (nothing to plot) when reliability is
    unavailable — a single-judge run, the project's default.
    """
    if not reliability.get("available"):
        return None
    labels = ["jury_score", *CRITERIA]
    alphas = [reliability["jury_score"]["krippendorff_alpha"]]
    for criterion in CRITERIA:
        alphas.append((reliability["per_criterion"].get(criterion) or {}).get("krippendorff_alpha"))
    pairs = [(label, alpha) for label, alpha in zip(labels, alphas) if alpha is not None]
    if not pairs:
        return None
    labels, alphas = zip(*pairs)

    plt = _lazy_pyplot()
    colors = [DIVERGING_POSITIVE if a >= 0 else DIVERGING_NEGATIVE for a in alphas]

    fig, ax = plt.subplots(figsize=(6.5, 0.6 * len(labels) + 1.5), dpi=150)
    fig.patch.set_facecolor(CHART_SURFACE)
    ax.set_facecolor(CHART_SURFACE)
    y = list(range(len(labels)))
    ax.barh(y, alphas, color=colors, edgecolor=CHART_SURFACE)
    ax.axvline(0, color=DIVERGING_NEUTRAL, linewidth=1)
    ax.axvline(0.667, color=INK_MUTED, linewidth=1, linestyle="--")  # Krippendorff's tentative-conclusion floor
    ax.set_yticks(y)
    ax.set_yticklabels([label.replace("_", " ") for label in labels])
    ax.invert_yaxis()
    ax.set_xlim(-1, 1)
    ax.set_xlabel("Krippendorff's alpha")
    ax.set_title(f"Inter-judge reliability ({', '.join(reliability.get('judges', []))})")
    _strip_spines(ax)
    fig.tight_layout()
    return fig


def save_figures(
    figures: dict[str, Any], out_dir: Path, *, formats: tuple[str, ...] = DEFAULT_FORMATS,
) -> dict[str, list[Path]]:
    """Save each non-None figure to ``out_dir`` in every requested format."""
    if not any(fig is not None for fig in figures.values()):
        return {}
    plt = _lazy_pyplot()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, list[Path]] = {}
    for name, fig in figures.items():
        if fig is None:
            continue
        paths: list[Path] = []
        for fmt in formats:
            path = out_dir / f"{name}.{fmt}"
            fig.savefig(path, facecolor=fig.get_facecolor())
            paths.append(path)
        saved[name] = paths
        plt.close(fig)
    return saved


# ===========================================================================
# Leaderboard
# ===========================================================================

def _human_validation_entry(by_provider: dict[str, dict[str, Any]], provider: str) -> dict[str, Any]:
    """One leaderboard cell's worth of human-vs-jury correlation, or 'unavailable'."""
    entry = by_provider.get(provider)
    overall = (entry or {}).get("overall") or {}
    if not overall.get("n"):
        return {
            "available": False,
            "reason": (
                "No human_reviews.jsonl rows for this provider yet. Run "
                "'export-human-review', have reviewers fill the scoresheets, then "
                "'ingest-human-review'."
            ),
        }
    return {
        "available": True,
        "n_items": overall.get("n"),
        "spearman": overall.get("spearman"),
        "spearman_p": overall.get("spearman_p"),
        "pearson": overall.get("pearson"),
        "interpretation": overall.get("interpretation"),
    }


def build_leaderboard(
    rows: Iterable[dict[str, Any]],
    *,
    report: dict[str, Any] | None = None,
    cost_index: dict[tuple[str, str, str], float] | None = None,
    human_rows: Iterable[dict[str, Any]] | None = None,
    seed: int = rt.DEFAULT_BOOTSTRAP_SEED,
    n_resamples: int = rt.DEFAULT_BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """Assemble the named, taxonomy-keyed leaderboard from evaluation rows.

    ``report`` lets a caller (the CLI below) reuse an already-built
    ``report_tables.build_publication_report`` result instead of recomputing
    the bootstrap CIs twice. Per-provider reliability filters the evaluation
    rows to that provider's summaries before calling
    ``reliability.compute_reliability`` — the same engine as the pooled
    figure, just scoped to "do judges agree on THIS provider specifically."
    Human-vs-jury correlation is similarly scoped via
    ``human_review.human_vs_jury_by_provider``. Both degrade to
    ``available: False`` with a plain-English reason rather than raising, so
    a leaderboard always has the same shape whether or not a jury run or a
    human-validation pass has happened yet.
    """
    materialized = list(rows)
    if report is None:
        report = rt.build_publication_report(
            materialized, cost_index=cost_index, seed=seed, n_resamples=n_resamples,
        )

    human_materialized = list(human_rows) if human_rows is not None else []
    human_by_provider = human_vs_jury_by_provider(human_materialized) if human_materialized else {}

    entries: list[dict[str, Any]] = []
    for row in report["provider_comparison"]:
        provider = row["provider"]
        provider_rows = [r for r in materialized if str(r.get("summarizer", "")).strip() == provider]
        provider_reliability = compute_reliability(provider_rows)
        entries.append({
            "provider": provider,
            "n_items": row["n_items"],
            "quality_unweighted": row["quality_unweighted"],
            "quality_weighted": row["quality_weighted"],
            "cost": row["cost"],
            "cost_per_quality_point": row["cost_per_quality_point"],
            "reliability": {
                "available": provider_reliability["available"],
                "krippendorff_alpha": (provider_reliability.get("jury_score") or {}).get("krippendorff_alpha"),
                "n_comparable_items": provider_reliability.get("n_comparable_items", 0),
                "reason": provider_reliability.get("reason"),
            },
            "human_validation": _human_validation_entry(human_by_provider, provider),
        })

    return {
        "leaderboard_name": "VetHELM-style Veterinary Summarization Leaderboard",
        "taxonomy": report["taxonomy"],
        "generated_at": None,  # stamped by the CLI, kept out of the pure builder
        "n_evaluation_rows": report["n_evaluation_rows"],
        "n_items": report["n_items"],
        "overall_reliability": report["reliability"],
        "entries": entries,
        "notes": [
            *report["notes"],
            "Per-provider reliability filters evaluations to that provider's "
            "summaries before computing Krippendorff's alpha ('do judges agree on "
            "THIS provider'), separately from the pooled figure above.",
            "Per-provider human validation correlates only that provider's reviewed "
            "items. A typical human-review sample (e.g. 15 items split three ways) "
            "is usually below reliability.MIN_CORRELATION_N — read the coefficient "
            "as a signal, not a conclusion, until more items are reviewed.",
        ],
    }


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_ci(block: dict[str, Any]) -> str:
    mean = block.get("mean")
    if mean is None:
        return "-"
    low, high = block.get("ci_low"), block.get("ci_high")
    if low is None or high is None:
        return f"{mean:.2f} (n<2)"
    return f"{mean:.2f} [{low:.2f}, {high:.2f}]"


def render_leaderboard_markdown(leaderboard: dict[str, Any]) -> str:
    taxonomy = leaderboard.get("taxonomy") or {}
    lines = [f"# {leaderboard['leaderboard_name']}", ""]
    if taxonomy:
        lines.append(
            f"Benchmark: **{taxonomy.get('taxonomy_id')} v{taxonomy.get('taxonomy_version')}** "
            f"({taxonomy.get('category_display_name')} / {taxonomy.get('task_display_name')})."
        )
    lines.append(
        f"{leaderboard['n_evaluation_rows']} evaluation row(s) across "
        f"{leaderboard['n_items']} item(s) (paper x input channel)."
    )
    lines.append("")

    if not leaderboard["entries"]:
        lines.append("No scored evaluation rows found. Run `evaluate` first, then re-run.")
        return "\n".join(lines) + "\n"

    lines.append(
        "| Rank | Provider | N | Unweighted (95% CI) | Weighted (95% CI) | "
        "Mean cost (USD) | Reliability (alpha) | Human r_s (n) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for rank, entry in enumerate(leaderboard["entries"], start=1):
        rel = entry["reliability"]
        hv = entry["human_validation"]
        rel_str = (
            f"{rel['krippendorff_alpha']:.2f}"
            if rel.get("available") and rel.get("krippendorff_alpha") is not None else "-"
        )
        hv_str = (
            f"{hv['spearman']:.2f} (n={hv['n_items']})"
            if hv.get("available") and hv.get("spearman") is not None else "-"
        )
        cost = entry["cost"]
        lines.append(
            f"| {rank} | {entry['provider']} | {entry['n_items']} | "
            f"{_fmt_ci(entry['quality_unweighted'])} | {_fmt_ci(entry['quality_weighted'])} | "
            f"{_fmt(cost['mean_usd'], 5) if cost['available'] else '-'} | {rel_str} | {hv_str} |"
        )
    lines.append("")

    lines.append("## Overall inter-judge reliability")
    lines.append("")
    overall = leaderboard.get("overall_reliability") or {}
    if overall.get("available"):
        jury = overall.get("jury_score") or {}
        lines.append(
            f"Krippendorff's alpha (jury score, all providers pooled): "
            f"**{jury.get('krippendorff_alpha')}** across judges "
            f"{', '.join(overall.get('judges', []))}. {overall.get('interpretation', '')}"
        )
    else:
        lines.append(overall.get("reason", "Single-judge run: no inter-judge agreement to report."))
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    for note in leaderboard.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines) + "\n"


def save_leaderboard(
    leaderboard: dict[str, Any], markdown: str, ts: str, out_dir: Path = RESULTS_DIR,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"leaderboard_{ts}.json"
    json_path.write_text(json.dumps(leaderboard, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path = out_dir / f"leaderboard_{ts}.md"
    md_path.write_text(markdown, encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


# ===========================================================================
# CLI
# ===========================================================================

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[report_figures] WARNING: invalid {name}={raw!r}; using {default}.")
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publication figures (PNG/SVG) and a VetHELM-style leaderboard "
                    "(JSON + Markdown) from data/evaluations.jsonl.",
    )
    parser.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    parser.add_argument("--summaries", type=Path, default=SUMMARIES_PATH,
                        help="Source of summariser token counts for cost columns "
                             "(default: data/summaries.jsonl).")
    parser.add_argument("--human-reviews", type=Path, default=None,
                        help="Normalized human-review rows (default: data/human_reviews.jsonl). "
                             "Missing/empty is fine — the leaderboard reports "
                             "human_validation.available=false.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR,
                        help="Where to save the leaderboard + figures (default: data/results/).")
    parser.add_argument("--seed", type=int, default=None,
                        help="Bootstrap seed (default: PUBLICATION_BOOTSTRAP_SEED or 42).")
    parser.add_argument("--bootstrap-resamples", type=int, default=None,
                        help="Bootstrap resamples per CI "
                             "(default: PUBLICATION_BOOTSTRAP_RESAMPLES or 2000).")
    parser.add_argument("--cost-batched", action="store_true",
                        help="Price summaries at batch (50%%-off) rates. "
                             "Overrides PUBLICATION_COST_BATCHED.")
    parser.add_argument("--no-figures", action="store_true",
                        help="Write only the leaderboard JSON/Markdown; skip PNG/SVG figures.")
    parser.add_argument("--formats", default=None,
                        help="Comma-separated figure formats "
                             "(default: PUBLICATION_FIGURE_FORMATS or 'png,svg').")
    args = parser.parse_args(argv)

    seed = args.seed if args.seed is not None else _env_int("PUBLICATION_BOOTSTRAP_SEED", rt.DEFAULT_BOOTSTRAP_SEED)
    n_resamples = (
        args.bootstrap_resamples if args.bootstrap_resamples is not None
        else _env_int("PUBLICATION_BOOTSTRAP_RESAMPLES", rt.DEFAULT_BOOTSTRAP_RESAMPLES)
    )
    cost_batched = args.cost_batched or _env_bool("PUBLICATION_COST_BATCHED", False)
    formats_raw = args.formats or os.getenv("PUBLICATION_FIGURE_FORMATS", "png,svg")
    formats = tuple(f.strip() for f in formats_raw.split(",") if f.strip()) or DEFAULT_FORMATS

    rows = list(iter_evaluation_rows(args.evaluations))
    if not rows:
        print("[report_figures] No evaluation rows found yet. Run 'evaluate' first, then re-run.")
        return 0

    cost_index = rt.build_summary_cost_index(args.summaries, batched=cost_batched)
    human_reviews_path = args.human_reviews if args.human_reviews is not None else HUMAN_REVIEWS_PATH
    human_rows = list(iter_human_review_rows(human_reviews_path))

    report = rt.build_publication_report(
        rows, cost_index=cost_index, seed=seed, n_resamples=n_resamples, cost_batched=cost_batched,
        summaries_by_doi=stats_engine.load_summaries_by_provider(args.summaries),
        human_review_rows=human_rows,
    )
    leaderboard = build_leaderboard(rows, report=report, human_rows=human_rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    leaderboard["generated_at"] = ts
    markdown = render_leaderboard_markdown(leaderboard)
    saved = save_leaderboard(leaderboard, markdown, ts, args.results_dir)
    print(f"[report_figures] leaderboard JSON: {saved['json']}")
    print(f"[report_figures] leaderboard Markdown: {saved['markdown']}")

    if not args.no_figures:
        criterion_means = aggregate_criterion_means(rows)
        figures = {
            "provider_comparison": build_provider_comparison_figure(report["provider_comparison"]),
            "cost_quality": build_cost_quality_figure(report["provider_comparison"]),
            "criterion_heatmap": build_criterion_heatmap_figure(criterion_means, report["providers"]),
            "reliability": build_reliability_figure(report["reliability"]),
        }
        fig_dir = args.results_dir / f"figures_{ts}"
        saved_figs = save_figures(figures, fig_dir, formats=formats)
        if saved_figs:
            print(f"[report_figures] figures written to {fig_dir}")
            for name, paths in saved_figs.items():
                print(f"  {name}: {', '.join(p.name for p in paths)}")
        else:
            print("[report_figures] no figures could be generated (insufficient data).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
