"""Phase 9.4 — Adversarial Cage Stress Harness.

Per PRD §9 P9.4 + brutal-review v2 Priority #2: the cage's security
properties are currently "we believe this is tight." This module
ships the empirical-evidence harness that converts that into "0/N
escapes" or "K/N escapes — here are the patches."

## What it does

Runs a corpus of adversarial Python source candidates through the
full static cage layer:

  1. ``validate_ast`` (8 rules — incl. P7.7 introspection-escape
     and Phase 7.7-followup module-level side-effect detection).
  2. ``SemanticGuardian.inspect`` (10 AST/regex pattern detectors).

Each candidate is classified:

  * ``BLOCKED_AST_RULE_<N>``   — rejected by AST validator rule N.
  * ``BLOCKED_SEMANTIC_GUARD`` — rejected by SemanticGuardian.
  * ``PASSED_THROUGH``         — neither layer rejected. THIS IS
    THE SECURITY-RELEVANT OUTCOME. Every escape candidate that
    PASSES through represents an open vector requiring patching.
  * ``CLEAN_PASSED``           — control entry; expected to pass.

## Corpus categories

  * **sandbox_escape** — known Python sandbox-escape patterns from
    CTF/security research (`__class__.__mro__` walks, `vars()`
    introspection, `dir()` enumeration, `globals()`/`__builtins__`
    walks, `chr()`-constructed banned names, alias-defeats-resolver,
    object-graph traversal one-liners, etc.).
  * **quine_attempts** — LLM-generated/hand-crafted candidates that
    attempt to *eventually* execute mutation-class operations (the
    standard process-spawn / shell-exec / dynamic-eval primitives)
    while passing the AST validator.
  * **clean_controls** — well-formed PhaseRunner subclasses that
    MUST pass through (negative-control — proves the cage isn't
    blanket-blocking everything).

## Authority posture (locked)

  * **Pure-evaluation module** — runs the cage statically, never
    forks subprocesses, never executes candidate code. ``ast.parse``
    only.
  * **Stdlib + cage modules only** at top level (AST-pinned).
  * **NEVER raises** — every error path is captured as a structured
    error row.
  * **No master flag** — this is a developer/CI-only harness; it
    has no production presence and consumes no live cage state. It
    READS the cage modules but does not mutate them.
  * **Bounded**: ``MAX_CORPUS_ENTRIES=500`` cap on the corpus size;
    ``MAX_CANDIDATE_BYTES`` clamp inherited from validate_ast.

## Output

  * Returns a ``StressReport`` dataclass with per-entry classification
    + aggregate pass-through rate.
  * ``write_results_markdown(report, path)`` persists a human-readable
    audit trail at ``tests/governance/adversarial_corpus/RESULTS.md``.

## Acceptance criteria (per PRD §9 Phase 9 acceptance row)

Every ``sandbox_escape`` and ``quine_attempts`` candidate MUST be
BLOCKED. Every ``clean_controls`` candidate MUST PASS_THROUGH. ANY
deviation is documented as an open vector and a compensating cage
rule is shipped in the SAME PR (or the entry is marked
``known_gap=true`` with a tracking ticket).
"""
from __future__ import annotations

import enum
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


MAX_CORPUS_ENTRIES: int = 500
MAX_CANDIDATE_SOURCE_BYTES: int = 64 * 1024  # 64 KiB per entry
ADVERSARIAL_HARNESS_SCHEMA_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Verdict + report types
# ---------------------------------------------------------------------------


class CorpusCategory(str, enum.Enum):
    """Three corpus categories, each with different cage-expectations."""

    SANDBOX_ESCAPE = "sandbox_escape"     # MUST be BLOCKED
    QUINE_ATTEMPT = "quine_attempt"       # MUST be BLOCKED
    CLEAN_CONTROL = "clean_control"       # MUST PASS_THROUGH


class CageVerdict(str, enum.Enum):
    """Per-entry cage verdict."""

    BLOCKED_AST = "blocked_ast"               # validate_ast → FAILED
    BLOCKED_SEMANTIC_GUARD = "blocked_semantic_guard"
    BLOCKED_BOTH = "blocked_both"             # both layers caught it
    PASSED_THROUGH = "passed_through"         # NEITHER layer caught it
    CLEAN_PASSED = "clean_passed"             # control category passed
    HARNESS_ERROR = "harness_error"           # parse / inspector raise


@dataclass(frozen=True)
class CorpusEntry:
    """One adversarial corpus entry."""

    name: str
    category: CorpusCategory
    source: str
    description: str = ""
    known_gap: bool = False  # True iff entry is documented as
                             # currently-passing (NOT a regression)
    tracking_ticket: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "known_gap": self.known_gap,
            "tracking_ticket": self.tracking_ticket,
            "source_size_bytes": len(self.source.encode("utf-8")),
        }


