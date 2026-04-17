"""MutationTester — the meta-test that distinguishes governance-approved-
and-correct from governance-approved-and-performative.

**Problem**: "tests pass" ≠ "tests test the right thing." A model can
produce `test_feature_enabled_by_default` that only checks
``obj.enabled is not None`` — it passes the test suite and the governance
pipeline (VALIDATE, Iron Gate, SemanticGuardian), but it catches no real
bugs. Session W's 20/20 pytest green is a great proof the model writes
syntactically valid tests; it is not proof the tests would catch a
regression.

**Approach**: mutation testing. Mutate the system-under-test (SUT) by
applying deterministic AST transformations (``==`` → ``!=``, ``True`` ↔
``False``, arithmetic swap, ``return X`` → ``return None``), then re-run
the test suite. If all tests still pass against the mutated code, the
mutant *survived* — at least one behavior of the SUT is not exercised
by any assertion in the test suite. Survivors are the list of specific
behaviors the test suite is blind to.

**Mutation score** = ``caught_mutants / total_mutants``. In the wild,
scores of 70–90% are excellent. A score of 100% is rare and often
suspicious (equivalent-mutant blindness, or the mutation operator set
is too shallow). Scores under 40% usually indicate performative tests —
they assert shape, not behavior.

**Scope caveats (load-bearing)**:

  * Mutation testing is theoretically bounded. *Equivalent mutants* —
    mutants that change the AST but produce identical observable
    behavior (e.g. `for i in range(n, n)` vs `for i in range(n, n+0)`,
    dead-branch changes, order-invariant set iteration) — cannot be
    caught by any test. They inflate the survivor count without
    indicating a real gap.
  * The mutation operator set in V1 is deliberately narrow (4 ops,
    fully deterministic). It will NOT catch bugs that require
    higher-order mutations (swapping two variables, removing a method
    call, changing the type of a return value).
  * Survived mutants are **hints**, not proofs. A human (or a
    stronger analyzer) must confirm the survivor points to a real
    test gap vs. an equivalent mutant.
  * Cost: each mutant requires a full pytest subprocess. For N=25
    mutants × 30s/mutant cap = ~12.5 min worst case. Run this offline
    or on a separate track — NOT in the VERIFY phase of every
    pipeline op.

**Authority invariant (Manifesto §1 Boundary Principle)**: this module
returns a ``MutationResult`` dataclass with a numeric score and a
survivor list. It NEVER mutates any governance surface, NEVER overrides
the risk tier, NEVER blocks a pipeline phase. The operator (or an
external orchestration layer) decides what to do with a low score. The
governance pipeline — Iron Gate, SemanticGuardian, risk tier floor,
approval gates — remains sole authority over whether an operation
lands on disk.

Env gates (all fail-closed / safe defaults):

    JARVIS_MUTATION_TEST_ENABLED       master, default 0
    JARVIS_MUTATION_TEST_MAX_MUTANTS   deterministic sample cap, default 25
    JARVIS_MUTATION_TEST_TIMEOUT_S     per-mutant pytest timeout, default 30
    JARVIS_MUTATION_TEST_GLOBAL_TIMEOUT_S  hard cap on full run, default 900
    JARVIS_MUTATION_TEST_SEED          deterministic sampler seed, default 0

V1 mutation operator catalog:

    bool_flip       True ↔ False (Constant nodes with bool value)
    compare_flip    == → !=, != → ==, < → >=, <= → >, > → <=, >= → <
    arith_swap      + → -, - → +, * → //, / → *
    return_none     return <expr> → return None (skips already-None returns)
"""
from __future__ import annotations

import ast
import copy
import json
import logging
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple,
)


logger = logging.getLogger("Ouroboros.MutationTester")

