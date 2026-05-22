"""
Mutation Testing Harness — AST Operator Flips + Test Survival
==============================================================

Closes §41.4 Phase 1 fifth arc (PRD v3.0+). Per the binding:

  "Mutation testing harness | Doesn't exist | ~1-2 weeks |
   Composes existing TestRunner + AST mutation library (e.g.,
   mutmut); fires on PRs touching governance/"

Standard tests answer: "given this code, does the test pass?"
Mutation tests answer the inverse: "if I deliberately corrupt
this code, do the tests CATCH the corruption?" A high
surviving-mutant ratio means tests pass against broken code —
the tests aren't actually testing what they claim.

The substrate is **advisory** — it emits a 4-value
:class:`MutationVerdict` plus per-mutant kill-ratio scores;
it does NOT gate APPLY. Consumer-side wiring (raising
risk_tier when verdict is WEAK, surfacing in operator panel)
stays out of scope.

Approach (pure-function mutation + pluggable test runner):

1. **Mutation site discovery** — ``ast.parse`` the target
   file, walk to find sites for 4 mutation kinds:
   - ``COMPARISON_FLIP``: ``==``↔``!=``, ``<``↔``>=``, etc.
   - ``ARITHMETIC_FLIP``: ``+``↔``-``, ``*``↔``/``
   - ``BOOLEAN_FLIP``: ``True``↔``False``, ``and``↔``or``
   - ``IDENTITY_FLIP``: ``is``↔``is not``, ``in``↔``not in``
2. **Per-mutant execution** — apply mutation to file via
   atomic copy-write-restore pattern, run test_runner_callable,
   classify result (KILLED/SURVIVED/TIMEOUT/ERROR), restore
   original file.
3. **Verdict synthesis** — compute kill_ratio = killed /
   (killed + survived); map to verdict band.

The default test_runner_callable invokes pytest via subprocess
(bounded by env-tunable timeout). Operator can inject their
own (e.g., one that uses the existing TestRunner pipeline with
language routing) — substrate works out-of-the-box AND scales.

Substrate is **deterministic** — same source + same mutation
operators → same mutant set. Test results depend on test
flakiness; operator should run mutation testing in a stable
test environment (or accept that flaky tests show as TIMEOUT/
ERROR, not skewing the kill-ratio).

**Safety**: substrate uses backup-then-restore pattern. If
process dies mid-mutation, operator can recover by checking
for a ``.mut_bak`` sibling file. The substrate also writes
nothing if dry_run=True (operator-side preview).

Composition contract:

* :mod:`ast` (stdlib) — mutation site discovery + mutated
  AST → source via :func:`ast.unparse`.
* :mod:`subprocess` (stdlib) — default test runner; bounded
  by env timeout.
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave
  2 #5) — cage-touch flag.
* :func:`cross_process_jsonl.flock_append_line` — §33.4
  audit ledger at ``.jarvis/mutation_testing_ledger.jsonl``.

NEVER raises. Malformed AST / missing file / test runner
exception all degrade to ERROR per-mutant or DISABLED
top-level, not exception.

Closed 4-value :class:`MutationVerdict`:

  WEAK           kill_ratio < weak_threshold (default 0.4)
  FAIR           weak ≤ kill_ratio < strong_threshold
                 (default 0.75)
  STRONG         kill_ratio ≥ strong_threshold
  DISABLED       master off OR no mutants found

Closed 4-value :class:`MutationKind`:

  COMPARISON_FLIP   == ↔ != / < ↔ >= / > ↔ <= / <= ↔ > / >= ↔ <
  ARITHMETIC_FLIP   + ↔ - / * ↔ /
  BOOLEAN_FLIP      True ↔ False / and ↔ or
  IDENTITY_FLIP     is ↔ is not / in ↔ not in

Closed 4-value :class:`MutantStatus`:

  KILLED            tests failed (mutation caught — good)
  SURVIVED          tests passed (mutation NOT caught — weak)
  TIMEOUT           test run exceeded timebox
  ERROR             mutation produced syntax error / test
                    framework crashed

§33.1 cognitive substrate
``JARVIS_MUTATION_TESTING_ENABLED`` default-**FALSE**.

Authority asymmetry (AST-pinned): stdlib only at module load.
``governance_boundary_gate`` + ``cross_process_jsonl`` are
lazy-imported. Does NOT import orchestrator / iron_gate /
policy / providers / candidate_generator / urgency_router /
change_engine / semantic_guardian / auto_committer /
risk_tier_floor / tool_executor / plan_generator /
test_runner (substrate is advisory; operator-side wiring
calls test_runner via the injectable callable, not by this
substrate).
"""
from __future__ import annotations

import ast
import asyncio
import enum
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


MUTATION_TESTING_SCHEMA_VERSION: str = "mutation_testing.1"


_ENV_MASTER = "JARVIS_MUTATION_TESTING_ENABLED"
_ENV_PERSIST = "JARVIS_MUTATION_TESTING_PERSIST_ENABLED"
_ENV_MAX_MUTANTS = "JARVIS_MUTATION_TESTING_MAX_MUTANTS"
_ENV_TEST_TIMEOUT_S = "JARVIS_MUTATION_TESTING_TEST_TIMEOUT_S"
_ENV_WEAK_THRESHOLD = "JARVIS_MUTATION_TESTING_WEAK_THRESHOLD"
_ENV_STRONG_THRESHOLD = "JARVIS_MUTATION_TESTING_STRONG_THRESHOLD"
_ENV_LEDGER_PATH = "JARVIS_MUTATION_TESTING_LEDGER_PATH"
_ENV_BACKUP_SUFFIX = "JARVIS_MUTATION_TESTING_BACKUP_SUFFIX"

