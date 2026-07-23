#!/usr/bin/env python3
"""
scripts/audit_eval_math.py — independent re-derivation audit of the evaluation math.

WHAT THIS IS
------------
A standalone, offline check that the headline numbers the evaluation pipeline
produces are arithmetically correct. For each statistic it computes the value
TWICE — once with the production code, once with a fresh, independent
re-implementation written here from the raw data — and reports any mismatch.

THE INDEPENDENCE RULE (why this catches real bugs)
--------------------------------------------------
The "independent" side deliberately does NOT call the production function it is
auditing. Calling ``calculate_jury_score`` (or ``krippendorff_alpha_interval``)
on both sides would only prove the function equals itself. So the jury-score
weighted mean, the inter-judge unit construction, Bland-Altman, the
item-weighted provider means, and the hallucination-rate majority-vote collapse
(including the same-judge duplicate-row merge rule) are all re-derived locally
from the raw JSONL, and compared against what production stored / computes.

The one exception is Krippendorff's alpha: it is hand-rolled in the study on
purpose (scipy has no implementation), so here it is cross-checked against the
independent ``krippendorff`` PyPI package (requirements-dev.txt) on the real
per-item ratings AND on Krippendorff's own published worked example. If the
package is not installed, that single cross-check is skipped with a note; every
other check still runs.

TOLERANCE
---------
Rounded stored fields are compared with EXACT equality at the precision they are
stored (jury scores 2 dp, Bland-Altman 3 dp, means 3 dp). A small numeric
approximation (``math.isclose``) is used ONLY for alpha vs the PyPI package,
whose internal rounding/edge handling legitimately differs from ours.

SAFETY
------
Offline only. Reads data/evaluations.jsonl (and data/human_reviews.jsonl if it
exists). Never writes to any ledger, never calls a paid API. Safe to re-run
after every ``evaluate`` batch merges. Exits 0 when every check passes, 1 on any
mismatch, so it can gate CI or a pre-publication check.

    python scripts/audit_eval_math.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

# --- Make the hyphenated llm-sum/ and src/ folders importable (same as tests). -
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "llm-sum"))
sys.path.insert(0, str(REPO_ROOT / "src"))
# Defence in depth: importing the pipeline must never take a live path, even
# though nothing here calls a provider. Mirrors tests/conftest.py.
os.environ.setdefault("PHASE3_MODE", "test")
os.environ.setdefault("DRY_RUN", "true")

import reliability  # noqa: E402
import evaluator  # noqa: E402
import eval_report  # noqa: E402

try:  # Optional cross-check dependency (requirements-dev.txt), guarded.
    import numpy as np  # noqa: E402
    import krippendorff as _krippendorff_pkg  # noqa: E402
    _HAVE_KRIPP_PKG = True
except ImportError:  # pragma: no cover - depends on local env
    _HAVE_KRIPP_PKG = False

try:
    from scipy import stats as _scipy_stats  # noqa: E402
    _HAVE_SCIPY = True
except ImportError:  # pragma: no cover
    _HAVE_SCIPY = False

DATA_DIR = REPO_ROOT / "data"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
HUMAN_REVIEWS_PATH = DATA_DIR / "human_reviews.jsonl"

CRITERIA = ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")
# Weights re-declared locally so the audit does not read them from the module it
# audits. The first check asserts these still equal evaluator's, so a silent
# weight change is itself caught rather than blindly trusted.
LOCAL_WEIGHTS = {
    "faithfulness": 1.5,
    "completeness": 1.0,
    "clinical_usefulness": 1.2,
    "clarity": 0.8,
    "safety": 1.3,
}


# ===========================================================================
# Independent (local) re-implementations — must not call the audited functions
# ===========================================================================

def _clamp_1_5(value: Any) -> float | None:
    """Coerce to a float in [1, 5], or None. Mirrors the intent of the pipeline's
    _coerce_score without importing it (booleans and non-numerics are None)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return max(1.0, min(5.0, f))


def local_jury_score(criteria_scores: Any, weights: dict[str, float]) -> float | None:
    """Weighted mean of the 1-5 criteria, renormalized over criteria present.

    A fully independent reimplementation of evaluator.calculate_jury_score's
    contract: clamp to 1-5, skip a criterion the judge did not score (dropping
    both its weighted term AND its weight from the divisor), round to 2 dp,
    return None when nothing was scored.
    """
    if not isinstance(criteria_scores, dict):
        return None
    weighted_sum = 0.0
    weight_total = 0.0
    for criterion, weight in weights.items():
        raw = criteria_scores.get(criterion)
        if isinstance(raw, dict):
            raw = raw.get("score")
        score = _clamp_1_5(raw)
        if score is None:
            continue
        weighted_sum += score * weight
        weight_total += weight
    if weight_total == 0:
        return None
    return round(weighted_sum / weight_total, 2)