_ENV_ENABLED = "JARVIS_MUTATION_TEST_ENABLED"
_ENV_MAX_MUTANTS = "JARVIS_MUTATION_TEST_MAX_MUTANTS"
_ENV_TIMEOUT = "JARVIS_MUTATION_TEST_TIMEOUT_S"
_ENV_GLOBAL_TIMEOUT = "JARVIS_MUTATION_TEST_GLOBAL_TIMEOUT_S"
_ENV_SEED = "JARVIS_MUTATION_TEST_SEED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_MUTATION_OPS: Tuple[str, ...] = (
    "bool_flip",
    "compare_flip",
    "arith_swap",
    "return_none",
)


# ---------------------------------------------------------------------------
# Env helpers — fail-closed, clamped to sane ranges
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in _TRUTHY


def _int_env(key: str, default: int, *, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(key, str(default)))))
    except (TypeError, ValueError):
        return default


def max_mutants() -> int:
    return _int_env(_ENV_MAX_MUTANTS, 25, lo=1, hi=500)


def mutant_timeout_s() -> float:
    return float(_int_env(_ENV_TIMEOUT, 30, lo=5, hi=600))


def global_timeout_s() -> float:
    return float(_int_env(_ENV_GLOBAL_TIMEOUT, 900, lo=30, hi=7200))


def sampler_seed() -> int:
    return _int_env(_ENV_SEED, 0, lo=0, hi=2**31 - 1)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mutant:
    """One candidate mutation: op + location + rendered patched source."""
    op: str                        # one of _MUTATION_OPS
    source_file: str               # relative or absolute path to SUT
    line: int                      # 1-indexed line of the mutated node
    col: int                       # 0-indexed column
    original: str                  # human-readable original token ("==")
    mutated: str                   # human-readable mutated token ("!=")
    patched_src: str               # full file source with mutation applied

    @property
    def key(self) -> str:
        return f"{self.source_file}:{self.line}:{self.col}:{self.op}:{self.original}->{self.mutated}"


@dataclass(frozen=True)
class MutantOutcome:
    """Result of running one mutant: caught or survived, with reason."""
    mutant: Mutant
    caught: bool                   # True = at least one test failed (good)
    reason: str                    # "test_failure" | "timeout" | "survived" | "run_error"
    duration_s: float
    stderr_excerpt: str = ""       # first ~200 chars of subprocess stderr


@dataclass(frozen=True)
class MutationResult:
    """Aggregate result of a mutation testing run."""
    source_file: str
    total_mutants: int             # number of mutants attempted
    caught: int                    # number caught by at least one test
    survived: int                  # number that passed all tests
    score: float                   # caught / total (0.0-1.0, 0 if total=0)
    grade: str                     # "A" / "B" / "C" / "D" / "F" / "N/A"
    survivors: Tuple[MutantOutcome, ...]  # survivor list for operator review
    coverage_by_op: Dict[str, int] = field(default_factory=dict)
    skipped_by_op: Dict[str, int] = field(default_factory=dict)
    duration_s: float = 0.0
    errored_mutants: int = 0
    equivalent_mutant_caveat: str = (
        "Survived mutants may be equivalent (behavior-preserving) — "
        "manual review required before concluding the test suite is weak."
    )

    def to_json(self) -> Dict[str, Any]:
        return {
            "source_file": self.source_file,
            "total_mutants": self.total_mutants,
            "caught": self.caught,
            "survived": self.survived,
            "score": round(self.score, 4),
            "grade": self.grade,
            "duration_s": round(self.duration_s, 2),
            "errored_mutants": self.errored_mutants,
            "coverage_by_op": dict(self.coverage_by_op),
            "skipped_by_op": dict(self.skipped_by_op),
            "survivors": [
                {
                    "op": s.mutant.op,
                    "line": s.mutant.line,
                    "col": s.mutant.col,
                    "original": s.mutant.original,
                    "mutated": s.mutant.mutated,
                    "reason": s.reason,
                }
                for s in self.survivors
            ],
            "equivalent_mutant_caveat": self.equivalent_mutant_caveat,
        }