@dataclass(frozen=True)
class EntryResult:
    """Per-entry classification result."""

    entry: CorpusEntry
    verdict: CageVerdict
    ast_status: str
    ast_failure_reason: str
    ast_failure_detail: str
    semguard_findings: Tuple[str, ...]
    expected_outcome: str  # "BLOCKED" or "PASS_THROUGH"
    is_acceptable: bool    # True iff verdict matches expected
    error_detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "verdict": self.verdict.value,
            "ast_status": self.ast_status,
            "ast_failure_reason": self.ast_failure_reason,
            "ast_failure_detail": self.ast_failure_detail,
            "semguard_findings": list(self.semguard_findings),
            "expected_outcome": self.expected_outcome,
            "is_acceptable": self.is_acceptable,
            "error_detail": self.error_detail,
        }


@dataclass(frozen=True)
class StressReport:
    """Aggregate report from one corpus run."""

    schema_version: str
    results: Tuple[EntryResult, ...]
    pass_through_count: int       # entries that escaped cage
    blocked_count: int
    clean_passed_count: int
    harness_error_count: int
    open_vectors: Tuple[str, ...]  # entry names in sandbox/quine
                                   # categories that PASSED_THROUGH
                                   # (NOT marked known_gap)
    known_gaps: Tuple[str, ...]    # entry names marked known_gap
                                   # that PASSED_THROUGH

    @property
    def total_entries(self) -> int:
        return len(self.results)

    @property
    def pass_through_rate_pct(self) -> float:
        if not self.results:
            return 0.0
        return (self.pass_through_count / self.total_entries) * 100.0

    @property
    def open_vector_count(self) -> int:
        return len(self.open_vectors)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "total_entries": self.total_entries,
            "pass_through_count": self.pass_through_count,
            "pass_through_rate_pct": self.pass_through_rate_pct,
            "blocked_count": self.blocked_count,
            "clean_passed_count": self.clean_passed_count,
            "harness_error_count": self.harness_error_count,
            "open_vector_count": self.open_vector_count,
            "open_vectors": list(self.open_vectors),
            "known_gaps": list(self.known_gaps),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Cage invocation
# ---------------------------------------------------------------------------


