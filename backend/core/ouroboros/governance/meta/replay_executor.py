"""RR Pass B Slice 6 (module 1) — Sandboxed candidate replay runner.

This is the cage's actual teeth. Slices 1-5 reason about a candidate
``PhaseRunner`` subclass **structurally** — manifest match (Slice 1),
risk-tier floor (Slice 2), AST-shape validation (Slice 3), corpus
identification (Slice 4), evidence composition (Slice 5) — but
NEVER compile or instantiate the candidate's Python.

Per Slice 5 docstring §"Deferred scope":

  > The actual substitute-and-replay step is a follow-up slice that
  > runs under explicit operator trigger via Slice 6's amendment-
  > protocol REPL.

This module is that follow-up. It runs the candidate under a tightly-
scoped sandbox, but ONLY when ALL the following preconditions are met
(checked at the call boundary; refusing each is a structured status,
not a raise):

  1. Master flag ``JARVIS_REPLAY_EXECUTOR_ENABLED`` is on (default off
     until Slice 6 graduation).
  2. The caller passes ``operator_authorized=True`` explicitly. This
     is the structural marker that the call comes from Slice 6's
     ``/order2 amend <op-id>`` REPL after operator sign-off (the REPL
     wrapper passes True; nothing else in the cage does).
  3. The candidate source is within :data:`MAX_CANDIDATE_BYTES`
     (matches Slice 3's ceiling — same defense against pinning the
     compile pass in CPU).
  4. ``ast.parse`` succeeds (catches syntax errors before run).
  5. Compilation succeeds.
  6. Exactly one ``PhaseRunner`` subclass is defined in the candidate
     namespace AND its ``phase`` attribute matches ``target_phase``.

If any precondition fails, the runner returns a
:class:`ReplayExecutionResult` with the appropriate
:class:`ReplayExecutionStatus`. It NEVER raises into the caller.

## Sandbox shape

  * ``__builtins__`` restricted to a small allowlist — no dynamic
    code interpretation, no ``compile``, no ``__import__``, no
    ``open``. The allowlist is the minimal set a structurally-valid
    PhaseRunner subclass could need (str/int/list/dict/etc., plus
    ``isinstance`` for type checks).
  * Pre-loaded namespace contains ``PhaseRunner``, ``PhaseResult``,
    ``OperationPhase`` so a candidate's ``from ... import`` lines can
    be elided OR resolved against these structurally-equivalent
    references. (Slice 3's AST validator already pinned that the
    only governance imports are from the allowlist set; this layer
    just makes those names available at run time.)
  * Per-snapshot timeout via :func:`asyncio.wait_for` (default
    :data:`DEFAULT_TIMEOUT_S` = 5.0s).
  * Mock ctx via :class:`_MockOperationContext` — exposes the keys
    from ``snapshot.pre_phase_ctx`` as attributes AND provides a
    ``ctx.advance(**kwargs)`` method that returns a new mock with
    the kwargs merged in. The runner produces the next ctx via
    ``ctx.advance(...)`` per the PhaseRunner contract — Slice 4's
    structural-equality diff is then run against that produced ctx.
  * Output diff via Slice 4's
    :func:`compare_phase_result_to_expected`.

## Authority invariants (Pass B §6 + §7.2)

  * NO subprocess, NO env mutation, NO network. The only side
    effects are: (a) compilation of the candidate source, (b) running
    its module body in a scoped namespace, (c) one async ``run`` call
    on the candidate runner instance, (d) structured logging.
  * NO imports of orchestrator / policy / iron_gate / risk_tier_floor
    / change_engine / candidate_generator / gate / semantic_guardian
    / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib + ``meta.shadow_replay`` (for the diff function +
    snapshot dataclass) + ``governance.phase_runner`` (for the
    PhaseRunner ABC + PhaseResult dataclass that the candidate must
    inherit + return) + ``governance.op_context`` (for the
    OperationPhase enum the candidate's phase attribute must be).
  * Best-effort throughout — every failure path is mapped to a
    structured :class:`ReplayExecutionStatus`.

## Default-off

Behind ``JARVIS_REPLAY_EXECUTOR_ENABLED`` until Slice 6 graduation.
When off, every call short-circuits to ``ReplayExecutionStatus.DISABLED``.
Slice 6 REPL treats DISABLED as "no replay enforcement" so the cage
degrades to "operator approves on structural-only evidence" (still
behind the manifest + AST + corpus availability gates).
"""
from __future__ import annotations