# ---------------------------------------------------------------------------
# AST mutation engine
# ---------------------------------------------------------------------------


_COMPARE_FLIPS: Dict[type, Tuple[type, str, str]] = {
    ast.Eq: (ast.NotEq, "==", "!="),
    ast.NotEq: (ast.Eq, "!=", "=="),
    ast.Lt: (ast.GtE, "<", ">="),
    ast.LtE: (ast.Gt, "<=", ">"),
    ast.Gt: (ast.LtE, ">", "<="),
    ast.GtE: (ast.Lt, ">=", "<"),
}

_ARITH_FLIPS: Dict[type, Tuple[type, str, str]] = {
    ast.Add: (ast.Sub, "+", "-"),
    ast.Sub: (ast.Add, "-", "+"),
    ast.Mult: (ast.FloorDiv, "*", "//"),
    ast.Div: (ast.Mult, "/", "*"),
}


def _is_bool_constant(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, bool)
    )


def _is_already_none_return(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Return)
        and (
            node.value is None
            or (
                isinstance(node.value, ast.Constant)
                and node.value.value is None
            )
        )
    )


def _walk_with_parents(tree: ast.AST) -> Iterator[ast.AST]:
    """Plain walk is fine — we don't need parents for V1 mutations."""
    yield from ast.walk(tree)


def _enumerate_mutation_sites(
    tree: ast.AST, source_file: str, src: str,
) -> List[Tuple[str, int, int, int, str, str]]:
    """Produce a deterministic list of mutation sites from one parsed tree.

    Returns (op, node_id, line, col, original, mutated) tuples where
    node_id is a 0-indexed pre-order position used to locate the node
    in a deep-copied tree at mutation time.
    """
    sites: List[Tuple[str, int, int, int, str, str]] = []
    for nid, node in enumerate(ast.walk(tree)):
        # (1) bool_flip
        if _is_bool_constant(node):
            original = "True" if node.value else "False"
            mutated = "False" if node.value else "True"
            sites.append((
                "bool_flip", nid,
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
                original, mutated,
            ))
            continue
        # (2) compare_flip — only flip the first op in a chained compare
        if isinstance(node, ast.Compare) and node.ops:
            first = node.ops[0]
            flip = _COMPARE_FLIPS.get(type(first))
            if flip is not None:
                _, orig_tok, new_tok = flip
                sites.append((
                    "compare_flip", nid,
                    getattr(node, "lineno", 0),
                    getattr(node, "col_offset", 0),
                    orig_tok, new_tok,
                ))
                continue
        # (3) arith_swap — only on BinOp with a swappable op
        if isinstance(node, ast.BinOp):
            flip = _ARITH_FLIPS.get(type(node.op))
            if flip is not None:
                _, orig_tok, new_tok = flip
                sites.append((
                    "arith_swap", nid,
                    getattr(node, "lineno", 0),
                    getattr(node, "col_offset", 0),
                    orig_tok, new_tok,
                ))
                continue
        # (4) return_none — only on returns that currently return non-None
        if isinstance(node, ast.Return) and not _is_already_none_return(node):
            try:
                orig_src = ast.unparse(node.value) if node.value else ""
            except Exception:  # noqa: BLE001
                orig_src = "<expr>"
            sites.append((
                "return_none", nid,
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
                f"return {orig_src[:40]}", "return None",
            ))
    return sites


def _apply_mutation(tree: ast.AST, op: str, node_id: int) -> None:
    """Mutate the tree in place at the Nth node of pre-order walk."""
    for nid, node in enumerate(ast.walk(tree)):
        if nid != node_id:
            continue
        if op == "bool_flip" and _is_bool_constant(node):
            node.value = not node.value
            return
        if op == "compare_flip" and isinstance(node, ast.Compare) and node.ops:
            first = node.ops[0]
            flip = _COMPARE_FLIPS.get(type(first))
            if flip is not None:
                new_cls = flip[0]
                node.ops = [new_cls()] + list(node.ops[1:])
                return
        if op == "arith_swap" and isinstance(node, ast.BinOp):
            flip = _ARITH_FLIPS.get(type(node.op))
            if flip is not None:
                new_cls = flip[0]
                node.op = new_cls()
                return
        if op == "return_none" and isinstance(node, ast.Return):
            node.value = ast.Constant(value=None)
            return
    raise LookupError(
        f"mutation site not found: op={op} node_id={node_id}"
    )