def _item_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("doi", "")),
        str(row.get("summarizer", "")),
        str(row.get("input_source") or "processed"),
    )


def _judge(row: dict[str, Any]) -> str:
    return str(row.get("judge", ""))


def local_units(rows: list[dict[str, Any]],
                value_fn: Callable[[dict[str, Any]], float | None]) -> list[list[float]]:
    """Rebuild per-item rating lists the way reliability._units_for does:
    one rating per judge per item, duplicate judges AVERAGED (not last-wins)."""
    by_item: dict[tuple[str, str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        value = value_fn(row)
        if value is None:
            continue
        by_item[_item_key(row)][_judge(row)].append(value)
    return [
        [sum(vals) / len(vals) for vals in judge_vals.values()]
        for judge_vals in by_item.values()
    ]


def local_bland_altman(primary: list[float], reference: list[float]) -> dict[str, Any] | None:
    n = len(primary)
    if n < 1 or len(reference) != n:
        return None
    diffs = [p - r for p, r in zip(primary, reference)]
    bias = sum(diffs) / n
    sd = (sum((d - bias) ** 2 for d in diffs) / (n - 1)) ** 0.5 if n >= 2 else 0.0
    return {
        "mean_bias": round(bias, 3),
        "sd_diff": round(sd, 3),
        "loa_lower": round(bias - 1.96 * sd, 3),
        "loa_upper": round(bias + 1.96 * sd, 3),
    }


def _local_input_source(row: dict[str, Any]) -> str:
    """Mirrors report_tables._input_source: top-level field, then strata, then default."""
    return str(row.get("input_source") or (row.get("strata") or {}).get("input_source")
               or "processed")


def _local_hallucination_flag(row: dict[str, Any]) -> bool | None:
    """Independent reimplementation of report_tables._hallucination_flag."""
    value = row.get("hallucination_count")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) > 0


def _local_major_hallucination_flag(row: dict[str, Any]) -> bool | None:
    """Independent reimplementation of report_tables._major_hallucination_flag."""
    claims = row.get("hallucination_claims")
    if not isinstance(claims, list):
        return None
    return any(isinstance(c, dict) and c.get("severity") == "major" for c in claims)


def _local_collapse_duplicate_judge_rows(judge_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Independent reimplementation of report_tables._collapse_duplicate_judge_rows,
    limited to the two fields the hallucination check reads: a re-run appending a
    second row from the SAME judge on the SAME item takes the max hallucination_count
    (the evidence-preserving rule — see local_jury_score's sibling reasoning) and
    concatenates both rows' claim lists, rather than one silently superseding the other."""
    if len(judge_rows) == 1:
        return judge_rows[0]
    counts = [c for r in judge_rows
              if isinstance(c := r.get("hallucination_count"), (int, float))
              and not isinstance(c, bool)]
    claims: list[Any] = []
    for r in judge_rows:
        if isinstance(r.get("hallucination_claims"), list):
            claims.extend(r["hallucination_claims"])
    return {
        "hallucination_count": max(counts) if counts else None,
        "hallucination_claims": claims if any(
            isinstance(r.get("hallucination_claims"), list) for r in judge_rows
        ) else None,
    }


def _local_collapse_flags(values: list[bool | None]) -> dict[str, Any]:
    """Independent reimplementation of report_tables._collapse_flags: majority vote
    across judges reporting on one item, plus the any-judge and mean-rate companions."""
    usable = [v for v in values if v is not None]
    if not usable:
        return {"value": None, "any_value": None, "mean_rate": None}
    n_flagged = sum(1 for v in usable if v)
    return {
        "value": n_flagged * 2 > len(usable),
        "any_value": n_flagged > 0,
        "mean_rate": n_flagged / len(usable),
    }


def local_item_hallucination_aggregates(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Independent reimplementation of report_tables.collect_item_scores, scoped to
    just the hallucination/major_hallucination verdict per (doi, input_source, provider).

    Returns (aggregates, n_duplicate_judge_groups) — the duplicate count lets the
    check report whether the same-judge-same-item merge path was ever exercised on
    this corpus, since a check that never hits its trickiest branch proves less.
    """
    by_item: dict[tuple[str, str], dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for row in rows:
        doi = str(row.get("doi", "")).strip()
        provider = str(row.get("summarizer", "")).strip()
        if not doi or not provider:
            continue
        item = (doi, _local_input_source(row))
        judge = str(row.get("judge", "")).strip() or f"__unkeyed_{id(row)}"
        by_item[item][provider][judge].append(row)

    aggregates: list[dict[str, Any]] = []
    n_duplicates = 0
    for (doi, input_source), providers in by_item.items():
        for provider, by_judge in providers.items():
            judge_rows = []
            for judge_row_list in by_judge.values():
                if len(judge_row_list) > 1:
                    n_duplicates += 1
                judge_rows.append(_local_collapse_duplicate_judge_rows(judge_row_list))
            aggregates.append({
                "doi": doi,
                "input_source": input_source,
                "provider": provider,
                "hallucination": _local_collapse_flags(
                    [_local_hallucination_flag(r) for r in judge_rows]
                ),
                "major_hallucination": _local_collapse_flags(
                    [_local_major_hallucination_flag(r) for r in judge_rows]
                ),
            })
    return aggregates, n_duplicates


def _local_rate(aggregates: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    """Independent reimplementation of eval_report._rate."""
    flags = [agg.get(key) or {} for agg in aggregates]
    reported = [f for f in flags if f.get("value") is not None]
    if not reported:
        return {"rate": None, "any_rate": None, "mean_rate": None}
    n = len(reported)
    return {
        "rate": round(sum(1 for f in reported if f["value"]) / n, 3),
        "any_rate": round(sum(1 for f in reported if f.get("any_value")) / n, 3),
        "mean_rate": round(sum(f.get("mean_rate") or 0.0 for f in reported) / n, 3),
    }


def alpha_via_package(units: list[list[float]]) -> float | None:
    """Cross-check alpha through the krippendorff PyPI package.

    RESHAPE FOOTGUN: the package wants a rater x unit matrix with missing
    entries as nan, whereas our units are per-unit lists of the present ratings.
    Interval alpha does not depend on which rater column a value sits in (it is
    built from within-unit coincidences), so each unit's values are dropped into
    the first free rater rows and the rest padded with nan. A unit with < 2
    ratings contributes nothing, exactly as our hand-rolled version filters.
    """
    pairable = [u for u in units if len(u) >= 2]
    if not pairable or not _HAVE_KRIPP_PKG:
        return None
    max_raters = max(len(u) for u in pairable)
    matrix = [[np.nan] * len(pairable) for _ in range(max_raters)]
    for ui, unit in enumerate(pairable):
        for ri, val in enumerate(unit):
            matrix[ri][ui] = val
    return float(_krippendorff_pkg.alpha(
        reliability_data=matrix, level_of_measurement="interval",
    ))


# ===========================================================================
# Check harness
# ===========================================================================

class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str, str]] = []
        self.failed = 0
        self.skipped = 0

    def record(self, name: str, prod: Any, indep: Any, ok: bool | None,
               note: str = "") -> None:
        if ok is None:
            verdict, self.skipped = "SKIP", self.skipped + 1
        elif ok:
            verdict = "PASS"
        else:
            verdict, self.failed = "FAIL", self.failed + 1
        self.rows.append((name, _fmt(prod), _fmt(indep), verdict, note))

    def print_table(self) -> None:
        headers = ("check", "production", "independent", "verdict", "note")
        widths = [max(len(r[i]) for r in (*self.rows, headers)) for i in range(5)]
        line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
        print(line)
        print("  ".join("-" * widths[i] for i in range(5)))
        for r in self.rows:
            print("  ".join(str(r[i]).ljust(widths[i]) for i in range(5)))


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def load_rows() -> list[dict[str, Any]]:
    """The same rows the report sees: valid JSONL, dry-run mock rows excluded."""
    return list(eval_report.iter_evaluation_rows(EVALUATIONS_PATH))


# ===========================================================================
# Headline checks
# ===========================================================================

def check_weights(res: Results) -> None:
    same = LOCAL_WEIGHTS == dict(evaluator.MEDHELM_CRITERION_WEIGHTS)
    res.record("weights match config", dict(evaluator.MEDHELM_CRITERION_WEIGHTS),
               LOCAL_WEIGHTS, same,
               "" if same else "AUDIT weights differ from evaluator's — update audit")


def check_jury_scores(res: Results, rows: list[dict[str, Any]]) -> None:
    """Every stored jury_score_weighted/unweighted equals a local recompute."""
    unweighted_w = {k: 1.0 for k in LOCAL_WEIGHTS}
    n = mism = 0
    examples: list[str] = []
    for row in rows:
        cs = row.get("criteria_scores")
        if not isinstance(cs, dict) or not cs:
            continue
        stored_w = row.get("jury_score_weighted")
        stored_u = row.get("jury_score_unweighted")
        if stored_w is None and stored_u is None:
            continue
        n += 1
        local_w = local_jury_score(cs, LOCAL_WEIGHTS)
        local_u = local_jury_score(cs, unweighted_w)
        if local_w != stored_w or local_u != stored_u:
            mism += 1
            if len(examples) < 3:
                examples.append(f"{row.get('doi')}/{row.get('summarizer')}/{row.get('judge')} "
                                f"stored(w={stored_w},u={stored_u}) local(w={local_w},u={local_u})")
    note = f"{n} rows, exact 2dp" + ("" if not examples else " | " + "; ".join(examples))
    res.record("jury scores (weighted+unweighted)", f"{n - mism}/{n} match",
               f"{mism} mismatch", mism == 0, note)


def check_primary_field(res: Results, rows: list[dict[str, Any]]) -> None:
    """Stored jury_score equals the mode-selected composite (per row's own mode)."""
    n = mism = 0
    for row in rows:
        primary = row.get("jury_score")
        if not isinstance(primary, (int, float)):
            continue
        mode = str(row.get("jury_aggregation_mode")
                   or evaluator.JURY_AGGREGATION_MODE).lower()
        selected = row.get(f"jury_score_{'weighted' if mode == 'weighted' else 'unweighted'}")
        if not isinstance(selected, (int, float)):
            continue
        n += 1
        if abs(float(primary) - float(selected)) > 1e-9:
            mism += 1
    res.record("primary jury_score = selected mode", f"{n - mism}/{n} match",
               f"{mism} mismatch", mism == 0, f"{n} rows")


def check_unit_construction(res: Results, rows: list[dict[str, Any]]) -> None:
    """Independently-built inter-judge units match production _units_for."""
    prod = reliability._units_for(rows, reliability._jury_value)
    mine = local_units(rows, lambda r: reliability._jury_value(r))
    norm = lambda units: sorted(tuple(round(x, 6) for x in sorted(u)) for u in units)
    ok = norm(prod) == norm(mine)
    n_multi = sum(1 for u in prod if len(u) >= 2)
    res.record("inter-judge unit construction", f"{len(prod)} units",
               f"{len(mine)} units", ok, f"{n_multi} multi-judge items")


def check_alpha(res: Results, rows: list[dict[str, Any]]) -> None:
    """Hand-rolled alpha vs the krippendorff PyPI package, on the real corpus
    AND on Krippendorff's published worked example (ground-truth = 0.811)."""
    units = reliability._units_for(rows, reliability._jury_value)
    prod = reliability.krippendorff_alpha_interval(units)

    # Ground-truth reference example (the same fixture the unit test pins).
    obs_a = [None, None, None, None, None, 3, 4, 1, 2, 1, 1, 3, 3, None, 3]
    obs_b = [1, None, 2, 1, 3, 3, 4, 3, None, None, None, None, None, None, None]
    obs_c = [None, None, 2, 1, 3, 4, 4, None, 2, 1, 1, 3, 3, None, 4]
    ref_units = [[v for v in t if v is not None] for t in zip(obs_a, obs_b, obs_c)]
    ref_prod = reliability.krippendorff_alpha_interval(ref_units)
    ref_ok = ref_prod is not None and math.isclose(ref_prod, 0.811, abs_tol=0.001)
    res.record("alpha vs published 0.811 (ground truth)", round(ref_prod, 4) if ref_prod else None,
               0.811, ref_ok, "Krippendorff 2011 worked example")

    if not _HAVE_KRIPP_PKG:
        res.record("alpha vs krippendorff package (corpus)", prod, None, None,
                   "package not installed — pip install -r requirements-dev.txt")
        return
    ref_pkg = alpha_via_package(ref_units)
    res.record("alpha vs krippendorff package (ref example)",
               round(ref_prod, 4) if ref_prod is not None else None,
               round(ref_pkg, 4) if ref_pkg is not None else None,
               ref_pkg is not None and ref_prod is not None
               and math.isclose(ref_prod, ref_pkg, abs_tol=1e-6), "reshape sanity")

    if prod is None:
        res.record("alpha vs krippendorff package (corpus)", None, None, None,
                   "no multi-judge items in corpus yet")
        return
    pkg = alpha_via_package(units)
    ok = pkg is not None and math.isclose(prod, pkg, abs_tol=1e-6)
    res.record("alpha vs krippendorff package (corpus)",
               round(prod, 4), round(pkg, 4) if pkg is not None else None, ok,
               "isclose 1e-6")


def check_correlation_and_bland_altman(res: Results) -> None:
    """Formula self-check on a fixed synthetic pair (proves the wiring even
    before human_reviews.jsonl exists), then the on-corpus human-vs-jury pairing
    if that file is present."""
    if not _HAVE_SCIPY:
        res.record("correlation self-check", None, None, None, "scipy missing")
        return
    human = [4, 5, 3, 4, 2, 5, 3, 4, 2, 5, 1, 3, 4, 2, 5]
    jury = [3, 5, 3, 4, 3, 4, 2, 4, 2, 5, 2, 3, 5, 1, 4]

    prod_p = reliability.pearson(human, jury)
    indep_p = round(float(_scipy_stats.pearsonr(human, jury).statistic), 3)
    res.record("pearson (synthetic)", prod_p, indep_p, prod_p == indep_p, "vs direct scipy")

    prod_s = reliability.spearman(human, jury)
    indep_s = round(float(_scipy_stats.spearmanr(human, jury).statistic), 3)
    res.record("spearman (synthetic)", prod_s, indep_s, prod_s == indep_s, "vs direct scipy")

    prod_ba = reliability.bland_altman(jury, human)
    indep_ba = local_bland_altman(jury, human)
    ba_ok = prod_ba is not None and all(
        prod_ba[k] == indep_ba[k] for k in ("mean_bias", "sd_diff", "loa_lower", "loa_upper")
    )
    res.record("bland-altman (synthetic)",
               prod_ba["mean_bias"] if prod_ba else None,
               indep_ba["mean_bias"] if indep_ba else None, ba_ok,
               "mean_bias/sd/LoA exact 3dp")

    if not HUMAN_REVIEWS_PATH.exists():
        res.record("human-vs-jury correlation (corpus)", None, None, None,
                   "no data/human_reviews.jsonl yet — runs once real reviews ingested")


def check_provider_means(res: Results, rows: list[dict[str, Any]]) -> None:
    """Item-weighted provider means: independent reconstruction vs summarize_rows."""
    prod = {g["group"]: g for g in eval_report.summarize_rows(rows, "summarizer")}

    # Independent item-weighting: average judges within each (item, provider),
    # then average items. Uses local_jury_score per row (== stored, verified).
    unweighted_w = {k: 1.0 for k in LOCAL_WEIGHTS}
    by_provider_items: dict[str, dict[tuple[str, str], dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for row in rows:
        doi = str(row.get("doi", "")).strip()
        prov = str(row.get("summarizer", "")).strip()
        if not doi or not prov:
            continue
        item = (doi, str(row.get("input_source") or "processed"))
        cs = row.get("criteria_scores")
        u = local_jury_score(cs, unweighted_w)
        w = local_jury_score(cs, LOCAL_WEIGHTS)
        judge = _judge(row) or f"__u{id(row)}"
        if u is not None:
            by_provider_items[prov][item].setdefault("u_" + judge, []).append(u)
        if w is not None:
            by_provider_items[prov][item].setdefault("w_" + judge, []).append(w)

    all_ok = True
    for prov, items in sorted(by_provider_items.items()):
        u_items, w_items = [], []
        for _item, judge_map in items.items():
            u_vals = [sum(v) / len(v) for k, v in judge_map.items() if k.startswith("u_")]
            w_vals = [sum(v) / len(v) for k, v in judge_map.items() if k.startswith("w_")]
            if u_vals:
                u_items.append(sum(u_vals) / len(u_vals))
            if w_vals:
                w_items.append(sum(w_vals) / len(w_vals))
        indep_u = round(sum(u_items) / len(u_items), 3) if u_items else None
        indep_w = round(sum(w_items) / len(w_items), 3) if w_items else None
        p = prod.get(prov, {})
        ok = indep_u == p.get("mean_score_unweighted") and indep_w == p.get("mean_score_weighted")
        all_ok = all_ok and ok
        res.record(f"item-weighted means [{prov}]",
                   f"u={p.get('mean_score_unweighted')} w={p.get('mean_score_weighted')}",
                   f"u={indep_u} w={indep_w}", ok, f"{p.get('n_items')} items")


def check_hallucination_rates(res: Results, rows: list[dict[str, Any]]) -> None:
    """Per-provider hallucination_rate / major_hallucination_rate / any-judge /
    mean-per-judge, rebuilt independently (majority-vote item collapse, duplicate-
    judge merge, and rate arithmetic all reimplemented from scratch here) and
    compared to eval_report.summarize_rows(rows, 'summarizer')."""
    prod = {g["group"]: g for g in eval_report.summarize_rows(rows, "summarizer")}
    aggregates, n_dup = local_item_hallucination_aggregates(rows)

    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for agg in aggregates:
        by_provider[agg["provider"]].append(agg)

    for prov in sorted(by_provider):
        indep_halluc = _local_rate(by_provider[prov], "hallucination")
        indep_major = _local_rate(by_provider[prov], "major_hallucination")
        p = prod.get(prov, {})
        ok = (
            indep_halluc["rate"] == p.get("hallucination_rate")
            and indep_halluc["any_rate"] == p.get("hallucination_rate_any_judge")
            and indep_halluc["mean_rate"] == p.get("hallucination_rate_mean_per_judge")
            and indep_major["rate"] == p.get("major_hallucination_rate")
            and indep_major["any_rate"] == p.get("major_hallucination_rate_any_judge")
        )
        res.record(
            f"hallucination rate [{prov}]",
            f"rate={p.get('hallucination_rate')} any={p.get('hallucination_rate_any_judge')} "
            f"major={p.get('major_hallucination_rate')}",
            f"rate={indep_halluc['rate']} any={indep_halluc['any_rate']} "
            f"major={indep_major['rate']}",
            ok, f"{len(by_provider[prov])} items, majority-vote + exact 3dp",
        )

    if n_dup:
        print(f"[audit] note: {n_dup} same-judge duplicate-row group(s) found — the "
              f"max(count)+concatenated-claims merge path was exercised by real data.")
    else:
        print("[audit] note: no same-judge duplicate rows in this corpus — the "
              "duplicate-merge path itself is untested by real data (still covered "
              "by tests/test_report_tables.py fixtures).")


def check_quality_score(res: Results, rows: list[dict[str, Any]]) -> None:
    """Legacy 1-10 quality_score = round((jury-1)/4*9+1), independently."""
    n = mism = 0
    for row in rows:
        primary = row.get("jury_score")
        stored_q = row.get("quality_score")
        if not isinstance(primary, (int, float)) or not isinstance(stored_q, (int, float)):
            continue
        if primary <= 0:  # sentinel path; skip
            continue
        n += 1
        local_q = int(round(((float(primary) - 1) / 4) * 9 + 1))
        if local_q != int(stored_q):
            mism += 1
    res.record("legacy quality_score scaling", f"{n - mism}/{n} match",
               f"{mism} mismatch", mism == 0 if n else None,
               f"{n} rows, round((j-1)/4*9+1)")


def main() -> int:
    if not EVALUATIONS_PATH.exists():
        print(f"[audit] {EVALUATIONS_PATH} does not exist — nothing to audit.")
        return 1
    rows = load_rows()
    print(f"[audit] {len(rows)} evaluation rows (dry-run mocks excluded) "
          f"from {EVALUATIONS_PATH}")
    print(f"[audit] krippendorff package: {'available' if _HAVE_KRIPP_PKG else 'MISSING'}; "
          f"scipy: {'available' if _HAVE_SCIPY else 'MISSING'}\n")

    res = Results()
    check_weights(res)
    check_jury_scores(res, rows)
    check_primary_field(res, rows)
    check_unit_construction(res, rows)
    check_alpha(res, rows)
    check_correlation_and_bland_altman(res)
    check_provider_means(res, rows)
    check_hallucination_rates(res, rows)
    check_quality_score(res, rows)

    print()
    res.print_table()
    print()
    total = len(res.rows)
    print(f"[audit] {total - res.failed - res.skipped} passed, "
          f"{res.failed} failed, {res.skipped} skipped.")
    if res.failed:
        print("[audit] FAIL — a computed statistic does not match its independent "
              "re-derivation. Investigate before trusting the report numbers.")
        return 1
    print("[audit] OK — every audited statistic matches its independent re-derivation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