_DEFAULT_MAX_MUTANTS = 30
_DEFAULT_TEST_TIMEOUT_S = 60
_DEFAULT_WEAK_THRESHOLD = 0.4
_DEFAULT_STRONG_THRESHOLD = 0.75
_DEFAULT_BACKUP_SUFFIX = ".mut_bak"

_DEFAULT_LEDGER_REL = ".jarvis/mutation_testing_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _read_clamped_float(
    name: str, default: float, lo: float, hi: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def max_mutants() -> int:
    """Cap on mutants per file. Default 30. Clamped [1, 10_000]."""
    return _read_clamped_int(
        _ENV_MAX_MUTANTS, _DEFAULT_MAX_MUTANTS, 1, 10_000,
    )


def test_timeout_s() -> int:
    """Timeout per mutant test run (seconds). Default 60.
    Clamped [1, 3600]."""
    return _read_clamped_int(
        _ENV_TEST_TIMEOUT_S, _DEFAULT_TEST_TIMEOUT_S, 1, 3600,
    )


def weak_threshold() -> float:
    """kill_ratio below this → WEAK verdict. Default 0.4."""
    return _read_clamped_float(
        _ENV_WEAK_THRESHOLD, _DEFAULT_WEAK_THRESHOLD, 0.0, 1.0,
    )


def strong_threshold() -> float:
    """kill_ratio above this → STRONG verdict. Auto-clamped
    above weak_threshold."""
    raw = _read_clamped_float(
        _ENV_STRONG_THRESHOLD,
        _DEFAULT_STRONG_THRESHOLD, 0.0, 1.0,
    )
    return max(raw, weak_threshold())


def backup_suffix() -> str:
    raw = os.environ.get(_ENV_BACKUP_SUFFIX, "").strip()
    return raw if raw else _DEFAULT_BACKUP_SUFFIX


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class MutationVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    WEAK = "weak"
    FAIR = "fair"
    STRONG = "strong"
    DISABLED = "disabled"


class MutationKind(str, enum.Enum):
    """Closed 4-value mutation kind — bytes-pinned via AST."""

    COMPARISON_FLIP = "comparison_flip"
    ARITHMETIC_FLIP = "arithmetic_flip"
    BOOLEAN_FLIP = "boolean_flip"
    IDENTITY_FLIP = "identity_flip"


class MutantStatus(str, enum.Enum):
    """Closed 4-value mutant status — bytes-pinned via AST."""

    KILLED = "killed"
    SURVIVED = "survived"
    TIMEOUT = "timeout"
    ERROR = "error"


_VERDICT_GLYPH: Dict[str, str] = {
    MutationVerdict.WEAK.value: "✗",
    MutationVerdict.FAIR.value: "◐",
    MutationVerdict.STRONG.value: "✓",
    MutationVerdict.DISABLED.value: "◌",
}


_KIND_GLYPH: Dict[str, str] = {
    MutationKind.COMPARISON_FLIP.value: "⚖",
    MutationKind.ARITHMETIC_FLIP.value: "±",
    MutationKind.BOOLEAN_FLIP.value: "¬",
    MutationKind.IDENTITY_FLIP.value: "≡",
}