def _render_mutant(
    original_src: str, op: str, node_id: int,
) -> Optional[str]:
    """Parse, clone, mutate, unparse. Returns None if mutation fails."""
    try:
        tree = ast.parse(original_src)
        mutated = copy.deepcopy(tree)
        _apply_mutation(mutated, op, node_id)
        rendered = ast.unparse(mutated)
        if rendered == original_src:
            return None
        return rendered
    except (SyntaxError, LookupError, TypeError, ValueError):
        logger.debug(
            "[MutationTester] render failed op=%s node_id=%d",
            op, node_id, exc_info=True,
        )
        return None


def enumerate_mutants(source_file: Path) -> List[Mutant]:
    """Produce every candidate mutant for a source file, deterministically.

    Order: pre-order AST walk. Callers may sample / trim this list using
    a deterministic seed so "same file + same seed → same mutants."
    """
    try:
        src = source_file.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.warning(
            "[MutationTester] cannot read %s", source_file, exc_info=True,
        )
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        logger.warning("[MutationTester] syntax error in %s", source_file)
        return []
    sites = _enumerate_mutation_sites(tree, str(source_file), src)
    mutants: List[Mutant] = []
    for op, node_id, line, col, orig, mut in sites:
        patched = _render_mutant(src, op, node_id)
        if patched is None:
            continue
        mutants.append(Mutant(
            op=op,
            source_file=str(source_file),
            line=line,
            col=col,
            original=orig,
            mutated=mut,
            patched_src=patched,
        ))
    return mutants


def sample_mutants(
    mutants: Sequence[Mutant], *, limit: int, seed: int,
) -> List[Mutant]:
    """Deterministic sampler. Same input + same seed → same output.

    Strategy: if len(mutants) <= limit, keep all. Else:
      1. Sort by (source_file, line, col, op) for stability.
      2. random.Random(seed).sample → pick ``limit`` items.
      3. Re-sort by (line, col, op) so survivors report in file order.
    """
    if len(mutants) <= limit:
        return sorted(mutants, key=lambda m: (m.source_file, m.line, m.col, m.op))
    ordered = sorted(
        mutants,
        key=lambda m: (m.source_file, m.line, m.col, m.op, m.original, m.mutated),
    )
    sampled = random.Random(seed).sample(ordered, limit)
    sampled.sort(key=lambda m: (m.source_file, m.line, m.col, m.op))
    return sampled


# ---------------------------------------------------------------------------
# Runner — writes mutated file, executes pytest, restores original
# ---------------------------------------------------------------------------


def _run_pytest(
    test_files: Sequence[Path],
    *, timeout_s: float, cwd: Optional[Path],
) -> Tuple[int, str]:
    cmd = [
        sys.executable, "-m", "pytest",
        "-x", "--tb=no", "-q",
        *[str(t) for t in test_files],
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout_s,
            text=True,
        )
        stderr = (proc.stderr or "")[:200]
        return proc.returncode, stderr
    except subprocess.TimeoutExpired:
        return 124, "<timeout>"
    except Exception as e:  # noqa: BLE001
        return 255, f"<run_error:{type(e).__name__}:{e}>"[:200]


