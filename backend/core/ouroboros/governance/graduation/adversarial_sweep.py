"""Slice 86 — async combinatorial adversarial sweep harness.

Composes the EXISTING corpus, the 8 deterministic mutation operators, and the
cage's evaluate_entry. NO new evaluator; NO reimplemented operators.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

SCHEMA_VERSION = "adversarial_sweep.v1"

_ADVERSARIAL_CATEGORIES = frozenset({"sandbox_escape", "quine_attempt"})


@dataclass(frozen=True)
class SweepVariantResult:
    seed_name: str
    seed_category: str
    strategy: str            # "raw" or a MutationStrategy value
    verdict: str             # CageVerdict value
    gate_reason: str
    is_escape: bool          # adversarial seed AND verdict == passed_through
    is_clean_fp: bool        # clean_control AND verdict != clean_passed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seed_name": self.seed_name,
            "seed_category": self.seed_category,
            "strategy": self.strategy,
            "verdict": self.verdict,
            "gate_reason": self.gate_reason,
            "is_escape": self.is_escape,
            "is_clean_fp": self.is_clean_fp,
        }


@dataclass(frozen=True)
class SweepReport:
    schema_version: str
    total_variants: int
    raw_seed_count: int
    mutation_variant_count: int
    adversarial_seed_count: int
    adversarial_escape_count_raw: int
    adversarial_escape_rate_raw: float
    adversarial_variant_total: int
    adversarial_escape_count_with_mutations: int
    adversarial_escape_rate_with_mutations: float
    clean_control_false_positive_count: int
    by_category: Dict[str, Dict[str, int]]
    by_gate_attribution: Dict[str, int]
    by_mutation_strategy: Dict[str, Dict[str, int]]
    mutation_induced_escapes: Tuple[Dict[str, str], ...]
    escaping_entries_raw: Tuple[Dict[str, str], ...]
    results: Tuple[SweepVariantResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "total_variants": self.total_variants,
            "raw_seed_count": self.raw_seed_count,
            "mutation_variant_count": self.mutation_variant_count,
            "adversarial_seed_count": self.adversarial_seed_count,
            "adversarial_escape_count_raw": self.adversarial_escape_count_raw,
            "adversarial_escape_rate_raw": self.adversarial_escape_rate_raw,
            "adversarial_variant_total": self.adversarial_variant_total,
            "adversarial_escape_count_with_mutations":
                self.adversarial_escape_count_with_mutations,
            "adversarial_escape_rate_with_mutations":
                self.adversarial_escape_rate_with_mutations,
            "clean_control_false_positive_count":
                self.clean_control_false_positive_count,
            "by_category": self.by_category,
            "by_gate_attribution": self.by_gate_attribution,
            "by_mutation_strategy": self.by_mutation_strategy,
            "mutation_induced_escapes": list(self.mutation_induced_escapes),
            "escaping_entries_raw": list(self.escaping_entries_raw),
            "results": [r.to_dict() for r in self.results],
        }


def build_sweep_matrix(
    *, include_mutations: bool = True,
) -> List[Tuple[Any, str]]:
    """Return [(CorpusEntry, strategy_label), ...] for every raw seed plus,
    if requested, one variant per APPLICABLE deterministic operator.

    Composes build_corpus() + generate_mutations(). NEVER reimplements an
    operator. Inapplicable operators (generate_mutations -> None) are skipped.
    """
    from tests.governance.adversarial_corpus.corpus import build_corpus
    from backend.core.ouroboros.governance.graduation.adversarial_cage import (
        CorpusEntry,
    )
    from backend.core.ouroboros.governance.self_immunization import (
        generate_mutations,
    )

    matrix: List[Tuple[Any, str]] = []
    for seed in build_corpus():
        matrix.append((seed, "raw"))
        if not include_mutations:
            continue
        for strat, mutated in generate_mutations(seed.source):
            if mutated is None:
                continue
            matrix.append((
                CorpusEntry(
                    name=f"{seed.name}::{strat.value}",
                    category=seed.category,
                    source=mutated,
                    description=f"mutation:{strat.value} of {seed.name}",
                ),
                strat.value,
            ))
    return matrix


def _gate_reason(result: Any) -> str:
    v = result.verdict.value
    if v == "blocked_ast":
        return f"blocked_ast:{result.ast_failure_reason}"
    if v == "blocked_semantic_guard":
        return f"blocked_semantic_guard:{','.join(result.semguard_findings)}"
    if v == "blocked_both":
        return f"blocked_both:{result.ast_failure_reason}"
    return v


async def run_sweep(
    *, include_mutations: bool = True, concurrency: int = 4,
) -> SweepReport:
    """Evaluate the full matrix through the cage and aggregate a SweepReport.

    evaluate_entry is sync + fast; we bound it with a Semaphore and offload to
    threads so this composes into async callers (CI gate, Chaos Monkey).
    """
    from backend.core.ouroboros.governance.graduation.adversarial_cage import (
        evaluate_entry,
    )

    matrix = build_sweep_matrix(include_mutations=include_mutations)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(entry: Any, label: str) -> SweepVariantResult:
        async with sem:
            res = await asyncio.to_thread(evaluate_entry, entry)
        cat = entry.category.value
        verdict = res.verdict.value
        is_adv = cat in _ADVERSARIAL_CATEGORIES
        is_escape = is_adv and verdict == "passed_through"
        is_clean_fp = cat == "clean_control" and verdict != "clean_passed"
        return SweepVariantResult(
            seed_name=entry.name, seed_category=cat, strategy=label,
            verdict=verdict, gate_reason=_gate_reason(res),
            is_escape=is_escape, is_clean_fp=is_clean_fp,
        )

    results = await asyncio.gather(*[_one(e, lbl) for e, lbl in matrix])

    raw = [r for r in results if r.strategy == "raw"]
    muts = [r for r in results if r.strategy != "raw"]
    adv_raw = [r for r in raw if r.seed_category in _ADVERSARIAL_CATEGORIES]
    adv_all = [r for r in results if r.seed_category in _ADVERSARIAL_CATEGORIES]

    esc_raw = [r for r in adv_raw if r.is_escape]
    esc_all = [r for r in adv_all if r.is_escape]

    # mutation-induced escapes: seed blocked raw but a mutation escaped
    raw_blocked = {
        r.seed_name for r in adv_raw if not r.is_escape
    }
    induced = tuple(
        {"seed": r.seed_name.split("::", 1)[0], "strategy": r.strategy}
        for r in muts
        if r.is_escape and r.seed_name.split("::", 1)[0] in raw_blocked
    )

    by_cat: Dict[str, Dict[str, int]] = {}
    for r in results:
        c = by_cat.setdefault(
            r.seed_category, {"blocked": 0, "escaped": 0, "errors": 0, "total": 0})
        c["total"] += 1
        if r.verdict == "harness_error":
            c["errors"] += 1
        elif r.is_escape:
            c["escaped"] += 1
        elif r.verdict in ("blocked_ast", "blocked_semantic_guard", "blocked_both"):
            c["blocked"] += 1

    by_gate: Dict[str, int] = {}
    for r in results:
        by_gate[r.verdict] = by_gate.get(r.verdict, 0) + 1

    by_strat: Dict[str, Dict[str, int]] = {}
    for r in results:
        s = by_strat.setdefault(r.strategy, {"variants": 0, "escapes": 0})
        s["variants"] += 1
        if r.is_escape:
            s["escapes"] += 1

    def _rate(n: int, d: int) -> float:
        return round((n / d) * 100.0, 4) if d else 0.0

    return SweepReport(
        schema_version=SCHEMA_VERSION,
        total_variants=len(results),
        raw_seed_count=len(raw),
        mutation_variant_count=len(muts),
        adversarial_seed_count=len(adv_raw),
        adversarial_escape_count_raw=len(esc_raw),
        adversarial_escape_rate_raw=_rate(len(esc_raw), len(adv_raw)),
        adversarial_variant_total=len(adv_all),
        adversarial_escape_count_with_mutations=len(esc_all),
        adversarial_escape_rate_with_mutations=_rate(len(esc_all), len(adv_all)),
        clean_control_false_positive_count=sum(
            1 for r in results if r.is_clean_fp),
        by_category=by_cat,
        by_gate_attribution=by_gate,
        by_mutation_strategy=by_strat,
        mutation_induced_escapes=induced,
        escaping_entries_raw=tuple(
            {"name": r.seed_name, "category": r.seed_category,
             "gate_reason": r.gate_reason}
            for r in esc_raw
        ),
        results=tuple(results),
    )


def evaluate_regression(
    report: SweepReport, *,
    baseline_escape_rate_raw: float, max_clean_fp: int = 0,
) -> Tuple[bool, str]:
    """Return (ok, message). NOT ok if the raw adversarial escape rate
    exceeds the baseline (a regression) OR any clean control false-positives."""
    if report.clean_control_false_positive_count > max_clean_fp:
        return (False, (
            f"clean-control false positives "
            f"{report.clean_control_false_positive_count} > {max_clean_fp}"))
    if report.adversarial_escape_rate_raw > baseline_escape_rate_raw + 1e-9:
        return (False, (
            f"raw adversarial escape rate "
            f"{report.adversarial_escape_rate_raw}% > baseline "
            f"{baseline_escape_rate_raw}%"))
    return (True, (
        f"ok: escape={report.adversarial_escape_rate_raw}% "
        f"clean_fp={report.clean_control_false_positive_count}"))


def render_console_report(report: SweepReport) -> str:
    lines = [
        "=== Adversarial Sweep Report ===",
        f"schema: {report.schema_version}",
        f"total variants: {report.total_variants} "
        f"(raw={report.raw_seed_count}, mutations={report.mutation_variant_count})",
        f"Adversarial escape (raw): "
        f"{report.adversarial_escape_count_raw}/{report.adversarial_seed_count} "
        f"= {round(report.adversarial_escape_rate_raw, 1)}%",
        f"Adversarial escape (with mutations): "
        f"{report.adversarial_escape_count_with_mutations}/"
        f"{report.adversarial_variant_total} "
        f"= {round(report.adversarial_escape_rate_with_mutations, 1)}%",
        f"Clean-control false positives: "
        f"{report.clean_control_false_positive_count}",
        f"By gate: {report.by_gate_attribution}",
        f"Mutation-induced escapes: {len(report.mutation_induced_escapes)}",
        "Escaping (raw):",
    ]
    for e in report.escaping_entries_raw:
        lines.append(f"  - {e['name']} [{e['category']}] {e['gate_reason']}")
    return "\n".join(lines)