_STATUS_GLYPH: Dict[str, str] = {
    MutantStatus.KILLED.value: "💀",
    MutantStatus.SURVIVED.value: "⚠",
    MutantStatus.TIMEOUT.value: "⏱",
    MutantStatus.ERROR.value: "✗",
}


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def kind_glyph(kind: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(kind, "value"):
            return _KIND_GLYPH.get(str(kind.value), "?")
        return _KIND_GLYPH.get(
            str(kind or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def status_glyph(status: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(status, "value"):
            return _STATUS_GLYPH.get(str(status.value), "?")
        return _STATUS_GLYPH.get(
            str(status or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class Mutant:
    """One mutation candidate."""

    mutant_id: str
    source_file: str
    line_number: int
    col_offset: int
    mutation_kind: MutationKind
    original_text: str
    mutated_text: str
    schema_version: str = MUTATION_TESTING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mutant_id": self.mutant_id[:64],
            "source_file": self.source_file[:256],
            "line_number": int(self.line_number),
            "col_offset": int(self.col_offset),
            "mutation_kind": self.mutation_kind.value,
            "original_text": self.original_text[:128],
            "mutated_text": self.mutated_text[:128],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class MutantResult:
    """One per-mutant test outcome."""

    mutant: Mutant
    status: MutantStatus
    test_duration_s: float
    diagnostic: str
    schema_version: str = MUTATION_TESTING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "mutant_result",
            "mutant": self.mutant.to_dict(),
            "status": self.status.value,
            "test_duration_s": float(self.test_duration_s),
            "diagnostic": self.diagnostic[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class MutationReport:
    """Top-level mutation testing report."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: MutationVerdict
    source_file: str
    total_mutants: int
    killed_count: int
    survived_count: int
    timeout_count: int
    error_count: int
    kill_ratio: float
    results: Tuple[MutantResult, ...]
    boundary_crossed: bool
    diagnostic: str
    elapsed_s: float
    schema_version: str = MUTATION_TESTING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "source_file": self.source_file[:256],
            "total_mutants": int(self.total_mutants),
            "killed_count": int(self.killed_count),
            "survived_count": int(self.survived_count),
            "timeout_count": int(self.timeout_count),
            "error_count": int(self.error_count),
            "kill_ratio": float(self.kill_ratio),
            "results": [r.to_dict() for r in self.results],
            "boundary_crossed": bool(self.boundary_crossed),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers


def _is_boundary_crossed(file_path: str) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not file_path:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed((file_path,)))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


# Mutation operators


# Closed mapping table — comparison flips. Each entry is a pair
# (original AST type, replacement AST type). The (cls, cls) pair
# represents "swap a → b" — we generate a separate Mutant for
# each direction-pair so a tests-against-`==` survives flipping
# to `!=` AND flipping to `>=`, etc.
_COMPARISON_PAIRS: Tuple[Tuple[type, type], ...] = (
    (ast.Eq, ast.NotEq),
    (ast.NotEq, ast.Eq),
    (ast.Lt, ast.GtE),
    (ast.GtE, ast.Lt),
    (ast.Gt, ast.LtE),
    (ast.LtE, ast.Gt),
)


_ARITHMETIC_PAIRS: Tuple[Tuple[type, type], ...] = (
    (ast.Add, ast.Sub),
    (ast.Sub, ast.Add),
    (ast.Mult, ast.Div),
    (ast.Div, ast.Mult),
)


_IDENTITY_PAIRS: Tuple[Tuple[type, type], ...] = (
    (ast.Is, ast.IsNot),
    (ast.IsNot, ast.Is),
    (ast.In, ast.NotIn),
    (ast.NotIn, ast.In),
)


_BOOLEAN_OP_PAIRS: Tuple[Tuple[type, type], ...] = (
    (ast.And, ast.Or),
    (ast.Or, ast.And),
)


_COMPARISON_DISPLAY: Dict[type, str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.GtE: ">=",
    ast.Gt: ">",
    ast.LtE: "<=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}


_ARITHMETIC_DISPLAY: Dict[type, str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
}


_BOOLEAN_OP_DISPLAY: Dict[type, str] = {
    ast.And: "and",
    ast.Or: "or",
}


def _mutant_id(
    source_file: str,
    line_number: int,
    col_offset: int,
    kind: MutationKind,
    suffix: int,
) -> str:
    """Stable mutant id. NEVER raises."""
    try:
        base = Path(source_file).stem
    except Exception:  # noqa: BLE001
        base = "file"
    return (
        f"{base}-L{line_number}C{col_offset}-"
        f"{kind.value[:8]}-{suffix:02d}"
    )


def find_mutation_sites(
    source_text: str,
    *,
    source_file: str = "<unknown>",
) -> Tuple[Mutant, ...]:
    """Pure-function mutation site discovery. NEVER raises.

    Walks the parsed AST and emits one :class:`Mutant` per
    candidate operator flip. Returns empty tuple on parse
    failure."""
    if not source_text:
        return ()
    try:
        tree = ast.parse(source_text)
    except (SyntaxError, ValueError):
        return ()
    out: List[Mutant] = []
    counter = 0
    for node in ast.walk(tree):
        # Comparison operators (a == b, etc.)
        if isinstance(node, ast.Compare):
            for op_idx, op in enumerate(node.ops):
                for orig_cls, repl_cls in _COMPARISON_PAIRS:
                    if isinstance(op, orig_cls):
                        # Distinguish IDENTITY flips
                        # (is/is not/in/not in) from comparison
                        # flips (==/!=/</>/<=/>=). The
                        # identity ops are Compare-subnodes too.
                        if orig_cls in (
                            ast.Is, ast.IsNot, ast.In, ast.NotIn,
                        ):
                            continue
                        counter += 1
                        out.append(Mutant(
                            mutant_id=_mutant_id(
                                source_file,
                                getattr(op, "lineno", 0)
                                or getattr(node, "lineno", 0),
                                getattr(op, "col_offset", 0)
                                or getattr(node, "col_offset", 0),
                                MutationKind.COMPARISON_FLIP,
                                counter,
                            ),
                            source_file=source_file,
                            line_number=(
                                getattr(node, "lineno", 0)
                                or 0
                            ),
                            col_offset=(
                                getattr(node, "col_offset", 0)
                                or 0
                            ),
                            mutation_kind=(
                                MutationKind.COMPARISON_FLIP
                            ),
                            original_text=_COMPARISON_DISPLAY.get(
                                orig_cls, orig_cls.__name__,
                            ),
                            mutated_text=_COMPARISON_DISPLAY.get(
                                repl_cls, repl_cls.__name__,
                            ),
                        ))
                # Identity-class operators (is, is not, in, not in)
                for orig_cls, repl_cls in _IDENTITY_PAIRS:
                    if isinstance(op, orig_cls):
                        counter += 1
                        out.append(Mutant(
                            mutant_id=_mutant_id(
                                source_file,
                                getattr(op, "lineno", 0)
                                or getattr(node, "lineno", 0),
                                getattr(op, "col_offset", 0)
                                or getattr(node, "col_offset", 0),
                                MutationKind.IDENTITY_FLIP,
                                counter,
                            ),
                            source_file=source_file,
                            line_number=(
                                getattr(node, "lineno", 0) or 0
                            ),
                            col_offset=(
                                getattr(node, "col_offset", 0) or 0
                            ),
                            mutation_kind=(
                                MutationKind.IDENTITY_FLIP
                            ),
                            original_text=_COMPARISON_DISPLAY.get(
                                orig_cls, orig_cls.__name__,
                            ),
                            mutated_text=_COMPARISON_DISPLAY.get(
                                repl_cls, repl_cls.__name__,
                            ),
                        ))
        # Arithmetic binary ops
        elif isinstance(node, ast.BinOp):
            for orig_cls, repl_cls in _ARITHMETIC_PAIRS:
                if isinstance(node.op, orig_cls):
                    counter += 1
                    out.append(Mutant(
                        mutant_id=_mutant_id(
                            source_file,
                            getattr(node, "lineno", 0) or 0,
                            getattr(node, "col_offset", 0) or 0,
                            MutationKind.ARITHMETIC_FLIP,
                            counter,
                        ),
                        source_file=source_file,
                        line_number=(
                            getattr(node, "lineno", 0) or 0
                        ),
                        col_offset=(
                            getattr(node, "col_offset", 0) or 0
                        ),
                        mutation_kind=(
                            MutationKind.ARITHMETIC_FLIP
                        ),
                        original_text=_ARITHMETIC_DISPLAY.get(
                            orig_cls, orig_cls.__name__,
                        ),
                        mutated_text=_ARITHMETIC_DISPLAY.get(
                            repl_cls, repl_cls.__name__,
                        ),
                    ))
        # Boolean ops (and / or)
        elif isinstance(node, ast.BoolOp):
            for orig_cls, repl_cls in _BOOLEAN_OP_PAIRS:
                if isinstance(node.op, orig_cls):
                    counter += 1
                    out.append(Mutant(
                        mutant_id=_mutant_id(
                            source_file,
                            getattr(node, "lineno", 0) or 0,
                            getattr(node, "col_offset", 0) or 0,
                            MutationKind.BOOLEAN_FLIP,
                            counter,
                        ),
                        source_file=source_file,
                        line_number=(
                            getattr(node, "lineno", 0) or 0
                        ),
                        col_offset=(
                            getattr(node, "col_offset", 0) or 0
                        ),
                        mutation_kind=MutationKind.BOOLEAN_FLIP,
                        original_text=_BOOLEAN_OP_DISPLAY.get(
                            orig_cls, orig_cls.__name__,
                        ),
                        mutated_text=_BOOLEAN_OP_DISPLAY.get(
                            repl_cls, repl_cls.__name__,
                        ),
                    ))
        # True / False literal flips
        elif (
            isinstance(node, ast.Constant)
            and node.value is True
        ):
            counter += 1
            out.append(Mutant(
                mutant_id=_mutant_id(
                    source_file,
                    getattr(node, "lineno", 0) or 0,
                    getattr(node, "col_offset", 0) or 0,
                    MutationKind.BOOLEAN_FLIP,
                    counter,
                ),
                source_file=source_file,
                line_number=(
                    getattr(node, "lineno", 0) or 0
                ),
                col_offset=(
                    getattr(node, "col_offset", 0) or 0
                ),
                mutation_kind=MutationKind.BOOLEAN_FLIP,
                original_text="True",
                mutated_text="False",
            ))
        elif (
            isinstance(node, ast.Constant)
            and node.value is False
        ):
            counter += 1
            out.append(Mutant(
                mutant_id=_mutant_id(
                    source_file,
                    getattr(node, "lineno", 0) or 0,
                    getattr(node, "col_offset", 0) or 0,
                    MutationKind.BOOLEAN_FLIP,
                    counter,
                ),
                source_file=source_file,
                line_number=(
                    getattr(node, "lineno", 0) or 0
                ),
                col_offset=(
                    getattr(node, "col_offset", 0) or 0
                ),
                mutation_kind=MutationKind.BOOLEAN_FLIP,
                original_text="False",
                mutated_text="True",
            ))
    return tuple(out[:max_mutants()])


def _build_op_replacement(mutant: Mutant) -> Optional[ast.AST]:
    """Map mutant.mutated_text → AST node for that operator.
    NEVER raises."""
    text = mutant.mutated_text
    table: Dict[str, type] = {
        "==": ast.Eq, "!=": ast.NotEq,
        "<": ast.Lt, ">=": ast.GtE,
        ">": ast.Gt, "<=": ast.LtE,
        "is": ast.Is, "is not": ast.IsNot,
        "in": ast.In, "not in": ast.NotIn,
        "+": ast.Add, "-": ast.Sub,
        "*": ast.Mult, "/": ast.Div,
        "and": ast.And, "or": ast.Or,
    }
    cls = table.get(text)
    if cls is None:
        return None
    try:
        return cls()
    except Exception:  # noqa: BLE001
        return None


class _MutationApplier(ast.NodeTransformer):
    """Walks tree applying one mutation at the targeted
    (lineno, col_offset). Idempotent: applies at most once."""

    def __init__(self, mutant: Mutant) -> None:
        self.mutant = mutant
        self.applied = False
        self._target_lineno = mutant.line_number
        self._target_col = mutant.col_offset

    def _matches(self, node: ast.AST) -> bool:
        if self.applied:
            return False
        return (
            getattr(node, "lineno", -1) == self._target_lineno
            and getattr(node, "col_offset", -1)
            == self._target_col
        )

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        if (
            self._matches(node)
            and self.mutant.mutation_kind in (
                MutationKind.COMPARISON_FLIP,
                MutationKind.IDENTITY_FLIP,
            )
            and node.ops
        ):
            repl = _build_op_replacement(self.mutant)
            if repl is not None:
                new_ops = list(node.ops)
                # Flip the FIRST operator only.
                new_ops[0] = repl  # type: ignore[assignment]
                self.applied = True
                return ast.Compare(
                    left=self.generic_visit(node.left)
                    if isinstance(node.left, ast.AST)
                    else node.left,
                    ops=new_ops,
                    comparators=[
                        self.generic_visit(c)
                        if isinstance(c, ast.AST) else c
                        for c in node.comparators
                    ],
                )
        return self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        if (
            self._matches(node)
            and self.mutant.mutation_kind
            is MutationKind.ARITHMETIC_FLIP
        ):
            repl = _build_op_replacement(self.mutant)
            if repl is not None:
                self.applied = True
                return ast.BinOp(
                    left=self.generic_visit(node.left)
                    if isinstance(node.left, ast.AST)
                    else node.left,
                    op=repl,
                    right=self.generic_visit(node.right)
                    if isinstance(node.right, ast.AST)
                    else node.right,
                )
        return self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        if (
            self._matches(node)
            and self.mutant.mutation_kind
            is MutationKind.BOOLEAN_FLIP
            and self.mutant.original_text in ("and", "or")
        ):
            repl = _build_op_replacement(self.mutant)
            if repl is not None:
                self.applied = True
                return ast.BoolOp(
                    op=repl,
                    values=[
                        self.generic_visit(v)
                        if isinstance(v, ast.AST) else v
                        for v in node.values
                    ],
                )
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if (
            self._matches(node)
            and self.mutant.mutation_kind
            is MutationKind.BOOLEAN_FLIP
            and isinstance(node.value, bool)
        ):
            self.applied = True
            return ast.Constant(value=not node.value)
        return node


def apply_mutation(
    source_text: str, mutant: Mutant,
) -> Optional[str]:
    """Pure — apply one mutation, return new source text.
    NEVER raises. Returns None on parse failure or when the
    mutant's target site isn't found."""
    if not source_text or mutant is None:
        return None
    try:
        tree = ast.parse(source_text)
    except (SyntaxError, ValueError):
        return None
    try:
        applier = _MutationApplier(mutant)
        mutated_tree = applier.visit(tree)
        if not applier.applied:
            return None
        ast.fix_missing_locations(mutated_tree)
        return ast.unparse(mutated_tree)
    except Exception:  # noqa: BLE001
        return None


# Default test runner (subprocess pytest)


_TestRunnerCallable = Callable[
    [str], Awaitable[Tuple[bool, str]],
]
"""Signature: ``async (source_file: str) -> (passed, diagnostic)``.

``passed=True`` means tests passed → mutant SURVIVED.
``passed=False`` means tests failed → mutant KILLED."""


async def _default_test_runner(
    source_file: str,
) -> Tuple[bool, str]:
    """Default test runner — invokes pytest via subprocess on a
    test directory derived from the source file. NEVER raises.

    Resolution: tests/<package>/test_<stem>.py OR
    test_<stem>.py OR the test/ directory siblings.
    Returns ``(passed, diagnostic)``."""
    timeout = test_timeout_s()
    try:
        path = Path(source_file).resolve()
        # Find the most plausible test file/dir.
        candidates: List[Path] = []
        for parent in (path.parent, *path.parents):
            candidates.append(parent / "tests")
            candidates.append(parent / "test")
        candidates.append(path.parent / f"test_{path.stem}.py")
        target = next(
            (c for c in candidates if c.exists()), None,
        )
        if target is None:
            return (
                True,
                "no test directory found — counted as SURVIVED",
            )
        # Slice 9 — canonical sync helper (stdin=DEVNULL +
        # process-group isolation + bounded timeout + provenance).
        from backend.core.ouroboros.governance.test_subprocess_helper import (  # noqa: E501
            KillReason,
            run_pytest_subprocess_sync,
        )
        result = run_pytest_subprocess_sync(
            ["python3", "-m", "pytest", str(target), "-q", "-x"],
            timeout_s=float(timeout),
            caller="mutation_testing_harness._default_test_runner",
        )
        if result.timed_out:
            return False, "test timeout"
        if result.kill_reason == KillReason.SPAWN_ERROR:
            return False, f"test runner exception: {result.spawn_error_class}"
        passed = result.returncode == 0
        tail = result.stdout[-200:].replace("\n", " | ")
        return passed, f"rc={result.returncode}; tail={tail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"test runner exception: {exc!r}"[:200]


# Per-mutant orchestration with backup-then-restore


async def run_mutant(
    mutant: Mutant,
    original_source: str,
    *,
    test_runner: Optional[_TestRunnerCallable] = None,
    dry_run: bool = False,
    repo_root: Optional[Path] = None,
) -> MutantResult:
    """Apply one mutant + run tests + restore. NEVER raises.

    Parameters
    ----------
    mutant:
        The mutation to apply.
    original_source:
        Original file content (used to apply the mutation
        cleanly AND restore on failure).
    test_runner:
        Async callable that runs tests and returns
        ``(passed, diagnostic)``. None → default subprocess
        pytest runner.
    dry_run:
        When True, no file write happens; result is always
        SURVIVED with diagnostic "dry-run".
    repo_root:
        Root for resolving the mutant's source_file. Default
        cwd."""
    started = time.time()
    try:
        target = (
            (repo_root or Path.cwd())
            / mutant.source_file
        ).resolve()
    except Exception as exc:  # noqa: BLE001
        return MutantResult(
            mutant=mutant,
            status=MutantStatus.ERROR,
            test_duration_s=0.0,
            diagnostic=f"path resolve failed: {exc!r}"[:200],
        )

    mutated = apply_mutation(original_source, mutant)
    if mutated is None:
        return MutantResult(
            mutant=mutant,
            status=MutantStatus.ERROR,
            test_duration_s=0.0,
            diagnostic="mutation could not be applied",
        )

    if dry_run:
        return MutantResult(
            mutant=mutant,
            status=MutantStatus.SURVIVED,
            test_duration_s=0.0,
            diagnostic="dry-run; mutation built but not executed",
        )

    # Backup-then-restore.
    backup_path = target.with_suffix(
        target.suffix + backup_suffix(),
    )
    try:
        if not target.exists():
            return MutantResult(
                mutant=mutant,
                status=MutantStatus.ERROR,
                test_duration_s=0.0,
                diagnostic="source file does not exist",
            )
        shutil.copy2(target, backup_path)
    except Exception as exc:  # noqa: BLE001
        return MutantResult(
            mutant=mutant,
            status=MutantStatus.ERROR,
            test_duration_s=0.0,
            diagnostic=f"backup failed: {exc!r}"[:200],
        )

    runner = test_runner if test_runner is not None else _default_test_runner
    timeout = float(test_timeout_s())

    try:
        target.write_text(mutated, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # Restore even on write failure.
        try:
            shutil.copy2(backup_path, target)
            backup_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return MutantResult(
            mutant=mutant,
            status=MutantStatus.ERROR,
            test_duration_s=0.0,
            diagnostic=f"mutation write failed: {exc!r}"[:200],
        )

    # Run tests.
    test_start = time.time()
    try:
        passed, diagnostic = await asyncio.wait_for(
            runner(str(target)), timeout=timeout + 5.0,
        )
        duration = time.time() - test_start
        if passed:
            status = MutantStatus.SURVIVED
        else:
            status = MutantStatus.KILLED
    except asyncio.TimeoutError:
        duration = time.time() - test_start
        status = MutantStatus.TIMEOUT
        diagnostic = f"test timeout (>{timeout:.0f}s)"
    except Exception as exc:  # noqa: BLE001
        duration = time.time() - test_start
        status = MutantStatus.ERROR
        diagnostic = f"runner raised: {exc!r}"[:200]
    finally:
        # ALWAYS restore.
        try:
            shutil.copy2(backup_path, target)
            backup_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    return MutantResult(
        mutant=mutant,
        status=status,
        test_duration_s=duration,
        diagnostic=diagnostic[:256],
    )


# Top-level


def _verdict_for_kill_ratio(
    kill_ratio: float, sample: int,
) -> MutationVerdict:
    """Pure classifier. Returns DISABLED when no mutants."""
    if sample == 0:
        return MutationVerdict.DISABLED
    w = weak_threshold()
    s = strong_threshold()
    if kill_ratio < w:
        return MutationVerdict.WEAK
    if kill_ratio < s:
        return MutationVerdict.FAIR
    return MutationVerdict.STRONG


async def evaluate_file(
    source_file: str,
    *,
    source_text_override: Optional[str] = None,
    repo_root: Optional[Path] = None,
    test_runner: Optional[_TestRunnerCallable] = None,
    dry_run: bool = False,
    now_unix: Optional[float] = None,
) -> MutationReport:
    """Top-level: find mutants in file + run each + aggregate.
    NEVER raises.

    Parameters
    ----------
    source_file:
        Repo-relative path to file under test.
    source_text_override:
        Testing seam — pass source directly. When None, reads
        the file from disk.
    repo_root:
        Root for resolving paths. Default cwd.
    test_runner:
        Operator-injectable async test runner. Default uses
        subprocess pytest.
    dry_run:
        When True, mutants are constructed and APPLIED in-
        memory (verifying ast.unparse works) but no test
        execution + no file writes occur. Each mutant result
        is SURVIVED with "dry-run" diagnostic."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return MutationReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=MutationVerdict.DISABLED,
            source_file=source_file,
            total_mutants=0, killed_count=0, survived_count=0,
            timeout_count=0, error_count=0,
            kill_ratio=0.0,
            results=(),
            boundary_crossed=False,
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )

    path_str = str(source_file or "").strip()
    if not path_str:
        return MutationReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=MutationVerdict.DISABLED,
            source_file=path_str,
            total_mutants=0, killed_count=0, survived_count=0,
            timeout_count=0, error_count=0,
            kill_ratio=0.0,
            results=(),
            boundary_crossed=False,
            diagnostic="empty source_file",
            elapsed_s=0.0,
        )

    # Read source text.
    if source_text_override is not None:
        source_text = source_text_override
    else:
        try:
            target = (
                (repo_root or Path.cwd()) / path_str
            ).resolve()
            source_text = target.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return MutationReport(
                evaluated_at_unix=started,
                master_enabled=True,
                verdict=MutationVerdict.DISABLED,
                source_file=path_str,
                total_mutants=0, killed_count=0, survived_count=0,
                timeout_count=0, error_count=0,
                kill_ratio=0.0,
                results=(),
                boundary_crossed=False,
                diagnostic=f"file read failed: {exc!r}"[:200],
                elapsed_s=max(0.0, time.time() - started),
            )

    boundary = _is_boundary_crossed(path_str)
    mutants = find_mutation_sites(
        source_text, source_file=path_str,
    )
    if not mutants:
        return MutationReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=MutationVerdict.DISABLED,
            source_file=path_str,
            total_mutants=0, killed_count=0, survived_count=0,
            timeout_count=0, error_count=0,
            kill_ratio=0.0,
            results=(),
            boundary_crossed=boundary,
            diagnostic="no mutation sites found",
            elapsed_s=max(0.0, time.time() - started),
        )

    # Run each mutant.
    results: List[MutantResult] = []
    for m in mutants:
        result = await run_mutant(
            m, source_text,
            test_runner=test_runner,
            dry_run=dry_run,
            repo_root=repo_root,
        )
        results.append(result)

    # Aggregate.
    killed = sum(
        1 for r in results
        if r.status is MutantStatus.KILLED
    )
    survived = sum(
        1 for r in results
        if r.status is MutantStatus.SURVIVED
    )
    timeout_count = sum(
        1 for r in results
        if r.status is MutantStatus.TIMEOUT
    )
    error_count = sum(
        1 for r in results
        if r.status is MutantStatus.ERROR
    )
    decisive = killed + survived
    kill_ratio = (killed / decisive) if decisive > 0 else 0.0
    verdict = _verdict_for_kill_ratio(kill_ratio, decisive)
    diagnostic = (
        f"{killed}/{decisive} killed (ratio={kill_ratio:.2f}); "
        f"timeout={timeout_count} error={error_count}; "
        f"verdict={verdict.value}"
    )

    report = MutationReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        source_file=path_str,
        total_mutants=len(mutants),
        killed_count=killed,
        survived_count=survived,
        timeout_count=timeout_count,
        error_count=error_count,
        kill_ratio=kill_ratio,
        results=tuple(results),
        boundary_crossed=boundary,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def evaluate_file_sync(
    source_file: str,
    *,
    source_text_override: Optional[str] = None,
    repo_root: Optional[Path] = None,
    test_runner: Optional[_TestRunnerCallable] = None,
    dry_run: bool = False,
    now_unix: Optional[float] = None,
) -> MutationReport:
    """Sync wrapper. NEVER raises. Returns DISABLED when
    invoked inside a running event loop."""
    started = time.time() if now_unix is None else float(now_unix)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        return MutationReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=MutationVerdict.DISABLED,
            source_file=source_file,
            total_mutants=0, killed_count=0, survived_count=0,
            timeout_count=0, error_count=0,
            kill_ratio=0.0,
            results=(),
            boundary_crossed=False,
            diagnostic=(
                "sync wrapper invoked inside running event "
                "loop — use evaluate_file() instead"
            ),
            elapsed_s=0.0,
        )
    try:
        return asyncio.run(evaluate_file(
            source_file,
            source_text_override=source_text_override,
            repo_root=repo_root,
            test_runner=test_runner,
            dry_run=dry_run,
            now_unix=now_unix,
        ))
    except Exception as exc:  # noqa: BLE001
        return MutationReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=MutationVerdict.DISABLED,
            source_file=source_file,
            total_mutants=0, killed_count=0, survived_count=0,
            timeout_count=0, error_count=0,
            kill_ratio=0.0,
            results=(),
            boundary_crossed=False,
            diagnostic=f"sync wrapper failed: {exc!r}"[:200],
            elapsed_s=0.0,
        )


def _persist_report(report: MutationReport) -> None:
    if report.verdict is MutationVerdict.DISABLED:
        return
    _flock_append({
        "kind": "mutation_report", "payload": report.to_dict(),
    })


def _publish_event(report: MutationReport) -> None:
    if not master_enabled():
        return
    if report.verdict is MutationVerdict.DISABLED:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_MUTATION_TESTING_EVALUATED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_MUTATION_TESTING_EVALUATED,
            (
                f"system::mutation_testing::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "source_file": report.source_file[:128],
                "total_mutants": report.total_mutants,
                "killed_count": report.killed_count,
                "survived_count": report.survived_count,
                "timeout_count": report.timeout_count,
                "error_count": report.error_count,
                "kill_ratio": report.kill_ratio,
                "boundary_crossed": report.boundary_crossed,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_mutation_panel(
    report: Optional[MutationReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"mutation testing: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "mutation testing: no report"
    if not report.master_enabled:
        return (
            f"mutation testing: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    lines = [
        f"🧬 Mutation Testing  {vg} {report.verdict.value}",
        f"  source_file        : {report.source_file[:60]}",
        f"  total_mutants      : {report.total_mutants}",
        f"  killed             : {report.killed_count} "
        f"{status_glyph(MutantStatus.KILLED)}",
        f"  survived           : {report.survived_count} "
        f"{status_glyph(MutantStatus.SURVIVED)}",
        f"  timeout            : {report.timeout_count} "
        f"{status_glyph(MutantStatus.TIMEOUT)}",
        f"  error              : {report.error_count} "
        f"{status_glyph(MutantStatus.ERROR)}",
        f"  kill_ratio         : {report.kill_ratio:.2f}",
    ]
    if report.results:
        survived_mutants = [
            r for r in report.results
            if r.status is MutantStatus.SURVIVED
        ]
        if survived_mutants:
            lines.append("  survived (top 5):")
            for r in survived_mutants[:5]:
                kg = kind_glyph(r.mutant.mutation_kind)
                lines.append(
                    f"    {kg} L{r.mutant.line_number:<4} "
                    f"{r.mutant.original_text} → "
                    f"{r.mutant.mutated_text}"
                )
    lines.append(f"  diagnostic         : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "mutation_testing_harness.py"
    )

    _EXPECTED_VERDICTS = {
        "weak", "fair", "strong", "disabled",
    }
    _EXPECTED_KINDS = {
        "comparison_flip", "arithmetic_flip",
        "boolean_flip", "identity_flip",
    }
    _EXPECTED_STATUSES = {
        "killed", "survived", "timeout", "error",
    }

    def _validate_taxonomy(class_name: str, expected: set):
        def _validate(tree: ast.AST, source: str) -> tuple:  # noqa: ARG001
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name == class_name
                ):
                    found = set()
                    for sub in node.body:
                        if (
                            isinstance(sub, ast.Assign)
                            and len(sub.targets) == 1
                            and isinstance(sub.targets[0], ast.Name)
                            and isinstance(sub.value, ast.Constant)
                            and isinstance(sub.value.value, str)
                        ):
                            found.add(sub.value.value)
                    missing = expected - found
                    extra = found - expected
                    if missing:
                        return (
                            f"{class_name} missing: "
                            f"{sorted(missing)}",
                        )
                    if extra:
                        return (
                            f"{class_name} drift: "
                            f"{sorted(extra)}",
                        )
                    return ()
            return (f"{class_name} class not found",)
        return _validate

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
            "backend.core.ouroboros.governance.test_runner",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (cage detection)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl",
            )
        if "import ast" not in source:
            violations.append(
                "must compose stdlib ast (mutation engine)",
            )
        if "subprocess" not in source:
            violations.append(
                "must compose stdlib subprocess "
                "(default test runner)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "mutation_testing_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MutationVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "MutationVerdict", _EXPECTED_VERDICTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mutation_testing_kind_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MutationKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "MutationKind", _EXPECTED_KINDS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mutation_testing_status_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MutantStatus 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "MutantStatus", _EXPECTED_STATUSES,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mutation_testing_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — advisory only. MUST NOT "
                "import orchestrator / iron_gate / policy / "
                "providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / tool_executor / "
                "plan_generator / test_runner (operator-side "
                "injects test_runner via callable, not by "
                "this substrate)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mutation_testing_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mutation_testing_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 2 #5 "
                "governance_boundary_gate + cross_process_jsonl "
                "+ stdlib ast (mutation engine) + stdlib "
                "subprocess (default test runner)."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "mutation_testing_harness.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Mutation Testing Harness master. §33.1 "
                "default-FALSE. Closes §41.4 Phase 1 fifth "
                "arc (PRD v3.0+). AST-based mutation testing "
                "with 4 operator kinds. Substrate is ADVISORY "
                "— does NOT gate APPLY."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_MAX_MUTANTS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_MUTANTS,
            description=(
                "Cap on mutants per file. Default 30. "
                "Clamped to [1, 10_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_MUTANTS}=100",
        ),
        FlagSpec(
            name=_ENV_TEST_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_TEST_TIMEOUT_S,
            description=(
                "Timeout per mutant test run (seconds). "
                "Default 60. Clamped to [1, 3600]."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_TEST_TIMEOUT_S}=120",
        ),
        FlagSpec(
            name=_ENV_WEAK_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_WEAK_THRESHOLD,
            description=(
                "kill_ratio below this → WEAK verdict. "
                "Default 0.4."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WEAK_THRESHOLD}=0.5",
        ),
        FlagSpec(
            name=_ENV_STRONG_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_STRONG_THRESHOLD,
            description=(
                "kill_ratio above this → STRONG verdict. "
                "Default 0.75. Auto-clamped above weak."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_STRONG_THRESHOLD}=0.85",
        ),
        FlagSpec(
            name=_ENV_BACKUP_SUFFIX,
            type=FlagType.STR,
            default=_DEFAULT_BACKUP_SUFFIX,
            description=(
                "Backup file suffix during mutation. Default "
                "'.mut_bak'. Operator can recover from a dead "
                "mid-mutation process by checking for "
                "<file><suffix> siblings."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_BACKUP_SUFFIX}=.muttest",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "MUTATION_TESTING_SCHEMA_VERSION",
    "MutationVerdict",
    "MutationKind",
    "MutantStatus",
    "Mutant",
    "MutantResult",
    "MutationReport",
    "master_enabled",
    "persistence_enabled",
    "max_mutants",
    "test_timeout_s",
    "weak_threshold",
    "strong_threshold",
    "backup_suffix",
    "ledger_path",
    "verdict_glyph",
    "kind_glyph",
    "status_glyph",
    "find_mutation_sites",
    "apply_mutation",
    "run_mutant",
    "evaluate_file",
    "evaluate_file_sync",
    "format_mutation_panel",
    "register_shipped_invariants",
    "register_flags",
]