def run_mutant(
    mutant: Mutant,
    *,
    test_files: Sequence[Path],
    timeout_s: Optional[float] = None,
    cwd: Optional[Path] = None,
) -> MutantOutcome:
    """Execute one mutant: write, run pytest, restore."""
    if timeout_s is None:
        timeout_s = mutant_timeout_s()
    source_path = Path(mutant.source_file)
    started = time.time()
    try:
        original = source_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return MutantOutcome(
            mutant=mutant, caught=False, reason="run_error",
            duration_s=0.0, stderr_excerpt=f"<read_original:{e}>"[:200],
        )
    try:
        source_path.write_text(mutant.patched_src, encoding="utf-8")
        rc, stderr = _run_pytest(test_files, timeout_s=timeout_s, cwd=cwd)
    finally:
        try:
            source_path.write_text(original, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.exception(
                "[MutationTester] FAILED TO RESTORE %s — re-writing now",
                source_path,
            )
            # Last-resort retry — losing the restore would corrupt SUT.
            source_path.write_text(original, encoding="utf-8")
    dur = time.time() - started
    if rc == 124:
        return MutantOutcome(
            mutant=mutant, caught=True, reason="timeout",
            duration_s=dur, stderr_excerpt=stderr,
        )
    if rc == 255:
        return MutantOutcome(
            mutant=mutant, caught=False, reason="run_error",
            duration_s=dur, stderr_excerpt=stderr,
        )
    if rc != 0:
        return MutantOutcome(
            mutant=mutant, caught=True, reason="test_failure",
            duration_s=dur, stderr_excerpt=stderr,
        )
    return MutantOutcome(
        mutant=mutant, caught=False, reason="survived",
        duration_s=dur, stderr_excerpt=stderr,
    )


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def _grade_from_score(score: float, total: int) -> str:
    if total == 0:
        return "N/A"
    if score >= 0.90:
        return "A"
    if score >= 0.75:
        return "B"
    if score >= 0.60:
        return "C"
    if score >= 0.40:
        return "D"
    return "F"


def run_mutation_test(
    source_file: Path,
    *,
    test_files: Sequence[Path],
    max_mutants_override: Optional[int] = None,
    timeout_s_override: Optional[float] = None,
    global_timeout_s_override: Optional[float] = None,
    seed_override: Optional[int] = None,
    cwd: Optional[Path] = None,
    progress_cb: Optional[Callable[[int, int, MutantOutcome], None]] = None,
) -> MutationResult:
    """Run mutation testing against ``source_file`` using ``test_files``.

    Override args let tests drive deterministic execution without
    mutating process env. ``progress_cb(idx, total, outcome)`` is called
    after each mutant for UI streaming — safe to omit.
    """
    started = time.time()
    cap = max_mutants_override if max_mutants_override is not None else max_mutants()
    per_timeout = (
        timeout_s_override if timeout_s_override is not None else mutant_timeout_s()
    )
    global_timeout = (
        global_timeout_s_override
        if global_timeout_s_override is not None
        else global_timeout_s()
    )
    seed = seed_override if seed_override is not None else sampler_seed()

    all_mutants = enumerate_mutants(source_file)
    coverage_by_op: Dict[str, int] = {op: 0 for op in _MUTATION_OPS}
    for m in all_mutants:
        coverage_by_op[m.op] = coverage_by_op.get(m.op, 0) + 1

    sampled = sample_mutants(all_mutants, limit=cap, seed=seed)
    skipped_by_op: Dict[str, int] = {op: 0 for op in _MUTATION_OPS}
    for op in _MUTATION_OPS:
        skipped_by_op[op] = coverage_by_op.get(op, 0) - sum(
            1 for m in sampled if m.op == op
        )

    logger.info(
        "[MutationTester] enumerate file=%s total_sites=%d sampled=%d "
        "per_timeout=%.0fs global_timeout=%.0fs seed=%d",
        source_file, len(all_mutants), len(sampled),
        per_timeout, global_timeout, seed,
    )

    outcomes: List[MutantOutcome] = []
    errored = 0
    for idx, m in enumerate(sampled):
        elapsed = time.time() - started
        if elapsed >= global_timeout:
            logger.warning(
                "[MutationTester] global timeout reached at mutant %d/%d",
                idx, len(sampled),
            )
            break
        out = run_mutant(
            m, test_files=test_files, timeout_s=per_timeout, cwd=cwd,
        )
        outcomes.append(out)
        if out.reason == "run_error":
            errored += 1
        if progress_cb is not None:
            try:
                progress_cb(idx + 1, len(sampled), out)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[MutationTester] progress_cb raised — ignoring",
                    exc_info=True,
                )

    caught = sum(1 for o in outcomes if o.caught)
    total = len(outcomes)
    survived_outcomes = tuple(o for o in outcomes if not o.caught)
    score = (caught / total) if total > 0 else 0.0
    grade = _grade_from_score(score, total)

    result = MutationResult(
        source_file=str(source_file),
        total_mutants=total,
        caught=caught,
        survived=total - caught,
        score=score,
        grade=grade,
        survivors=survived_outcomes,
        coverage_by_op=coverage_by_op,
        skipped_by_op=skipped_by_op,
        duration_s=time.time() - started,
        errored_mutants=errored,
    )
    logger.info(
        "[MutationTester] done file=%s score=%.2f grade=%s caught=%d/%d "
        "duration=%.1fs errored=%d",
        source_file, score, grade, caught, total, result.duration_s, errored,
    )
    return result