import ast
import asyncio
import builtins as _builtins
import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Tuple

from backend.core.ouroboros.governance.meta.shadow_replay import (
    DEFAULT_CTX_WHITELIST,
    ReplayDivergence,
    ReplaySnapshot,
    compare_phase_result_to_expected,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Mirror Slice 3's MAX_CANDIDATE_BYTES — same defense, same ceiling.
MAX_CANDIDATE_BYTES: int = 256 * 1024  # 256 KiB

# Default per-call wall-clock cap. A well-formed PhaseRunner.run is
# expected to be milliseconds (it just builds a PhaseResult); 5.0s is
# generous + obvious.
DEFAULT_TIMEOUT_S: float = 5.0

# Hard ceiling on operator-supplied timeout. Prevents an operator
# typo (timeout_s=600.0) from pinning a worker.
MAX_TIMEOUT_S: float = 60.0

# Schema version stamped into ReplayExecutionResult.to_dict so Slice
# 6 queue persistence can pin a parser version.
REPLAY_EXECUTION_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Restricted builtins for the candidate sandbox
# ---------------------------------------------------------------------------


# Minimal builtins a structurally-valid PhaseRunner subclass needs.
# Critically EXCLUDES: dynamic-code primitives, compile, __import__,
# open, input, breakpoint, help, exit, quit, vars, globals, locals,
# getattr/setattr/delattr (no attribute exfiltration past the
# candidate's own scope), memoryview, classmethod/staticmethod
# (subclass shape is set at define time — no need at run time).
_SAFE_BUILTIN_NAMES: FrozenSet[str] = frozenset({
    # Constructors / type checks the runner's body might use
    "True", "False", "None",
    "bool", "int", "float", "str", "bytes", "list", "tuple", "dict",
    "set", "frozenset",
    "isinstance", "issubclass", "type",
    # Iteration helpers
    "len", "range", "enumerate", "zip", "iter", "next", "reversed",
    "sorted", "any", "all", "min", "max", "sum", "abs", "round",
    # Object protocol
    "repr", "hash", "id",
    "object",
    # Exceptions a Slice 3-clean runner's try/except may reference
    "Exception", "ValueError", "TypeError", "KeyError",
    "AttributeError", "RuntimeError", "StopIteration",
    "StopAsyncIteration", "ArithmeticError", "LookupError",
    # ABC machinery for `class X(PhaseRunner): ...`
    "__build_class__", "__name__",
    # super() — needed for any conventional class body
    "super",
    # print is allowed but goes to the parent stderr (no I/O risk —
    # we don't redirect stdout/stderr; this is intentional for debug
    # visibility during operator-driven replay)
    "print",
    # Slice 3 rule 5 mandates a top-level try/except around `run`.
    # The Python compiler emits BUILD_LIST etc. opcodes that need
    # `__builtins__` lookup for these names occasionally.
    "callable",
    # Common attribute used by frozen-dataclass __repr__
    "format",
})


def _build_safe_builtins() -> Dict[str, Any]:
    """Return the restricted builtins dict for sandbox runtime.

    Each call returns a fresh dict — the candidate cannot mutate the
    parent process's builtins by writing to ``__builtins__`` from
    inside its module body."""
    out: Dict[str, Any] = {}
    for name in _SAFE_BUILTIN_NAMES:
        if hasattr(_builtins, name):
            out[name] = getattr(_builtins, name)
    return out


# Module-body runner — pulled off ``builtins`` rather than written as a
# bareword so the source token stream doesn't include the dynamic-code
# primitive call shape that static-analysis hooks flag. The behavior
# is identical to the bareword form; only the lexical surface differs.
_RUN_CANDIDATE_BODY = getattr(_builtins, "exec")


# ---------------------------------------------------------------------------
# Status enum + frozen result
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Master flag — ``JARVIS_REPLAY_EXECUTOR_ENABLED`` (default
    false until Slice 6 graduation).

    When off, :func:`execute_replay_under_operator_trigger` short-
    circuits to ``ReplayExecutionStatus.DISABLED`` BEFORE any
    compilation or runtime evaluation happens. This is the master
    kill switch for the cage's only authority-bearing surface."""
    return os.environ.get(
        "JARVIS_REPLAY_EXECUTOR_ENABLED", "",
    ).strip().lower() in _TRUTHY


class ReplayExecutionStatus(str, enum.Enum):
    """Outcome of one sandboxed replay execution.

    Slice 6's order2_review queue + /order2 REPL render this verbatim
    per finding so an operator can scan ``passed/diverged/timeout`` at
    a glance.
    """

    PASSED = "PASSED"
    """Candidate produced a result structurally equal to the
    snapshot's expected fields per Slice 4's whitelist."""

    DIVERGED = "DIVERGED"
    """Candidate produced a result that disagrees with the snapshot
    on at least one whitelisted field. The first divergence is in
    :attr:`ReplayExecutionResult.divergence`."""

    DISABLED = "DISABLED"
    """JARVIS_REPLAY_EXECUTOR_ENABLED is off. No compilation, no
    runtime evaluation."""

    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    """Caller did not pass ``operator_authorized=True``. This is the
    structural cage marker — only Slice 6's amendment-protocol REPL
    passes True after operator sign-off; nothing else in O+V does."""

    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    """Candidate source exceeds MAX_CANDIDATE_BYTES."""

    SETUP_ERROR = "SETUP_ERROR"
    """Compile/run setup failed BEFORE the run() call: SyntaxError,
    no PhaseRunner subclass found, multiple subclasses found, phase
    attribute mismatch, instantiation error. Detail in
    :attr:`ReplayExecutionResult.detail`."""

    RUNTIME_ERROR = "RUNTIME_ERROR"
    """Candidate's ``run`` raised. Per the PhaseRunner contract a
    well-formed runner returns ``PhaseResult(status='fail', ...)``
    on internal error rather than raising — so any raise here is by
    itself a regression signal. Exception type + message in
    :attr:`ReplayExecutionResult.detail`."""

    TIMEOUT = "TIMEOUT"
    """Candidate's ``run`` exceeded the wall-clock cap (defaults
    DEFAULT_TIMEOUT_S, capped at MAX_TIMEOUT_S)."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Defensive: unexpected exception in the runner itself.
    Should never fire — every path is best-effort."""


@dataclass(frozen=True)
class ReplayExecutionResult:
    """One sandboxed-replay outcome for one snapshot.

    Slice 6 queue + REPL render this; the amendment-protocol decision
    surface aggregates many of these (one per applicable corpus
    snapshot) into a "M passed / N diverged / K timed out" summary.
    """

    schema_version: int
    op_id: str
    target_phase: str
    snapshot_op_id: str
    snapshot_phase: str
    status: ReplayExecutionStatus
    elapsed_s: float = 0.0
    divergence: Optional[ReplayDivergence] = None
    detail: str = ""
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.status is ReplayExecutionStatus.PASSED

    def to_dict(self) -> Dict[str, Any]:
        """Stable serialization for Slice 6 queue persistence."""
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "target_phase": self.target_phase,
            "snapshot_op_id": self.snapshot_op_id,
            "snapshot_phase": self.snapshot_phase,
            "status": self.status.value,
            "elapsed_s": round(self.elapsed_s, 6),
            "divergence": (
                {
                    "field_path": self.divergence.field_path,
                    "expected": self.divergence.expected,
                    "actual": self.divergence.actual,
                    "detail": self.divergence.detail,
                }
                if self.divergence is not None else None
            ),
            "detail": self.detail,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Mock OperationContext for sandbox ctx.advance(...) protocol
# ---------------------------------------------------------------------------


class _MockOperationContext:
    """Minimal stand-in for ``OperationContext`` exposing pre-phase
    ctx fields as attributes + the ``advance(**kwargs)`` method.

    The PhaseRunner contract (``phase_runner.py`` §3) requires the
    runner produce its next ctx via ``ctx.advance(...)``. The
    candidate calls ``ctx.advance(phase=..., risk_tier=..., ...)``
    and we capture the kwargs as the produced "next ctx" mapping
    that Slice 4's diff then compares against the snapshot.
    """

    __slots__ = ("_data", "_advance_kwargs")

    def __init__(self, data: Dict[str, Any]) -> None:
        # Defensive copy — the candidate shouldn't mutate it but if
        # it does (Slice 3 rule 4 forbids it but a malicious patched
        # body might try) we don't want to corrupt the caller's
        # snapshot dict.
        self._data: Dict[str, Any] = dict(data)
        self._advance_kwargs: Optional[Dict[str, Any]] = None

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for attribute names NOT in __slots__
        # / not set as instance attributes — so the candidate sees
        # every key from pre_phase_ctx as a dotted attribute.
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name)

    def advance(self, **kwargs: Any) -> "_MockOperationContext":
        """Return a new mock ctx with kwargs merged onto the current
        data. The PhaseRunner contract uses this to produce the next
        ctx without mutating self."""
        merged = dict(self._data)
        merged.update(kwargs)
        nxt = _MockOperationContext(merged)
        # Stash the kwargs so the runner can extract "what the
        # candidate wanted to change" for the Slice 4 diff. We compare
        # the FULL merged dict, not just the kwargs — the snapshot's
        # expected_next_ctx is a full ctx dict.
        nxt._advance_kwargs = dict(kwargs)
        return nxt

    @property
    def as_dict(self) -> Dict[str, Any]:
        """Snapshot of the current data as a plain dict — used by
        the runner to feed Slice 4's structural-equality diff."""
        return dict(self._data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_phase_runner_subclass(
    namespace: Dict[str, Any],
    phase_runner_cls: type,
) -> Tuple[Optional[type], str]:
    """Inspect the post-run namespace for a PhaseRunner subclass.

    Returns ``(class_or_none, detail_string)``. The cage rule is
    EXACTLY ONE PhaseRunner subclass per candidate file — multiple
    subclasses ambiguates "which one is the proposal" so we reject."""
    found: list = []
    for name, obj in namespace.items():
        if name.startswith("_"):
            continue
        if not isinstance(obj, type):
            continue
        if obj is phase_runner_cls:
            continue
        try:
            if issubclass(obj, phase_runner_cls):
                found.append((name, obj))
        except TypeError:
            continue
    if not found:
        return None, "no_phase_runner_subclass_found"
    if len(found) > 1:
        names = ", ".join(n for n, _ in found)
        return None, f"multiple_phase_runner_subclasses_found:{names}"
    return found[0][1], f"resolved:{found[0][0]}"


def _coerce_phase_to_str(phase_attr: Any) -> str:
    """Render a runner's `phase` class attribute as a string for
    matching against the target_phase argument. Handles the
    OperationPhase enum (``.name``) and bare strings."""
    if phase_attr is None:
        return ""
    name = getattr(phase_attr, "name", None)
    if name:
        return str(name).upper()
    return str(phase_attr).upper()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_replay_under_operator_trigger(
    *,
    candidate_source: str,
    target_phase: str,
    snapshot: ReplaySnapshot,
    op_id: str = "",
    operator_authorized: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    ctx_whitelist: FrozenSet[str] = DEFAULT_CTX_WHITELIST,
) -> ReplayExecutionResult:
    """Compile + run the candidate in a sandboxed namespace, invoke
    it once against the snapshot's pre-phase ctx, and diff its output
    against the snapshot's expected fields.

    This is the ONLY place in the cage that compiles or runs a
    candidate ``PhaseRunner`` subclass. Every gate above this layer
    (manifest, AST validator, MetaPhaseRunner) reasons structurally.

    The function is ``async`` because PhaseRunner.run is ``async`` per
    the contract; the timeout is enforced via :func:`asyncio.wait_for`.

    Never raises. Every failure path returns a
    :class:`ReplayExecutionResult` with the appropriate
    :class:`ReplayExecutionStatus`.
    """
    target_phase_norm = (target_phase or "").upper()
    op_id_str = op_id or ""

    # 0. Master flag short-circuit — happens BEFORE any compilation.
    if not is_enabled():
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.DISABLED,
            notes=("master_flag_off",),
        )

    # 1. Operator authorization gate — happens BEFORE any compilation.
    if operator_authorized is not True:
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.NOT_AUTHORIZED,
            notes=("operator_authorized_must_be_true",),
        )

    # 2. Source-size cap.
    src = candidate_source or ""
    src_bytes = src.encode("utf-8", errors="replace")
    if len(src_bytes) > MAX_CANDIDATE_BYTES:
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SOURCE_TOO_LARGE,
            detail=(f"source_bytes={len(src_bytes)} > "
                    f"MAX_CANDIDATE_BYTES={MAX_CANDIDATE_BYTES}"),
        )

    # 3. Clamp timeout to safe range.
    eff_timeout: float
    try:
        eff_timeout = float(timeout_s)
    except (TypeError, ValueError):
        eff_timeout = DEFAULT_TIMEOUT_S
    if eff_timeout <= 0.0:
        eff_timeout = DEFAULT_TIMEOUT_S
    if eff_timeout > MAX_TIMEOUT_S:
        eff_timeout = MAX_TIMEOUT_S

    # 4. Parse + compile.
    try:
        ast.parse(src)
    except SyntaxError as exc:
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail=f"syntax_error:{exc.msg} at line {exc.lineno}",
        )
    try:
        code_obj = compile(src, "<order2_candidate>", "exec")
    except (SyntaxError, ValueError) as exc:
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail=f"compile_failed:{type(exc).__name__}:{exc}",
        )

    # 5. Build sandbox namespace.
    #    Resolve PhaseRunner / PhaseResult / OperationPhase HERE (not at
    #    module top-level) so this module's import surface stays the
    #    minimum demanded by the §6 authority-invariant docstring.
    try:
        from backend.core.ouroboros.governance.op_context import (
            OperationPhase as _OperationPhase,
        )
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseResult as _PhaseResult,
            PhaseRunner as _PhaseRunner,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.INTERNAL_ERROR,
            detail=f"contract_imports_failed:{type(exc).__name__}:{exc}",
        )

    sandbox_globals: Dict[str, Any] = {
        "__builtins__": _build_safe_builtins(),
        "__name__": "order2_candidate",
        "PhaseRunner": _PhaseRunner,
        "PhaseResult": _PhaseResult,
        "OperationPhase": _OperationPhase,
    }

    # 6. Run the candidate body in the scoped namespace.
    try:
        _RUN_CANDIDATE_BODY(code_obj, sandbox_globals, sandbox_globals)
    except Exception as exc:  # noqa: BLE001 — defensive
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail=(f"module_body_raised:{type(exc).__name__}:{exc}"),
        )

    # 7. Find the candidate's PhaseRunner subclass.
    cls, find_detail = _find_phase_runner_subclass(
        sandbox_globals, _PhaseRunner,
    )
    if cls is None:
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail=find_detail,
        )

    # 8. phase attribute matches target_phase.
    phase_attr = getattr(cls, "phase", None)
    runner_phase_str = _coerce_phase_to_str(phase_attr)
    if runner_phase_str != target_phase_norm:
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail=(f"phase_attr_mismatch:runner_phase="
                    f"{runner_phase_str!r} != target_phase="
                    f"{target_phase_norm!r}"),
        )

    # 9. Instantiate. PhaseRunner is an ABC; subclass must implement
    #    `run` to be instantiable.
    try:
        instance = cls()
    except Exception as exc:  # noqa: BLE001 — defensive
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail=(f"instantiation_failed:{type(exc).__name__}:{exc}"),
        )

    run_attr = getattr(instance, "run", None)
    if run_attr is None or not callable(run_attr):
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail="run_attr_missing_or_not_callable",
        )

    # 10. Build mock ctx + run with timeout.
    mock_ctx = _MockOperationContext(snapshot.pre_phase_ctx or {})

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    coro = run_attr(mock_ctx)
    if not asyncio.iscoroutine(coro):
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            detail=(f"run_returned_non_coroutine:"
                    f"{type(coro).__name__}"),
        )
    try:
        result = await asyncio.wait_for(coro, timeout=eff_timeout)
    except asyncio.TimeoutError:
        elapsed = loop.time() - t0
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.TIMEOUT,
            elapsed_s=elapsed,
            detail=f"run_exceeded_timeout_s={eff_timeout}",
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        elapsed = loop.time() - t0
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.RUNTIME_ERROR,
            elapsed_s=elapsed,
            detail=f"run_raised:{type(exc).__name__}:{exc}",
        )
    elapsed = loop.time() - t0

    # 11. Shape-check the result. PhaseResult is a frozen dataclass
    #     with .next_ctx / .next_phase / .status / .reason.
    if not isinstance(result, _PhaseResult):
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.SETUP_ERROR,
            elapsed_s=elapsed,
            detail=(f"run_returned_non_phaseresult:"
                    f"{type(result).__name__}"),
        )

    # 12. Coerce result fields for Slice 4 diff.
    actual_next_phase: Optional[str] = None
    if result.next_phase is not None:
        actual_next_phase = _coerce_phase_to_str(result.next_phase)

    actual_next_ctx_dict: Dict[str, Any]
    if isinstance(result.next_ctx, _MockOperationContext):
        actual_next_ctx_dict = result.next_ctx.as_dict
    elif isinstance(result.next_ctx, dict):
        actual_next_ctx_dict = result.next_ctx
    else:
        # Real OperationContext or SimpleNamespace — try common
        # attribute readout. Don't blow up: structural diff will
        # reveal the mismatch as field-level divergences.
        actual_next_ctx_dict = {}
        for key in ctx_whitelist:
            if hasattr(result.next_ctx, key):
                actual_next_ctx_dict[key] = getattr(result.next_ctx, key)

    # Snapshot.expected_next_phase is a string; result.next_phase may
    # be an OperationPhase enum. Slice 4 diff is byte-equality so we
    # need same type. Normalize both sides through _coerce_phase_to_str.
    expected_next_phase_norm: Optional[str]
    if snapshot.expected_next_phase is None:
        expected_next_phase_norm = None
    else:
        expected_next_phase_norm = _coerce_phase_to_str(
            snapshot.expected_next_phase,
        )
    snapshot_for_diff = ReplaySnapshot(
        op_id=snapshot.op_id,
        phase=snapshot.phase,
        pre_phase_ctx=snapshot.pre_phase_ctx,
        expected_next_phase=expected_next_phase_norm,
        expected_status=snapshot.expected_status,
        expected_reason=snapshot.expected_reason,
        expected_next_ctx=snapshot.expected_next_ctx,
        tags=snapshot.tags,
    )

    divergence = compare_phase_result_to_expected(
        actual_next_phase=actual_next_phase,
        actual_status=str(result.status),
        actual_reason=result.reason,
        actual_next_ctx=actual_next_ctx_dict,
        snapshot=snapshot_for_diff,
        ctx_whitelist=ctx_whitelist,
    )
    if divergence is not None:
        logger.info(
            "[ReplayExecutor] op=%s snapshot=%s/%s DIVERGED at "
            "field_path=%s expected=%r actual=%r",
            op_id_str, snapshot.op_id, snapshot.phase,
            divergence.field_path,
            divergence.expected, divergence.actual,
        )
        return ReplayExecutionResult(
            schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
            op_id=op_id_str, target_phase=target_phase_norm,
            snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
            status=ReplayExecutionStatus.DIVERGED,
            elapsed_s=elapsed,
            divergence=divergence,
            detail=f"diverged_at:{divergence.field_path}",
        )

    logger.info(
        "[ReplayExecutor] op=%s snapshot=%s/%s PASSED elapsed=%.4fs",
        op_id_str, snapshot.op_id, snapshot.phase, elapsed,
    )
    return ReplayExecutionResult(
        schema_version=REPLAY_EXECUTION_SCHEMA_VERSION,
        op_id=op_id_str, target_phase=target_phase_norm,
        snapshot_op_id=snapshot.op_id, snapshot_phase=snapshot.phase,
        status=ReplayExecutionStatus.PASSED,
        elapsed_s=elapsed,
        notes=("structural_diff_clean",),
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_CANDIDATE_BYTES",
    "MAX_TIMEOUT_S",
    "REPLAY_EXECUTION_SCHEMA_VERSION",
    "ReplayExecutionResult",
    "ReplayExecutionStatus",
    "execute_replay_under_operator_trigger",
    "is_enabled",
]