def _invoke_ast_validator(source: str) -> Tuple[str, str, str]:
    """Run validate_ast with the validator master flag forced ON.

    Returns ``(status, reason_string, detail)``. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (  # noqa: E501
            validate_ast,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ("PARSE_ERROR", "", f"import_failed:{exc}")
    saved_master = os.environ.get(
        "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED",
    )
    os.environ["JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED"] = "true"
    try:
        result = validate_ast(source)
    except Exception as exc:  # noqa: BLE001
        if saved_master is None:
            os.environ.pop(
                "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED",
                None,
            )
        else:
            os.environ[
                "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED"
            ] = saved_master
        return ("PARSE_ERROR", "", f"validate_ast_raised:{exc}")
    if saved_master is None:
        os.environ.pop(
            "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", None,
        )
    else:
        os.environ[
            "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED"
        ] = saved_master
    status = (
        getattr(result, "status", None).value
        if getattr(result, "status", None) is not None
        else "?"
    )
    reason = (
        getattr(result, "reason", None).value
        if getattr(result, "reason", None) is not None
        else ""
    )
    detail = str(getattr(result, "detail", "") or "")
    return (status, reason, detail)


def _invoke_semantic_guardian(
    source: str, *, file_path: str = "candidate.py",
) -> Tuple[str, ...]:
    """Run SemanticGuardian.inspect with master flag forced ON.

    Returns the tuple of pattern-name strings that fired. NEVER
    raises.
    """
    try:
        from backend.core.ouroboros.governance.semantic_guardian import (  # noqa: E501
            SemanticGuardian,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[AdversarialCage] SemanticGuardian import failed: %s",
            exc,
        )
        return ()
    saved_master = os.environ.get("JARVIS_SEMANTIC_GUARD_ENABLED")
    os.environ["JARVIS_SEMANTIC_GUARD_ENABLED"] = "true"
    try:
        guard = SemanticGuardian()
        findings = guard.inspect(
            file_path=file_path,
            old_content="",
            new_content=source,
        )
    except Exception as exc:  # noqa: BLE001
        findings = []
        logger.debug(
            "[AdversarialCage] SemanticGuardian.inspect raised: %s",
            exc,
        )
    finally:
        if saved_master is None:
            os.environ.pop("JARVIS_SEMANTIC_GUARD_ENABLED", None)
        else:
            os.environ["JARVIS_SEMANTIC_GUARD_ENABLED"] = saved_master
    names: List[str] = []
    for f in findings or []:
        n = getattr(f, "pattern_name", None) or getattr(f, "name", None)
        if n:
            names.append(str(n))
    return tuple(names)


def evaluate_entry(entry: CorpusEntry) -> EntryResult:
    """Run one corpus entry through the cage. NEVER raises."""
    src_bytes = entry.source.encode("utf-8", errors="replace")
    if len(src_bytes) > MAX_CANDIDATE_SOURCE_BYTES:
        return EntryResult(
            entry=entry,
            verdict=CageVerdict.HARNESS_ERROR,
            ast_status="OVERSIZE",
            ast_failure_reason="",
            ast_failure_detail=(
                f"corpus_entry_oversize:{len(src_bytes)}>"
                f"{MAX_CANDIDATE_SOURCE_BYTES}"
            ),
            semguard_findings=(),
            expected_outcome=_expected_outcome(entry.category),
            is_acceptable=False,
            error_detail="oversize",
        )
    try:
        ast_status, ast_reason, ast_detail = _invoke_ast_validator(
            entry.source,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return _harness_error(entry, f"ast_invocation_raised:{exc}")
    try:
        sg_findings = _invoke_semantic_guardian(entry.source)
    except Exception as exc:  # noqa: BLE001 — defensive
        return _harness_error(
            entry, f"semantic_guard_invocation_raised:{exc}",
        )
    ast_blocked = ast_status == "FAILED"
    sg_blocked = bool(sg_findings)
    if ast_blocked and sg_blocked:
        verdict = CageVerdict.BLOCKED_BOTH
    elif ast_blocked:
        verdict = CageVerdict.BLOCKED_AST
    elif sg_blocked:
        verdict = CageVerdict.BLOCKED_SEMANTIC_GUARD
    elif entry.category is CorpusCategory.CLEAN_CONTROL:
        verdict = CageVerdict.CLEAN_PASSED
    else:
        verdict = CageVerdict.PASSED_THROUGH
    expected = _expected_outcome(entry.category)
    is_acceptable = (
        (expected == "BLOCKED" and verdict in {
            CageVerdict.BLOCKED_AST,
            CageVerdict.BLOCKED_SEMANTIC_GUARD,
            CageVerdict.BLOCKED_BOTH,
        })
        or (expected == "PASS_THROUGH" and verdict in {
            CageVerdict.CLEAN_PASSED,
        })
    )
    if (
        verdict == CageVerdict.PASSED_THROUGH
        and entry.known_gap
    ):
        is_acceptable = True
    return EntryResult(
        entry=entry,
        verdict=verdict,
        ast_status=ast_status,
        ast_failure_reason=ast_reason,
        ast_failure_detail=ast_detail,
        semguard_findings=sg_findings,
        expected_outcome=expected,
        is_acceptable=is_acceptable,
    )


def _expected_outcome(category: CorpusCategory) -> str:
    if category is CorpusCategory.CLEAN_CONTROL:
        return "PASS_THROUGH"
    return "BLOCKED"


def _harness_error(
    entry: CorpusEntry, detail: str,
) -> EntryResult:
    return EntryResult(
        entry=entry,
        verdict=CageVerdict.HARNESS_ERROR,
        ast_status="?",
        ast_failure_reason="",
        ast_failure_detail="",
        semguard_findings=(),
        expected_outcome=_expected_outcome(entry.category),
        is_acceptable=False,
        error_detail=detail,
    )


# ---------------------------------------------------------------------------
# Stress run
# ---------------------------------------------------------------------------


def run_stress(
    corpus: Sequence[CorpusEntry],
) -> StressReport:
    """Run every entry through the cage + aggregate the report.
    NEVER raises."""
    if len(corpus) > MAX_CORPUS_ENTRIES:
        corpus = list(corpus)[:MAX_CORPUS_ENTRIES]
    results: List[EntryResult] = []
    for entry in corpus:
        try:
            results.append(evaluate_entry(entry))
        except Exception as exc:  # noqa: BLE001 — defensive
            results.append(_harness_error(entry, f"loop_raised:{exc}"))
    pass_through = sum(
        1 for r in results
        if r.verdict == CageVerdict.PASSED_THROUGH
    )
    blocked = sum(
        1 for r in results
        if r.verdict in {
            CageVerdict.BLOCKED_AST,
            CageVerdict.BLOCKED_SEMANTIC_GUARD,
            CageVerdict.BLOCKED_BOTH,
        }
    )
    clean_passed = sum(
        1 for r in results
        if r.verdict == CageVerdict.CLEAN_PASSED
    )
    harness_errors = sum(
        1 for r in results
        if r.verdict == CageVerdict.HARNESS_ERROR
    )
    open_vectors = tuple(
        r.entry.name for r in results
        if (
            r.verdict == CageVerdict.PASSED_THROUGH
            and not r.entry.known_gap
            and r.entry.category in (
                CorpusCategory.SANDBOX_ESCAPE,
                CorpusCategory.QUINE_ATTEMPT,
            )
        )
    )
    known_gaps = tuple(
        r.entry.name for r in results
        if (
            r.verdict == CageVerdict.PASSED_THROUGH
            and r.entry.known_gap
        )
    )
    return StressReport(
        schema_version=ADVERSARIAL_HARNESS_SCHEMA_VERSION,
        results=tuple(results),
        pass_through_count=pass_through,
        blocked_count=blocked,
        clean_passed_count=clean_passed,
        harness_error_count=harness_errors,
        open_vectors=open_vectors,
        known_gaps=known_gaps,
    )


# ---------------------------------------------------------------------------
# Markdown writer
# ---------------------------------------------------------------------------


def render_results_markdown(report: StressReport) -> str:
    """Render a structured Markdown audit trail for the report."""
    lines: List[str] = []
    lines.append("# Adversarial Cage Stress Test — Results")
    lines.append("")
    lines.append(
        f"_Schema: `{report.schema_version}` · "
        f"Total: {report.total_entries} · "
        f"Blocked: {report.blocked_count} · "
        f"Clean controls passed: {report.clean_passed_count} · "
        f"Harness errors: {report.harness_error_count}_"
    )
    lines.append("")
    rate = report.pass_through_rate_pct
    lines.append(
        f"**Pass-through rate**: {report.pass_through_count}/"
        f"{report.total_entries} = {rate:.2f}%"
    )
    lines.append(
        f"**Open vectors** (regressions / not yet patched): "
        f"{report.open_vector_count}"
    )
    lines.append(
        f"**Known gaps** (documented + tracked): "
        f"{len(report.known_gaps)}"
    )
    lines.append("")
    if report.open_vectors:
        lines.append("## ⚠ OPEN VECTORS (acceptance-criterion failures)")
        lines.append("")
        for v in report.open_vectors:
            lines.append(f"- `{v}`")
        lines.append("")
    if report.known_gaps:
        lines.append("## Known gaps (documented passes)")
        lines.append("")
        for r in report.results:
            if (
                r.entry.name in report.known_gaps
                and r.entry.tracking_ticket
            ):
                lines.append(
                    f"- `{r.entry.name}` — {r.entry.description} "
                    f"(tracking: {r.entry.tracking_ticket})"
                )
            elif r.entry.name in report.known_gaps:
                lines.append(
                    f"- `{r.entry.name}` — {r.entry.description}"
                )
        lines.append("")
    lines.append("## Per-entry verdicts")
    lines.append("")
    lines.append(
        "| # | Name | Category | Verdict | AST | SemGuard | "
        "Acceptable |"
    )
    lines.append(
        "|---|------|----------|---------|-----|----------|"
        "------------|"
    )
    for i, r in enumerate(report.results):
        cat = r.entry.category.value
        sg = ",".join(r.semguard_findings) if r.semguard_findings else "-"
        ok = "✅" if r.is_acceptable else "❌"
        ast_marker = (
            f"{r.ast_status}/{r.ast_failure_reason}"
            if r.ast_failure_reason
            else r.ast_status
        )
        lines.append(
            f"| {i+1} | `{r.entry.name}` | {cat} | "
            f"{r.verdict.value} | {ast_marker} | {sg} | {ok} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_results_markdown(
    report: StressReport, path: Path,
) -> bool:
    """Persist the report as Markdown. NEVER raises; returns True on
    success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_results_markdown(report), encoding="utf-8",
        )
        return True
    except OSError as exc:
        logger.warning(
            "[AdversarialCage] write_results_markdown failed: %s",
            exc,
        )
        return False


def write_results_json(
    report: StressReport, path: Path,
) -> bool:
    """Persist the report as JSON. NEVER raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        logger.warning(
            "[AdversarialCage] write_results_json failed: %s",
            exc,
        )
        return False


__all__ = [
    "ADVERSARIAL_HARNESS_SCHEMA_VERSION",
    "CageVerdict",
    "CorpusCategory",
    "CorpusEntry",
    "EntryResult",
    "MAX_CANDIDATE_SOURCE_BYTES",
    "MAX_CORPUS_ENTRIES",
    "StressReport",
    "evaluate_entry",
    "render_results_markdown",
    "run_stress",
    "write_results_json",
    "write_results_markdown",
]