# ---------------------------------------------------------------------------
# Report renderers
# ---------------------------------------------------------------------------


def render_console_report(result: MutationResult) -> str:
    """Plain-text report. Rich variant lives alongside but falls back to
    this when Rich isn't installed or the output stream isn't a TTY."""
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append(f"Mutation Test Report — {result.source_file}")
    lines.append("=" * 78)
    lines.append(
        f"Score: {result.score:.1%}  Grade: {result.grade}  "
        f"Caught: {result.caught}/{result.total_mutants}  "
        f"Duration: {result.duration_s:.1f}s"
    )
    if result.errored_mutants:
        lines.append(
            f"WARNING: {result.errored_mutants} mutant(s) errored "
            "(subprocess failure) — not counted toward score."
        )
    lines.append("")
    lines.append("Coverage (sites found / sampled / skipped):")
    for op in _MUTATION_OPS:
        found = result.coverage_by_op.get(op, 0)
        skipped = result.skipped_by_op.get(op, 0)
        sampled = found - skipped
        lines.append(
            f"  {op:<16} sites={found:<4d} sampled={sampled:<4d} skipped={skipped}"
        )
    lines.append("")
    if result.survivors:
        lines.append(f"Survived mutants ({len(result.survivors)}):")
        lines.append(
            f"  {'line':<6} {'op':<16} {'original':<24} {'mutated':<24} reason"
        )
        lines.append("  " + "-" * 74)
        for s in result.survivors[:50]:
            lines.append(
                f"  {s.mutant.line:<6} {s.mutant.op:<16} "
                f"{s.mutant.original[:22]:<24} {s.mutant.mutated[:22]:<24} "
                f"{s.reason}"
            )
        if len(result.survivors) > 50:
            lines.append(
                f"  … {len(result.survivors) - 50} more elided — "
                "see JSON export for full list"
            )
    else:
        lines.append("Survivors: none — every mutant was caught.")
    lines.append("")
    lines.append("Caveat: " + result.equivalent_mutant_caveat)
    lines.append("=" * 78)
    return "\n".join(lines)


def render_json_report(result: MutationResult) -> str:
    return json.dumps(result.to_json(), indent=2, sort_keys=True)


__all__ = [
    "Mutant",
    "MutantOutcome",
    "MutationResult",
    "enabled",
    "enumerate_mutants",
    "global_timeout_s",
    "max_mutants",
    "mutant_timeout_s",
    "render_console_report",
    "render_json_report",
    "run_mutant",
    "run_mutation_test",
    "sample_mutants",
    "sampler_seed",
]