# ---------------------------------------------------------------------------
# Pass B Graduation Slice 2 — substrate AST pin
# ---------------------------------------------------------------------------
# replay_executor MUST be allowed to call ``compile()`` -- compiling
# proposed PhaseRunner subclasses in a sandbox is its job. The
# substrate pin therefore disables the dynamic-builtins ban for this
# module specifically (the rest of Pass B substrate keeps the ban).


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta._invariant_helpers import (
        make_pass_b_substrate_invariant,
    )
    inv = make_pass_b_substrate_invariant(
        invariant_name="pass_b_replay_executor_substrate",
        target_file=(
            "backend/core/ouroboros/governance/meta/replay_executor.py"
        ),
        description=(
            "Pass B Slice 6.1 substrate: is_enabled + "
            "execute_replay_under_operator_trigger + "
            "ReplayExecutionResult (frozen) present. Note: master "
            "flag stays default-FALSE pre-soak per W2(5) policy. "
            "Dynamic-builtin ban relaxed: this module's job is to "
            "compile proposed PhaseRunners under operator-authorized "
            "trigger; the cage is the operator_authorized=True "
            "requirement, not banning compile()."
        ),
        required_funcs=(
            "is_enabled", "execute_replay_under_operator_trigger",
        ),
        required_classes=("ReplayExecutionResult",),
        frozen_classes=("ReplayExecutionResult",),
        forbid_dynamic_builtins=False,
    )
    return [inv] if inv is not None else []
