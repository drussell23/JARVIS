"""Priority E — Shipped-code structural invariants.

Pass B's ``ast_phase_runner_validator`` validates *candidate* code
before it's allowed to ship. This module pins structural invariants
on **already-shipped** code — preventing future refactors from
silently regressing load-bearing wiring.

The seed invariant (Priority E completion per PRD §25.5.5) closes
the soak #3 silent-disable gap structurally:

  * Every ``return PhaseResult(...)`` in
    ``phase_runners/plan_runner.py`` MUST be preceded by a call to
    ``_capture_default_claims_at_plan_exit`` within the same
    containing block. Without this, a refactor that silently removes
    the helper call from one exit path would re-introduce the
    "Phase 2 is theatrical because PLAN-time claim capture is
    skipped" pattern that produced 120 empty postmortems in soak #3.

Design discipline (mirrors Pass B Slice 3 — different scope):
  * Pure ``ast.parse`` walk. Zero runtime introspection, zero
    network, zero subprocess. Deterministic for the same source
    bytes.
  * Hybrid AST + bytes window: AST locates Return-PhaseResult
    nodes (filtering docstring/comment false-positives), then
    a source-byte scan over the K-line window preceding each
    Return verifies the helper call.
  * Registry pattern (mirrors Slice A2 default_claims registry).
    Operators register additional invariants from their own
    modules; the seed set is amend-via-Pass-B governance (this
    module added to the Order-2 manifest by E2).
  * Master flag ``JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED`` —
    default ``true``. Hot-revert: ``export JARVIS_SHIPPED_CODE_-
    INVARIANTS_ENABLED=false`` returns ``validate_all`` to a
    pure no-op (returns ``()``).

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall.
  * Pure stdlib (``ast``, ``logging``, ``os``, ``pathlib``).
  * NEVER raises out of any public method — defensive everywhere.
  * Read-only over source files — never writes back.

Per PRD §25.5.5: "Future refactors that re-introduce the silent-
disable gap will fail this test." This module promotes the test-
time check shipped in Slice A3 to a runtime-callable structural
primitive.
"""
from __future__ import annotations

import ast
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


SHIPPED_CODE_INVARIANTS_SCHEMA_VERSION: str = "shipped_code_invariant.1"

# Default lookback window for the helper-call check (in source lines).
# 30 lines is enough to span the typical setup-then-return block in
# PLAN runner exit paths without false-matching across distant returns.
_DEFAULT_LOOKBACK_LINES: int = 30


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def shipped_code_invariants_enabled() -> bool:
    """``JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED`` (default ``true``).

    When off, ``validate_all`` returns an empty tuple — operators
    can disable structural enforcement at boot time without removing
    the registry. Hot-revert: a single env knob."""
    raw = os.environ.get(
        "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


# A validator function takes (parsed_ast_module, source_bytes) and
# returns a tuple of human-readable violation strings (empty tuple
# means "invariant holds"). NEVER raises — defensive contract.
ShippedCodeValidator = Callable[
    [ast.Module, str], Tuple[str, ...],
]


@dataclass(frozen=True)
class ShippedCodeInvariant:
    """One pin on shipped code's structural shape. Frozen + hashable."""

    invariant_name: str
    target_file: str  # repo-relative path
    description: str
    validate: ShippedCodeValidator
    schema_version: str = SHIPPED_CODE_INVARIANTS_SCHEMA_VERSION


@dataclass(frozen=True)
class InvariantViolation:
    """One violation report. Frozen for safe propagation across
    threads / serialization."""

    invariant_name: str
    target_file: str
    detail: str
    schema_version: str = SHIPPED_CODE_INVARIANTS_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invariant_name": self.invariant_name,
            "target_file": self.target_file,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, ShippedCodeInvariant] = {}
_REGISTRY_LOCK = threading.RLock()


def register_shipped_code_invariant(
    inv: ShippedCodeInvariant, *, overwrite: bool = False,
) -> None:
    """Install an invariant. NEVER raises. Idempotent on identical
    re-register; rejects different-callable re-register without
    overwrite=True."""
    if not isinstance(inv, ShippedCodeInvariant):
        return
    safe_name = (
        str(inv.invariant_name).strip() if inv.invariant_name else ""
    )
    if not safe_name:
        return
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(safe_name)
        if existing is not None:
            if existing == inv:
                return
            if not overwrite:
                logger.info(
                    "[ShippedCodeInvariants] %r already registered",
                    safe_name,
                )
                return
        _REGISTRY[safe_name] = inv


def unregister_shipped_code_invariant(invariant_name: str) -> bool:
    """Remove an invariant. Returns True if removed. NEVER raises."""
    safe_name = str(invariant_name).strip() if invariant_name else ""
    if not safe_name:
        return False
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(safe_name, None) is not None


def list_shipped_code_invariants() -> Tuple[ShippedCodeInvariant, ...]:
    """Return all registered invariants in stable alphabetical order."""
    with _REGISTRY_LOCK:
        return tuple(_REGISTRY[k] for k in sorted(_REGISTRY.keys()))


def reset_registry_for_tests() -> None:
    """Test isolation."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _register_seed_invariants()


# ---------------------------------------------------------------------------
# AST helpers — shared across validators
# ---------------------------------------------------------------------------


def _is_phase_result_call(node: ast.AST) -> bool:
    """True iff ``node`` is ``ast.Call`` whose func is a Name
    ``PhaseResult``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "PhaseResult":
        return True
    return False


def _is_return_phase_result(stmt: ast.AST) -> bool:
    """True iff ``stmt`` is ``return PhaseResult(...)``."""
    return (
        isinstance(stmt, ast.Return)
        and stmt.value is not None
        and _is_phase_result_call(stmt.value)
    )


def _bytes_window_above(
    source: str, target_line: int, *, lookback: int,
) -> str:
    """Extract the source bytes window spanning ``lookback`` lines
    above ``target_line`` (1-indexed). Used to check for textual
    presence of a helper call without re-walking the AST per check."""
    lines = source.splitlines()
    start = max(0, target_line - 1 - lookback)
    end = max(0, target_line - 1)  # exclude the target line itself
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Seed invariant — PLAN runner default-claim wiring (Priority E)
# ---------------------------------------------------------------------------


def _validate_plan_runner_default_claims(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Every ``return PhaseResult(...)`` in plan_runner.py must be
    preceded by a call to ``_capture_default_claims_at_plan_exit``
    within the prior ``_DEFAULT_LOOKBACK_LINES`` source lines.

    AST locates real Return-PhaseResult nodes (filters docstrings/
    comments containing the substring); bytes-scan over the source
    window preceding each Return verifies the helper call.

    Returns a tuple of violation descriptions; empty tuple means
    the invariant holds. NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if not _is_return_phase_result(node):
            continue
        # ast.Return has .lineno on Py 3.8+; defensive check anyway.
        lineno = getattr(node, "lineno", None)
        if not isinstance(lineno, int) or lineno < 1:
            continue
        window = _bytes_window_above(
            source, lineno, lookback=_DEFAULT_LOOKBACK_LINES,
        )
        if "_capture_default_claims_at_plan_exit(" not in window:
            # Diagnostic: include the line number so operators can
            # pinpoint the offending exit.
            violations.append(
                f"line {lineno}: return PhaseResult without preceding "
                f"_capture_default_claims_at_plan_exit call within "
                f"{_DEFAULT_LOOKBACK_LINES} lines"
            )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Seed invariant — Cost Contract (PRD §26.6.1, post-Phase-12 reinforcement)
# ---------------------------------------------------------------------------
#
# Pins the `BG never cascades to Claude unless is_read_only` contract
# at the AST level. Two pins compose:
#
#   1. SPEC route MUST NOT call self._call_fallback — SPEC never
#      cascades regardless of is_read_only (no Nervous System Reflex
#      exception for SPEC; only BG has the read-only escape hatch).
#
#   2. BG route MAY call self._call_fallback ONLY in code paths
#      gated by an `is_read_only` predicate above the call within
#      `_generate_background` (Manifesto §5 Nervous System Reflex).
#      Any unguarded fallback call inside _generate_background is
#      a contract violation.
#
# These match the actual code contract (post-soak-#7) — not a
# simplified version. The simplified "BG never goes to Claude"
# from PRD §26.6 was an intentional simplification; the real
# contract has the read-only escape hatch documented in
# memory/project_bg_spec_sealed.md.


def _enclosing_function_node(
    tree: ast.Module, lineno: int,
) -> Optional[ast.AST]:
    """Return the (Async)FunctionDef AST node containing ``lineno``,
    or ``None`` if the line is at module scope. NEVER raises."""
    best_node: Optional[ast.AST] = None
    best_start: int = -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if start <= lineno <= end and start > best_start:
            best_node = node
            best_start = start
    return best_node


def _is_call_fallback_invocation(node: ast.AST) -> bool:
    """True iff ``node`` is ``ast.Await`` wrapping
    ``self._call_fallback(...)`` OR a direct ``self._call_fallback(...)``
    call. Both shapes occur in practice — the AsyncAwait wrapper around
    the call is the common one."""
    # Unwrap Await
    if isinstance(node, ast.Await):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # Pattern: self._call_fallback(...)
    if isinstance(func, ast.Attribute) and func.attr == "_call_fallback":
        if isinstance(func.value, ast.Name) and func.value.id == "self":
            return True
    return False


def _function_references_symbol(
    fn_node: ast.AST, symbol: str,
) -> bool:
    """True iff ``symbol`` appears as a structural reference in
    ``fn_node`` — Name, Attribute attr, or string Constant. AST-only
    (comments are stripped by ast.parse, so comment-only mentions do
    NOT count — which is the contract we want: the symbol must be a
    real code reference, not a docstring or commentary).

    Catches all idiomatic threading patterns:
      * Bare Name:                ``is_read_only`` → ast.Name(id=...)
      * Attribute:                ``ctx.is_read_only`` → ast.Attribute(attr=...)
      * Underscore-prefixed alias: ``_is_read_only = ...`` → catches via
        Name match below if the alias contains the symbol substring;
        explicit pattern for ``_<symbol>`` aliasing also recognized.
      * String constant arg:      ``getattr(ctx, "is_read_only", ...)``
        → ast.Constant(value=str)

    NEVER raises."""
    try:
        for sub in ast.walk(fn_node):
            # Bare Name node — direct variable reference
            if isinstance(sub, ast.Name):
                # Exact match OR the symbol embedded in an alias
                # (e.g., `_is_read_only` aliases `is_read_only` in
                # idiomatic Python — common pattern).
                if sub.id == symbol or sub.id.endswith("_" + symbol):
                    return True
                if sub.id == "_" + symbol:
                    return True
            # Attribute access — `ctx.is_read_only`
            if isinstance(sub, ast.Attribute) and sub.attr == symbol:
                return True
            # String constant — `getattr(ctx, "is_read_only", False)`
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if sub.value == symbol:
                    return True
        return False
    except Exception:  # noqa: BLE001 — defensive
        return False


def _is_inside_if_block(
    fn_node: ast.AST, target_lineno: int,
) -> bool:
    """True iff the line at ``target_lineno`` is contained inside
    an ``ast.If`` block within ``fn_node``. Proves the call is
    conditional, not unconditional. NEVER raises.

    Walks the AST and checks whether any If-statement's body or
    orelse spans the target line."""
    try:
        for sub in ast.walk(fn_node):
            if not isinstance(sub, ast.If):
                continue
            for branch in (sub.body, sub.orelse):
                for stmt in branch:
                    start = getattr(stmt, "lineno", None)
                    end = getattr(stmt, "end_lineno", None)
                    if not isinstance(start, int) or not isinstance(end, int):
                        continue
                    if start <= target_lineno <= end:
                        return True
        return False
    except Exception:  # noqa: BLE001 — defensive
        return False


def _validate_cost_contract_bg_spec(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Cost contract structural pin (PRD §26.6.1).

    Three composing checks (matches the actual code contract per
    project_bg_spec_sealed.md, not the simplified PRD version):

      1. ``_generate_speculative`` MUST NOT contain any
         ``self._call_fallback`` invocation — SPEC never cascades
         to Claude under any condition.

      2. ``_generate_background`` body MUST reference the symbol
         ``is_read_only`` somewhere (proves the Nervous System
         Reflex hatch wiring, Manifesto §5).

      3. Every ``self._call_fallback`` invocation inside
         ``_generate_background`` MUST be contained within an
         ``ast.If`` block (proves the call is conditional, not
         unconditional). Combined with check 2, this structurally
         pins that BG cascades are gated.

    Returns tuple of violations; empty tuple means the contract holds.
    NEVER raises."""
    violations: List[str] = []

    # Pre-walk: locate the BG / SPEC function nodes once
    bg_node: Optional[ast.AST] = None
    spec_node: Optional[ast.AST] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_generate_background":
                bg_node = node
            elif node.name == "_generate_speculative":
                spec_node = node

    # Check 2: BG function references is_read_only
    if bg_node is not None and not _function_references_symbol(
        bg_node, "is_read_only",
    ):
        violations.append(
            f"_generate_background (line {getattr(bg_node, 'lineno', '?')}) "
            f"does not reference is_read_only — Nervous System Reflex "
            f"hatch is not wired (cost contract per "
            f"project_bg_spec_sealed.md + PRD §26.6)"
        )

    # Checks 1 + 3: walk each _call_fallback invocation
    for node in ast.walk(tree):
        if not _is_call_fallback_invocation(node):
            continue
        lineno = getattr(node, "lineno", None)
        if not isinstance(lineno, int) or lineno < 1:
            continue

        # Check 1: SPEC has zero tolerance
        if spec_node is not None:
            spec_start = getattr(spec_node, "lineno", -1)
            spec_end = getattr(spec_node, "end_lineno", -1)
            if (
                isinstance(spec_start, int) and isinstance(spec_end, int)
                and spec_start <= lineno <= spec_end
            ):
                violations.append(
                    f"line {lineno}: self._call_fallback invocation inside "
                    f"_generate_speculative — SPEC route MUST NEVER cascade "
                    f"to Claude under any condition (cost contract per "
                    f"project_bg_spec_sealed.md + PRD §26.6)"
                )
                continue

        # Check 3: BG calls must be inside an If
        if bg_node is not None:
            bg_start = getattr(bg_node, "lineno", -1)
            bg_end = getattr(bg_node, "end_lineno", -1)
            if (
                isinstance(bg_start, int) and isinstance(bg_end, int)
                and bg_start <= lineno <= bg_end
            ):
                if not _is_inside_if_block(bg_node, lineno):
                    violations.append(
                        f"line {lineno}: self._call_fallback invocation "
                        f"inside _generate_background but NOT contained "
                        f"in an if-block — cascade is unconditional, "
                        f"violating Nervous System Reflex gating "
                        f"(cost contract per project_bg_spec_sealed.md "
                        f"+ PRD §26.6)"
                    )

    return tuple(violations)


# ---------------------------------------------------------------------------
# Priority 1 Slice 5 — confidence-aware execution structural pins
# ---------------------------------------------------------------------------


def _validate_confidence_capture_authority(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 1 capture primitive structural pin: must NOT import any
    forbidden authority module (orchestrator/policy/iron_gate/
    providers/etc). The capture path is structurally read-only on
    stream events — no provider imports means no path can mutate
    the stream / response.

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    forbidden_substrings = (
        "orchestrator",
        "phase_runners",
        "candidate_generator",
        "iron_gate",
        "change_engine",
        "policy",
        "semantic_guardian",
        "semantic_firewall",
        "providers",
        "doubleword_provider",
    )
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in forbidden_substrings:
                    if fb in alias.name:
                        violations.append(
                            f"forbidden import: {alias.name}",
                        )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for fb in forbidden_substrings:
                if fb in mod:
                    violations.append(f"forbidden import: {mod}")
    return tuple(violations)


def _validate_confidence_monitor_pure_data(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 2 monitor structural pin: must NOT do I/O — no file
    reads/writes, no network, no subprocess. Pure-data evaluator
    means the monitor cannot become a control-flow surface for
    confidence-driven side effects.

    Bytes-level scan for forbidden module imports (open / requests /
    urllib / socket / subprocess) + AST scan for ``open(`` calls
    outside the documented threading import path. NEVER raises."""
    forbidden_module_substrings = (
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
    )
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in forbidden_module_substrings:
                    if fb == alias.name.split(".")[0]:
                        violations.append(
                            f"forbidden I/O module import: {alias.name}",
                        )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".")[0]
            for fb in forbidden_module_substrings:
                if fb == root:
                    violations.append(
                        f"forbidden I/O module import: {mod}",
                    )
        # Block bare open() calls (file I/O)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: bare open() call detected — "
                    f"monitor must be I/O-free"
                )
    return tuple(violations)


def _validate_confidence_probe_consumer(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 3 probe-consumer structural pin: hypothesis_consumers.py
    MUST contain the ``ConfidenceCollapseAction`` enum with all three
    canonical actions (RETRY_WITH_FEEDBACK / ESCALATE_TO_OPERATOR /
    INCONCLUSIVE) AND the ``probe_confidence_collapse`` async
    function. Future refactors that drop any of these break the
    Slice 3 contract.

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    violations: List[str] = []
    if "class ConfidenceCollapseAction" not in source:
        violations.append(
            "ConfidenceCollapseAction enum class missing — "
            "Slice 3 contract broken"
        )
    for action in (
        "RETRY_WITH_FEEDBACK",
        "ESCALATE_TO_OPERATOR",
        "INCONCLUSIVE",
    ):
        if action not in source:
            violations.append(
                f"ConfidenceCollapseAction.{action} member missing"
            )
    if "async def probe_confidence_collapse" not in source:
        violations.append(
            "probe_confidence_collapse async consumer missing"
        )
    return tuple(violations)


def _validate_confidence_route_advisor_cost_guard(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 4 route-advisor structural pin: ``_propose_route_change``
    MUST contain a ``raise CostContractViolation(...)`` statement.
    This is the AST-pinned guard preventing any future refactor
    from silently dropping the BG/SPEC → higher-cost escalation
    check.

    Walks the AST to find the function definition and verifies a
    ``raise CostContractViolation(...)`` Call exists in its body.
    Bytes-fallback also checks for the token signature.
    NEVER raises."""
    violations: List[str] = []

    # Must reference the cost-contract symbols
    if "CostContractViolation" not in source:
        violations.append(
            "CostContractViolation reference missing"
        )
    if "COST_GATED_ROUTES" not in source:
        violations.append("COST_GATED_ROUTES reference missing")

    # AST pin: find _propose_route_change and verify a
    # `raise CostContractViolation(...)` exists in its body
    found_function = False
    found_guard = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_propose_route_change"
        ):
            found_function = True
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise):
                    exc = sub.exc
                    if isinstance(exc, ast.Call) and isinstance(
                        exc.func, ast.Name,
                    ):
                        if exc.func.id == "CostContractViolation":
                            found_guard = True
                            break
            break
    if not found_function:
        violations.append(
            "_propose_route_change function missing"
        )
    elif not found_guard:
        violations.append(
            "_propose_route_change body missing "
            "`raise CostContractViolation(...)` guard"
        )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Priority 2 Slice 6 — Causality DAG structural pins
# ---------------------------------------------------------------------------


def _validate_causality_dag_no_authority_imports(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 3 ``causality_dag.py`` structural pin: must NOT import
    any forbidden authority module. Pure-data graph builder; reads
    the JSONL ledger via stdlib only.

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    forbidden_substrings = (
        "orchestrator",
        "phase_runners",
        "candidate_generator",
        "iron_gate",
        "change_engine",
        "policy",
        "semantic_guardian",
        "semantic_firewall",
        "providers",
        "doubleword_provider",
        "urgency_router",
    )
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in forbidden_substrings:
                    if fb in alias.name:
                        violations.append(
                            f"forbidden import: {alias.name}",
                        )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for fb in forbidden_substrings:
                if fb in mod:
                    violations.append(f"forbidden import: {mod}")
    return tuple(violations)


def _validate_causality_dag_bounded_traversal(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 3 ``subgraph()`` structural pin: the function MUST
    accept a ``max_depth`` parameter so traversal is bounded.
    Without this bound, a DAG with cycles or pathological depth
    could OOM or hang the navigation surface.

    AST-walks ``causality_dag.py`` for the ``subgraph`` function
    definition and verifies it has a ``max_depth`` parameter.
    Bytes-fallback also confirms the parameter token is present in
    source so a refactor that renames it to ``depth`` would be
    caught.

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    violations: List[str] = []

    if "max_depth" not in source:
        violations.append(
            "max_depth parameter token missing from "
            "causality_dag source"
        )

    found_function = False
    found_max_depth_arg = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "subgraph"
        ):
            found_function = True
            for arg in (
                list(node.args.args)
                + list(node.args.kwonlyargs)
            ):
                if arg.arg == "max_depth":
                    found_max_depth_arg = True
                    break
            break
    if not found_function:
        violations.append("subgraph function missing")
    elif not found_max_depth_arg:
        violations.append(
            "subgraph function missing max_depth parameter "
            "(bounded traversal contract broken)"
        )
    return tuple(violations)


def _validate_dag_navigation_no_ctx_mutation(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 4 ``dag_navigation.py`` structural pin: MUST NOT call
    any mutation method on a ctx-shaped object. The navigation
    surface is read-only — no ``ctx.advance(...)`` /
    ``ctx.with_*(...)`` / ``ctx.with_strategic_*(...)`` etc.
    AST-walks function calls and rejects any ``ctx.advance``,
    ``ctx.with_*``, or ``ctx.replace(...)`` invocation.

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    forbidden_method_prefixes = ("with_", "advance", "replace")
    violations: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        # ctx.method(...) pattern
        if not isinstance(func.value, ast.Name):
            continue
        if func.value.id != "ctx":
            continue
        method_name = func.attr
        for prefix in forbidden_method_prefixes:
            if (
                method_name == prefix
                or method_name.startswith(prefix)
            ):
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: forbidden ctx.{method_name}() "
                    f"call — dag_navigation must be read-only on ctx"
                )
                break
    return tuple(violations)


def _validate_dag_replay_cost_contract_preserved(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 5 + Slice 6 structural pin: the ``--rerun-from`` replay
    path in ``scripts/ouroboros_battle_test.py`` MUST go through the
    existing orchestrator entry point — it cannot bypass the §26.6
    four-layer cost contract by introducing a new dispatch path.

    Bytes-level check: source MUST reference both the replay
    helper functions (``prepare_replay_from_record`` +
    ``apply_replay_from_record_env``) AND the existing ``--rerun``
    flag — proving the replay flow piggybacks on the orchestrator's
    existing dispatch. Source MUST NOT contain a direct call to
    ``ClaudeProvider`` or any provider construction (which would
    indicate a shortcut bypass).

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    violations: List[str] = []

    if "prepare_replay_from_record" not in source:
        violations.append(
            "prepare_replay_from_record reference missing — "
            "--rerun-from is not wired through the replay primitive"
        )
    if "apply_replay_from_record_env" not in source:
        violations.append(
            "apply_replay_from_record_env reference missing — "
            "--rerun-from is not wired through the env-overlay path"
        )
    # The fork must require --rerun (so it goes through the existing
    # orchestrator-dispatched replay, not a new code path).
    if "--rerun-from" in source and "args.rerun is None" not in source:
        violations.append(
            "--rerun-from path may bypass --rerun's orchestrator "
            "dispatch — required guard `if args.rerun is None` is "
            "missing"
        )
    # Direct provider construction / dispatch in the replay path
    # would be a §26.6 cost-contract bypass.
    forbidden_provider_tokens = (
        "ClaudeProvider(",
        "DoublewordProvider(",
        "from backend.core.ouroboros.governance.providers import",
        "from backend.core.ouroboros.governance.doubleword_provider import",
    )
    for token in forbidden_provider_tokens:
        if token in source:
            violations.append(
                f"direct provider reference detected: {token!r} — "
                f"replay must go through orchestrator entry point, "
                f"not bypass dispatch"
            )
    return tuple(violations)


def _validate_adaptation_ledger_monotonic_tightening(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Pass C ``adaptation/ledger.py`` LOAD-BEARING pin: the ledger
    MUST contain the monotonic-tightening verdict enum + validator
    function. Without these, surface miners could propose looser
    safety properties (regression vector). Bytes-pinned so a rename
    or accidental removal is caught.

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    violations: List[str] = []
    required_tokens = (
        "MonotonicTighteningVerdict",
        "validate_monotonic_tightening",
        "REJECTED_WOULD_LOOSEN",
    )
    for tok in required_tokens:
        if tok not in source:
            violations.append(
                f"monotonic-tightening token missing: {tok}"
            )
    return tuple(violations)


_ADAPTATION_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
)


def _validate_adaptation_miners_no_authority_imports(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Pass C surface-miner structural pin: miners are read-only
    proposal generators. They MUST NOT import any authority module.
    Their write-surface is the ledger (proposal records); apply
    happens via /adapt approve gated by operator approval.

    Note: the SemanticGuardian miner intentionally references the
    *string* token 'semantic_guardian' in its proposal kind metadata —
    that is a data label, not an import. This validator only flags
    actual ``Import`` / ``ImportFrom`` AST nodes.

    Returns tuple of violations; empty tuple means pin holds. NEVER
    raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in _ADAPTATION_FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in alias.name:
                        violations.append(
                            f"forbidden import: {alias.name}"
                        )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for fb in _ADAPTATION_FORBIDDEN_AUTHORITY_SUBSTRINGS:
                if fb in mod:
                    violations.append(f"forbidden import: {mod}")
    return tuple(violations)


def _validate_providers_dispatch_assertion(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Cost contract runtime-assertion presence pin (PRD §26.6.2).

    Verifies that ``providers.py`` calls
    ``assert_provider_route_compatible`` (or imports it from
    ``cost_contract_assertion``) — pinning that Layer 2's runtime
    assertion is structurally wired into ClaudeProvider's generate
    entry point.

    Soft pin: the assertion may be invoked from a helper or directly;
    the structural requirement is that the symbol appears in
    providers.py source (i.e., wiring exists). Slice-2's tests verify
    the wiring is *correct* (right place, right args); this invariant
    just verifies the wiring is *present*.

    Returns tuple of violations; empty tuple means contract holds.
    NEVER raises."""
    if "assert_provider_route_compatible" not in source:
        return (
            "providers.py does not reference "
            "assert_provider_route_compatible — Layer 2 cost contract "
            "runtime assertion is missing from the dispatch boundary "
            "(see cost_contract_assertion.py + PRD §26.6.2)",
        )
    return ()


# ---------------------------------------------------------------------------
# Move 4 Slice 5 — InvariantDriftAuditor structural pins
# ---------------------------------------------------------------------------


def _validate_invariant_drift_bridge_uses_propose_action(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """The InvariantDriftAutoActionBridge MUST consume
    ``_propose_action`` from auto_action_router so the §26.6 cost-
    contract structural guard is inherited.

    Two-part check:
      1. Source MUST reference the symbol ``_propose_action`` —
         pinned by bytes scan so a renamed import is caught.
      2. Source MUST NOT directly construct ``AdvisoryAction(...)``
         in any code position (only the imported reference is OK,
         which is an ast.Name, not an ast.Call).

    The combined pin: bridge uses the helper, doesn't bypass it.

    Returns tuple of violation strings; empty tuple means pin holds.
    NEVER raises."""
    violations: List[str] = []
    # Part 1 — must reference _propose_action.
    if "_propose_action" not in source:
        violations.append(
            "_propose_action reference missing — bridge must "
            "consume the auto_action_router cost-contract helper"
        )
    # Part 2 — must NOT have ``AdvisoryAction(...)`` Call nodes
    # anywhere in the bridge module. Importing the type is fine
    # (ast.Name); only ast.Call where func is the AdvisoryAction
    # name counts as direct construction.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "AdvisoryAction":
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: direct AdvisoryAction(...) "
                f"construction — bridge MUST consume "
                f"_propose_action so the §26.6 cost-contract "
                f"structural guard is inherited"
            )
    return tuple(violations)


def _validate_confidence_probe_no_mutation_tools(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Move 5 Slice 5 — bridge module MUST NOT reference mutation
    tool names in code. AST-walk Name + Attribute nodes; docstring
    strings allowed (they describe what's forbidden).

    NEVER raises. Returns tuple of violations; empty = pin holds."""
    forbidden = (
        "edit_file", "write_file", "delete_file",
        "run_tests", "bash",
    )
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in forbidden:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: forbidden mutation tool "
                    f"name {node.id!r} as Name reference"
                )
        elif isinstance(node, ast.Attribute):
            if node.attr in forbidden:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: forbidden mutation tool "
                    f"name {node.attr!r} as Attribute reference"
                )
    return tuple(violations)


_EXPECTED_READONLY_TOOLS: Tuple[str, ...] = (
    "git_blame", "git_diff", "git_log",
    "glob_files", "get_callers",
    "list_dir", "list_symbols",
    "read_file", "search_code",
)


def _validate_readonly_evidence_prober_allowlist(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Move 5 Slice 5 — READONLY_TOOL_ALLOWLIST MUST be a
    frozenset module-level constant containing only the canonical
    9 read-only tools. AST-walks Assign nodes for the constant +
    bytes-pin verifies each tool name string literal is present.

    NEVER raises."""
    violations: List[str] = []
    # Bytes-pin: every expected tool must appear as a literal in
    # the source (in the frozenset construction)
    for tool in _EXPECTED_READONLY_TOOLS:
        if f'"{tool}"' not in source and f"'{tool}'" not in source:
            violations.append(
                f"expected read-only tool {tool!r} missing from "
                f"READONLY_TOOL_ALLOWLIST literal"
            )
    # Module-level constant exists?
    if "READONLY_TOOL_ALLOWLIST" not in source:
        violations.append(
            "READONLY_TOOL_ALLOWLIST constant missing — "
            "move 5 read-only allowlist contract broken"
        )
    if "frozenset(" not in source:
        violations.append(
            "READONLY_TOOL_ALLOWLIST must be a frozenset (immutable)"
        )
    # Mutation-tool defense in depth: forbidden names must NOT
    # appear in the source at all (constant or otherwise)
    forbidden_mutations = (
        "edit_file", "write_file", "delete_file",
        "run_tests",
    )
    for forbid in forbidden_mutations:
        if (
            f'"{forbid}"' in source
            or f"'{forbid}'" in source
        ):
            violations.append(
                f"forbidden mutation tool {forbid!r} appears as "
                f"string literal in prober module — must not be "
                f"in allowlist or referenced anywhere"
            )
    return tuple(violations)


def _validate_confidence_probe_cap_structure(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Move 5 Slice 5 — env-knob helpers (max_questions,
    max_tool_rounds_per_question) MUST use ``min(ceiling, max(floor,
    value))`` clamps in source. Catches refactor that loosens caps.

    Bytes-pinned for the clamp pattern + presence of cap-helper
    functions. NEVER raises."""
    violations: List[str] = []
    required_helpers = (
        "def max_questions",
        "def convergence_quorum",
        "def max_tool_rounds_per_question",
    )
    for helper in required_helpers:
        if helper not in source:
            violations.append(
                f"cap helper missing: {helper}"
            )
    # The clamp pattern must appear at least once (catches refactor
    # that drops min/max compose)
    if "min(" not in source or "max(" not in source:
        violations.append(
            "cap helpers must use min()/max() clamp pattern"
        )
    # Bytes-pin the floor/ceiling constants exist
    required_constants = (
        "_MAX_QUESTIONS_FLOOR",
        "_MAX_QUESTIONS_CEILING",
        "_CONVERGENCE_QUORUM_FLOOR",
        "_MAX_TOOL_ROUNDS_FLOOR",
        "_MAX_TOOL_ROUNDS_CEILING",
    )
    for const in required_constants:
        if const not in source:
            violations.append(
                f"cap-structure constant missing: {const}"
            )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Move 6 — Generative Quorum AST pins (4 invariants)
# ---------------------------------------------------------------------------


def _validate_generative_quorum_no_authority_imports(
    tree: ast.Module, source: str,  # noqa: ARG001 — interface
) -> Tuple[str, ...]:
    """Move 6 Slice 5 — Quorum primitive + runner + gate must NOT
    import orchestrator-tier modules. Pure structural primitives.

    NEVER raises."""
    forbidden = (
        "orchestrator", "iron_gate", "policy", "change_engine",
        "candidate_generator", "providers", "doubleword_provider",
        "urgency_router", "auto_action_router",
        "subagent_scheduler", "tool_executor", "phase_runners",
        "semantic_guardian", "semantic_firewall", "risk_engine",
    )
    violations: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        module = (
            node.module if isinstance(node, ast.ImportFrom)
            else (node.names[0].name if node.names else "")
        )
        module = module or ""
        for f in forbidden:
            if f in module:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: forbidden authority import "
                    f"contains {f!r}: {module}"
                )
    return tuple(violations)


def _validate_ast_canonical_pure_stdlib(
    tree: ast.Module, source: str,  # noqa: ARG001 — interface
) -> Tuple[str, ...]:
    """Move 6 Slice 5 — ast_canonical signature module must be
    stdlib-only (no governance imports). Critical because the
    signature compute is a load-bearing trust boundary: any
    governance dep widens the attack surface.

    Also AST-pins the no-exec/eval/compile contract — signature
    compute MUST never execute candidate code.

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (
                "backend." in module
                or "governance" in module
            ):
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: ast_canonical must be "
                    f"stdlib-only — found {module!r}"
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("exec", "eval", "compile"):
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: ast_canonical MUST NOT "
                        f"execute candidate code — found "
                        f"{node.func.id}() call"
                    )
    return tuple(violations)


def _validate_quorum_gate_consumes_cost_gated_routes(
    tree: ast.Module, source: str,  # noqa: ARG001 — interface
) -> Tuple[str, ...]:
    """Move 6 Slice 5 — STRUCTURAL §26.6 cost-contract guard. The
    gate MUST reference ``COST_GATED_ROUTES`` symbol from
    ``cost_contract_assertion``. Catches a refactor that drops
    the cost guard structurally BEFORE shipping.

    Bytes-pinned for the symbol name + import-from line; AST-
    pinned for the importfrom node. NEVER raises."""
    violations: List[str] = []
    if "COST_GATED_ROUTES" not in source:
        violations.append(
            "gate dropped its reference to "
            "COST_GATED_ROUTES — the structural §26.6 cost-"
            "contract guard is gone"
        )
    found_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (
                node.module
                == "backend.core.ouroboros.governance."
                "cost_contract_assertion"
            ):
                for alias in node.names:
                    if alias.name == "COST_GATED_ROUTES":
                        found_import = True
                        break
    if not found_import:
        violations.append(
            "gate must import COST_GATED_ROUTES via importfrom "
            "from cost_contract_assertion (single source of "
            "truth)"
        )
    return tuple(violations)


def _validate_quorum_cap_structure_pinned(
    tree: ast.Module, source: str,  # noqa: ARG001 — interface
) -> Tuple[str, ...]:
    """Move 6 Slice 5 — Quorum K + agreement-threshold env knobs
    MUST use ``min(ceiling, max(floor, value))`` clamps. Catches
    refactor that loosens caps (e.g., letting K=10 through).

    Bytes-pinned for floor/ceiling constants + the clamp pattern.
    NEVER raises."""
    violations: List[str] = []
    required_constants = (
        "_K_FLOOR",
        "_K_CEILING",
        "_AGREEMENT_THRESHOLD_FLOOR",
    )
    for const in required_constants:
        if const not in source:
            violations.append(
                f"cap-structure constant missing: {const}"
            )
    required_helpers = (
        "def quorum_k",
        "def agreement_threshold",
    )
    for helper in required_helpers:
        if helper not in source:
            violations.append(
                f"cap helper missing: {helper}"
            )
    if "min(" not in source or "max(" not in source:
        violations.append(
            "cap helpers must use min()/max() clamp pattern"
        )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Priority #5 — Continuous Invariant Gradient Watcher AST pins
# (4 invariants)
# ---------------------------------------------------------------------------


def _validate_gradient_watcher_pure_stdlib(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 1 ``gradient_watcher`` primitive MUST be pure-stdlib —
    strongest authority invariant. CIGW is observational not
    prescriptive; the primitive must NEVER reach into governance
    modules. Zero governance imports, zero exec/eval/compile, zero
    async (Slice 2's collector wraps via ``asyncio.to_thread``).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "backend." in module or "governance" in module:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: gradient_watcher primitive must "
                    f"be pure-stdlib — found {module!r}"
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("exec", "eval", "compile"):
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: gradient_watcher MUST NOT "
                        f"execute candidate code — found "
                        f"{node.func.id}() call"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 1 primitive must remain sync "
                f"— found async function {node.name!r}"
            )
    return tuple(violations)


# Cost-contract banned imports — CIGW collector + comparator + observer
# all forbid orchestrator-tier coupling so the cost contract is
# preserved by AST-pinned construction (no path through these modules
# can invoke a generation provider — the only structural cost is
# stdlib ast.parse + file.read on source files).
_CIGW_BANNED_IMPORT_SUBSTRINGS: Tuple[str, ...] = (
    ".providers", "doubleword_provider", "urgency_router",
    "candidate_generator", "orchestrator", "tool_executor",
    "phase_runner", "iron_gate", "change_engine",
    "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
)


def _validate_gradient_collector_cost_contract(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """STRUCTURAL §26.6 cost-contract preservation: Slice 2
    collector MUST NOT import any orchestrator-tier module. MUST
    reuse Slice 1 primitives (InvariantSample + MeasurementKind)
    + stdlib ``ast`` for structural-metric extraction. MUST define
    ``COST_CONTRACT_PRESERVED_BY_CONSTRUCTION``.

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for banned in _CIGW_BANNED_IMPORT_SUBSTRINGS:
                    if banned in alias.name:
                        lineno = getattr(node, "lineno", "?")
                        violations.append(
                            f"line {lineno}: CIGW collector MUST NOT "
                            f"import {alias.name!r} — cost contract "
                            f"violation"
                        )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for banned in _CIGW_BANNED_IMPORT_SUBSTRINGS:
                if banned in module:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: CIGW collector MUST NOT "
                        f"import from {module!r} — cost contract "
                        f"violation"
                    )

    required_symbols = (
        ("InvariantSample", "Slice 1 schema reuse"),
        ("MeasurementKind", "Slice 1 closed-taxonomy reuse"),
        ("ast", "stdlib AST extraction"),
        ("COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
         "structural marker constant"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"CIGW collector dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


def _validate_gradient_comparator_authority(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 3 comparator MUST be PURE-DATA aggregator: no
    orchestrator-tier imports, no async (Slice 4 wraps via
    to_thread), no exec/eval/compile. MUST reuse Slice 1 closed-
    taxonomy enums. MUST resolve PASSED via
    ``adaptation.ledger.MonotonicTighteningVerdict`` for Phase C
    cross-stack vocabulary integration (6th module after Move 6 +
    Priority #1/#2/#3/#4).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for banned in _CIGW_BANNED_IMPORT_SUBSTRINGS:
                if banned in module:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: CIGW comparator MUST NOT "
                        f"import from {module!r} — authority "
                        f"violation"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 3 comparator must remain "
                f"sync — found async function {node.name!r}"
            )
        if isinstance(node, ast.Call) and isinstance(
            node.func, ast.Name,
        ):
            if node.func.id in ("exec", "eval", "compile"):
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: CIGW comparator MUST NOT "
                    f"execute candidate code — found "
                    f"{node.func.id}() call"
                )

    required_symbols = (
        ("GradientReport", "Slice 1 schema reuse"),
        ("GradientOutcome", "Slice 1 closed-taxonomy reuse"),
        ("MonotonicTighteningVerdict",
         "Phase C cross-stack vocabulary"),
        ("adaptation.ledger",
         "Phase C cage rule integration"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"CIGW comparator dropped {symbol!r} — "
                f"{reason} gone"
            )
    return tuple(violations)


def _validate_gradient_observer_uses_flock(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 4 observer MUST use Tier 1 #3 cross-process flock for
    the JSONL ring buffer (zero-duplication contract — same
    discipline as InvariantDriftStore + Coherence + PostmortemRecall
    + Priority #3/#4 observers). MUST reuse Slice 3's
    ``compare_gradient_history`` + ``stamp_gradient_report``. MUST
    reuse the ``ide_observability_stream`` broker AND the 2 new
    CIGW event-type constants registered in Slice 4.

    NEVER raises."""
    violations: List[str] = []
    required_symbols = (
        ("flock_append_line", "Tier 1 #3 cross-process safety"),
        ("flock_critical_section", "Tier 1 #3 ring-buffer safety"),
        ("cross_process_jsonl", "Tier 1 #3 module reuse"),
        ("compare_gradient_history", "Slice 3 aggregator reuse"),
        ("stamp_gradient_report", "Slice 3 stamp reuse"),
        ("ide_observability_stream", "Gap #6 broker reuse"),
        ("EVENT_TYPE_CIGW_REPORT_RECORDED",
         "per-report SSE event vocabulary"),
        ("EVENT_TYPE_CIGW_BASELINE_UPDATED",
         "per-aggregation SSE event vocabulary"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"CIGW observer dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Priority #4 — Speculative Branch Tree AST pins (4 invariants)
# ---------------------------------------------------------------------------


def _validate_speculative_branch_pure_stdlib(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 1 ``speculative_branch`` primitive MUST be pure-stdlib —
    strongest authority invariant. SBT is observational not
    prescriptive; the primitive must NEVER reach into governance
    modules. Zero governance imports, zero exec/eval/compile, zero
    async (Slice 2's runner wraps via ``asyncio.gather``).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "backend." in module or "governance" in module:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: speculative_branch primitive "
                    f"must be pure-stdlib — found {module!r}"
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("exec", "eval", "compile"):
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: speculative_branch MUST NOT "
                        f"execute candidate code — found "
                        f"{node.func.id}() call"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 1 primitive must remain sync "
                f"— found async function {node.name!r}"
            )
    return tuple(violations)


# Cost-contract banned imports — SBT runner + comparator + observer
# all forbid orchestrator-tier coupling so the cost contract is
# preserved by AST-pinned construction (no path through these modules
# can invoke a generation provider — the only LLM costs are inside
# the per-branch tool execution path, bounded structurally by
# max_depth × max_breadth).
_SBT_BANNED_IMPORT_SUBSTRINGS: Tuple[str, ...] = (
    ".providers", "doubleword_provider", "urgency_router",
    "candidate_generator", "orchestrator", "tool_executor",
    "phase_runner", "iron_gate", "change_engine",
    "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
)


def _validate_speculative_branch_runner_cost_contract(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 2 runner MUST preserve the §26.6 cost contract by
    construction: only LLM costs are inside the per-branch tool
    execution path (bounded by max_depth × max_breadth × per-tool
    budget). Pinned via AST-level absence of every orchestrator-tier
    import + presence of canonical reuse contracts:

      * Slice 1 primitives (compute_tree_verdict + compute_tree_outcome
        — convergence logic delegated, not duplicated)
      * Move 5's READONLY_TOOL_ALLOWLIST + is_tool_allowlisted from
        readonly_evidence_prober (defense-in-depth tool filtering;
        no re-implementation of the 9-tool frozenset)

    The COST_CONTRACT_PRESERVED_BY_CONSTRUCTION constant must be
    defined (structural marker for operators).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for banned in _SBT_BANNED_IMPORT_SUBSTRINGS:
                    if banned in alias.name:
                        lineno = getattr(node, "lineno", "?")
                        violations.append(
                            f"line {lineno}: SBT runner MUST NOT "
                            f"import {alias.name!r} — cost contract "
                            f"violation"
                        )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for banned in _SBT_BANNED_IMPORT_SUBSTRINGS:
                if banned in module:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: SBT runner MUST NOT "
                        f"import from {module!r} — cost contract "
                        f"violation"
                    )

    # Positive reuse contracts.
    required_symbols = (
        ("compute_tree_verdict", "Slice 1 convergence reuse"),
        ("compute_tree_outcome", "Slice 1 outcome reuse"),
        ("READONLY_TOOL_ALLOWLIST", "Move 5 frozenset reuse"),
        ("is_tool_allowlisted", "Move 5 helper reuse"),
        ("readonly_evidence_prober", "Move 5 module reuse"),
        ("COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
         "structural marker constant"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"SBT runner dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


def _validate_speculative_branch_comparator_authority(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 3 comparator MUST be PURE-DATA aggregator: no
    orchestrator-tier imports, no async, no exec/eval/compile.
    MUST reuse Slice 1's closed-taxonomy enums (TreeVerdict +
    TreeVerdictResult). MUST resolve PASSED via
    ``adaptation.ledger.MonotonicTighteningVerdict`` (Phase C
    cross-stack vocabulary integration — 5th module after Move 6 +
    Priority #1/#2/#3).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for banned in _SBT_BANNED_IMPORT_SUBSTRINGS:
                if banned in module:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: SBT comparator MUST NOT "
                        f"import from {module!r} — authority violation"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 3 comparator must remain sync "
                f"— found async function {node.name!r}"
            )
        if isinstance(node, ast.Call) and isinstance(
            node.func, ast.Name,
        ):
            if node.func.id in ("exec", "eval", "compile"):
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: SBT comparator MUST NOT execute "
                    f"candidate code — found {node.func.id}() call"
                )

    required_symbols = (
        ("TreeVerdict", "Slice 1 closed-taxonomy reuse"),
        ("TreeVerdictResult", "Slice 1 schema reuse"),
        ("MonotonicTighteningVerdict",
         "Phase C cross-stack vocabulary"),
        ("adaptation.ledger",
         "Phase C cage rule integration"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"SBT comparator dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


def _validate_speculative_branch_observer_uses_flock(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 4 observer MUST use Tier 1 #3 cross-process flock for
    the JSONL ring buffer (zero-duplication contract — same discipline
    as InvariantDriftStore + Coherence + PostmortemRecall + Priority
    #3 observer). MUST reuse Slice 3's ``compare_tree_history`` +
    ``stamp_tree_verdict``. MUST reuse the
    ``ide_observability_stream`` broker (Gap #6 reuse) AND the 2 new
    SBT event-type constants registered in Slice 4.

    NEVER raises."""
    violations: List[str] = []
    required_symbols = (
        ("flock_append_line", "Tier 1 #3 cross-process safety"),
        ("flock_critical_section", "Tier 1 #3 ring-buffer safety"),
        ("cross_process_jsonl", "Tier 1 #3 module reuse"),
        ("compare_tree_history", "Slice 3 aggregator reuse"),
        ("stamp_tree_verdict", "Slice 3 stamp reuse"),
        ("ide_observability_stream", "Gap #6 broker reuse"),
        ("EVENT_TYPE_SBT_TREE_COMPLETE",
         "per-tree SSE event vocabulary"),
        ("EVENT_TYPE_SBT_BASELINE_UPDATED",
         "per-aggregation SSE event vocabulary"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"SBT observer dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Priority #3 — Counterfactual Replay AST pins (4 invariants)
# ---------------------------------------------------------------------------


#: Module-owned registration contract — function names that the
#: dynamic discovery loops in ``flag_registry_seed`` and
#: ``shipped_code_invariants`` invoke at BOOT time. Imports inside
#: these functions are STRUCTURALLY exempt from hot-path pure-stdlib
#: pins because the contract guarantees they fire only during
#: registration, never on the hot path. The contract is enforced by
#: shared function names (architectural invariant), not by allowlists.
MODULE_REGISTRATION_CONTRACT_FUNCS: frozenset = frozenset({
    "register_flags",
    "register_shipped_invariants",
})


def _registration_contract_line_ranges(
    tree: ast.Module,
) -> Tuple[Tuple[int, int], ...]:
    """Return (start, end) line ranges of every function body that
    matches the module-owned registration contract. Imports inside
    these ranges are exempt from hot-path purity pins because they
    only fire at boot via the dynamic discovery loops."""
    ranges: List[Tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name in MODULE_REGISTRATION_CONTRACT_FUNCS:
                start = getattr(node, "lineno", 0)
                end = getattr(node, "end_lineno", start) or start
                ranges.append((start, end))
    return tuple(ranges)


def _validate_counterfactual_replay_pure_stdlib(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 1 ``counterfactual_replay`` primitive MUST be pure-stdlib
    on the HOT PATH — strongest authority invariant. Replay is
    observational not prescriptive; the primitive must NEVER reach
    into governance modules during operation. Zero governance imports,
    zero exec/eval/compile, zero async (Slice 2's engine wraps via
    ``asyncio.to_thread``). Boot-time module-owned registration
    (``register_flags`` / ``register_shipped_invariants``) is
    structurally exempt — those imports only fire from the discovery
    loops, never on the hot replay path. NEVER raises."""
    violations: List[str] = []
    exempt_ranges = _registration_contract_line_ranges(tree)

    def _in_exempt_range(lineno: int) -> bool:
        return any(s <= lineno <= e for s, e in exempt_ranges)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "backend." in module or "governance" in module:
                lineno = getattr(node, "lineno", 0)
                if _in_exempt_range(lineno):
                    continue
                violations.append(
                    f"line {lineno}: counterfactual_replay primitive "
                    f"must be pure-stdlib — found {module!r}"
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("exec", "eval", "compile"):
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: counterfactual_replay MUST "
                        f"NOT execute candidate code — found "
                        f"{node.func.id}() call"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 1 primitive must remain sync "
                f"— found async function {node.name!r}"
            )
    return tuple(violations)


# Cost-contract banned imports — replay engine + comparator + observer
# all forbid orchestrator-tier coupling so the cost contract is
# preserved by AST-pinned construction (no path through these modules
# can invoke a generation provider).
_REPLAY_BANNED_IMPORT_SUBSTRINGS: Tuple[str, ...] = (
    ".providers", "doubleword_provider", "urgency_router",
    "candidate_generator", "orchestrator", "tool_executor",
    "phase_runner", "iron_gate", "change_engine",
    "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
)


def _validate_counterfactual_replay_engine_cost_contract(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 2 engine MUST preserve the §26.6 cost contract by
    construction: zero LLM cost on the replay path. Pinned via
    AST-level absence of every orchestrator-tier import + presence
    of the canonical reuse contracts (causality_dag.build_dag,
    last_session_summary, decision_runtime.DecisionRecord).

    The COST_CONTRACT_PRESERVED_BY_CONSTRUCTION constant must be
    defined (structural marker for operators).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for banned in _REPLAY_BANNED_IMPORT_SUBSTRINGS:
                    if banned in alias.name:
                        lineno = getattr(node, "lineno", "?")
                        violations.append(
                            f"line {lineno}: replay engine MUST NOT "
                            f"import {alias.name!r} — cost contract "
                            f"violation"
                        )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for banned in _REPLAY_BANNED_IMPORT_SUBSTRINGS:
                if banned in module:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: replay engine MUST NOT "
                        f"import from {module!r} — cost contract "
                        f"violation"
                    )

    # Positive reuse contracts — engine must reuse existing infra,
    # not duplicate it.
    required_symbols = (
        ("causality_dag", "Priority 2 Slice 3 reuse"),
        ("last_session_summary", "Phase 1 reuse"),
        ("DecisionRecord", "Phase 1 Slice 1.2 reuse"),
        ("COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
         "structural marker constant"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"replay engine dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


def _validate_counterfactual_replay_comparator_authority(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """Slice 3 comparator MUST be PURE-DATA aggregator: no
    orchestrator-tier imports, no async, no exec/eval/compile.
    MUST reuse Slice 1's closed-taxonomy enums (BranchVerdict,
    ReplayOutcome, ReplayVerdict). MUST resolve PASSED via
    ``adaptation.ledger.MonotonicTighteningVerdict`` (Phase C
    cross-stack vocabulary integration).

    NEVER raises."""
    violations: List[str] = []
    # Banned authority imports.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for banned in _REPLAY_BANNED_IMPORT_SUBSTRINGS:
                if banned in module:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: comparator MUST NOT import "
                        f"from {module!r} — authority violation"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 3 comparator must remain "
                f"sync — found async function {node.name!r}"
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("exec", "eval", "compile"):
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: comparator MUST NOT execute "
                    f"candidate code — found {node.func.id}() call"
                )

    # Positive reuse contracts.
    required_symbols = (
        ("BranchVerdict", "Slice 1 closed-taxonomy reuse"),
        ("ReplayOutcome", "Slice 1 closed-taxonomy reuse"),
        ("ReplayVerdict", "Slice 1 schema reuse"),
        ("MonotonicTighteningVerdict",
         "Phase C cross-stack vocabulary"),
        ("adaptation.ledger",
         "Phase C cage rule integration"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"comparator dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


def _validate_counterfactual_replay_observer_uses_flock(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 4 observer MUST use Tier 1 #3 cross-process flock for
    the JSONL ring buffer (zero-duplication contract — same
    discipline as InvariantDriftStore + Coherence window store +
    PostmortemRecall index). MUST reuse Slice 3's
    ``compare_replay_history`` + ``stamp_verdict`` (no
    re-aggregation, no re-stamping). MUST reuse the
    ``ide_observability_stream`` broker (Gap #6 reuse) AND the
    2 new event-type constants registered in Slice 4.

    NEVER raises."""
    violations: List[str] = []
    required_symbols = (
        ("flock_append_line", "Tier 1 #3 cross-process safety"),
        ("flock_critical_section", "Tier 1 #3 ring-buffer safety"),
        ("cross_process_jsonl", "Tier 1 #3 module reuse"),
        ("compare_replay_history", "Slice 3 aggregator reuse"),
        ("stamp_verdict", "Slice 3 stamp reuse"),
        ("ide_observability_stream", "Gap #6 broker reuse"),
        ("EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE",
         "per-verdict SSE event vocabulary"),
        ("EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED",
         "per-aggregation SSE event vocabulary"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"observer dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Priority #2 — PostmortemRecall AST pins (4 invariants)
# ---------------------------------------------------------------------------


def _validate_postmortem_recall_pure_stdlib(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 1 PostmortemRecord primitive MUST be pure-stdlib —
    strongest authority invariant. Zero governance imports.
    No exec/eval/compile (canonical safety pin). No async
    (Slices 1-4 are sync; Slice 5 wraps via to_thread).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "backend." in module or "governance" in module:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: postmortem_recall must be "
                    f"pure-stdlib — found {module!r}"
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("exec", "eval", "compile"):
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: postmortem_recall MUST "
                        f"NOT execute candidate code — found "
                        f"{node.func.id}() call"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 1 primitive must remain "
                f"sync — found async function {node.name!r}"
            )
    return tuple(violations)


def _validate_postmortem_recall_index_uses_flock(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """STRUCTURAL cross-process safety pin + zero-duplication
    via reuse contract. Slice 2 index store MUST reference:
      * ``flock_append_line`` + ``flock_critical_section``
        (Tier 1 #3 cross-process safety)
      * ``_sanitize_field`` (load-bearing safety helper reuse
        from last_session_summary)
      * ``_parse_summary`` (canonical summary.json parser reuse)

    NEVER raises."""
    violations: List[str] = []
    required_symbols = (
        ("flock_append_line", "audit log cross-process safety"),
        ("flock_critical_section", "ring-buffer cross-process safety"),
        ("_sanitize_field", "zero-duplication safety helper reuse"),
        ("_parse_summary", "canonical summary.json parser reuse"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"index store dropped {symbol!r} reference — "
                f"{reason} guard is gone"
            )
    return tuple(violations)


def _validate_postmortem_recall_injector_authority_free(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 3 CONTEXT_EXPANSION injector MUST NOT import
    orchestrator-tier modules. READ-ONLY contract over the
    index. MUST reference ``_sanitize_field`` (canonical safety
    helper reuse) + ``recall_postmortems`` (Slice 1 reuse) +
    ``read_index`` (Slice 2 reuse).

    NEVER raises."""
    forbidden = (
        "orchestrator", "iron_gate", "policy", "change_engine",
        "candidate_generator", "providers",
        "doubleword_provider", "urgency_router",
        "auto_action_router", "subagent_scheduler",
        "tool_executor", "phase_runners", "semantic_guardian",
        "semantic_firewall", "risk_engine",
    )
    violations: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        module = (
            node.module if isinstance(node, ast.ImportFrom)
            else (node.names[0].name if node.names else "")
        )
        module = module or ""
        for f in forbidden:
            if f in module:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: forbidden authority "
                    f"import {f!r}: {module}"
                )
    required_symbols = (
        ("_sanitize_field", "canonical safety helper reuse"),
        ("recall_postmortems", "Slice 1 reuse"),
        ("read_index", "Slice 2 reuse"),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"injector dropped {symbol!r} — {reason} gone"
            )
    return tuple(violations)


def _validate_postmortem_recall_consumer_uses_adaptation_ledger(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """STRUCTURAL Phase C universal-cage-rule integration pin.
    Slice 4 consumer MUST import
    ``MonotonicTighteningVerdict`` from ``adaptation.ledger``
    AND reference the symbol + ``read_coherence_advisories``
    (canonical reader reuse from Priority #1 Slice 4) +
    ``INJECT_POSTMORTEM_RECALL_HINT`` (filter target — catches
    refactor that drops the action filter).

    NEVER raises."""
    violations: List[str] = []
    required_symbols = (
        (
            "MonotonicTighteningVerdict",
            "Phase C cage rule integration",
        ),
        (
            "read_coherence_advisories",
            "Priority #1 canonical reader reuse",
        ),
        (
            "INJECT_POSTMORTEM_RECALL_HINT",
            "filter target",
        ),
    )
    for symbol, reason in required_symbols:
        if symbol not in source:
            violations.append(
                f"consumer dropped {symbol!r} — {reason} gone"
            )
    # Verify importfrom shape for MonotonicTighteningVerdict
    found_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (
                node.module
                == "backend.core.ouroboros.governance"
                ".adaptation.ledger"
            ):
                for alias in node.names:
                    if alias.name == "MonotonicTighteningVerdict":
                        found_import = True
                        break
    if not found_import:
        violations.append(
            "consumer must import MonotonicTighteningVerdict "
            "via importfrom from adaptation.ledger"
        )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Priority #1 — Coherence Auditor AST pins (4 invariants)
# ---------------------------------------------------------------------------


def _validate_coherence_auditor_pure_stdlib(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 1 primitive MUST be pure-stdlib. NO governance
    imports of any kind — strongest authority invariant. Any
    ``backend.*`` or ``governance`` import is a violation.

    Also AST-pinned no-exec/eval/compile (mirrors Move 6 Slice 2's
    critical safety pin — auditor compares fingerprints, never
    executes shipped code) and no async (Slice 3 introduces
    async; Slice 1 stays sync).

    NEVER raises."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "backend." in module or "governance" in module:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: coherence_auditor must be "
                    f"pure-stdlib — found {module!r}"
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("exec", "eval", "compile"):
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"line {lineno}: coherence_auditor MUST "
                        f"NOT execute candidate code — found "
                        f"{node.func.id}() call"
                    )
        if isinstance(node, ast.AsyncFunctionDef):
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: Slice 1 primitive must remain "
                f"sync — found async function {node.name!r}"
            )
    return tuple(violations)


def _validate_coherence_observer_no_authority(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """Slice 3 observer MUST NOT import orchestrator-tier modules.
    Allowed governance imports: Slice 1 (coherence_auditor),
    Slice 2 (coherence_window_store), posture_observer (read-
    only), posture_health (Tier 1 #2 lazy), and
    ide_observability_stream (lazy SSE). NEVER raises."""
    forbidden = (
        "orchestrator", "iron_gate", "policy", "change_engine",
        "candidate_generator", "providers",
        "doubleword_provider", "urgency_router",
        "auto_action_router", "subagent_scheduler",
        "tool_executor", "phase_runners", "semantic_guardian",
        "semantic_firewall", "risk_engine",
    )
    allowed_governance = {
        (
            "backend.core.ouroboros.governance.verification."
            "coherence_auditor"
        ),
        (
            "backend.core.ouroboros.governance.verification."
            "coherence_window_store"
        ),
        "backend.core.ouroboros.governance.posture_observer",
        "backend.core.ouroboros.governance.posture_health",
        (
            "backend.core.ouroboros.governance."
            "ide_observability_stream"
        ),
    }
    violations: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        module = (
            node.module if isinstance(node, ast.ImportFrom)
            else (node.names[0].name if node.names else "")
        )
        module = module or ""
        for f in forbidden:
            if f in module:
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: forbidden authority "
                    f"import contains {f!r}: {module}"
                )
        if isinstance(node, ast.ImportFrom):
            if (
                module
                and "governance" in module
                and module not in allowed_governance
            ):
                lineno = getattr(node, "lineno", "?")
                violations.append(
                    f"line {lineno}: governance import outside "
                    f"observer allowlist: {module}"
                )
    return tuple(violations)


def _validate_coherence_window_store_uses_flock(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """STRUCTURAL cross-process safety pin. Slice 2 store MUST
    reference both ``flock_append_line`` (audit log) AND
    ``flock_critical_section`` (signature ring buffer) from
    ``cross_process_jsonl``. Catches a refactor that drops cross-
    process safety on either persistence path. NEVER raises."""
    violations: List[str] = []
    if "flock_append_line" not in source:
        violations.append(
            "store dropped flock_append_line reference — audit "
            "log cross-process safety guard is gone"
        )
    if "flock_critical_section" not in source:
        violations.append(
            "store dropped flock_critical_section reference — "
            "signature ring buffer cross-process safety guard is "
            "gone"
        )
    # Verify the importfrom shape exists too
    found_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (
                node.module
                == "backend.core.ouroboros.governance."
                "cross_process_jsonl"
            ):
                found_import = True
                break
    if not found_import:
        violations.append(
            "store must import from cross_process_jsonl via "
            "importfrom"
        )
    return tuple(violations)


def _validate_coherence_action_bridge_uses_adaptation_ledger(
    tree: ast.Module, source: str,  # noqa: ARG001
) -> Tuple[str, ...]:
    """STRUCTURAL universal-cage-rule integration pin. Slice 4
    bridge MUST import ``MonotonicTighteningVerdict`` from
    ``adaptation.ledger`` AND reference the symbol in code.
    Catches a refactor that drops the Phase C monotonic-
    tightening vocabulary integration. NEVER raises."""
    violations: List[str] = []
    if "MonotonicTighteningVerdict" not in source:
        violations.append(
            "bridge dropped MonotonicTighteningVerdict — Phase "
            "C universal cage rule integration is gone"
        )
    found_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (
                node.module
                == "backend.core.ouroboros.governance."
                "adaptation.ledger"
            ):
                for alias in node.names:
                    if alias.name == "MonotonicTighteningVerdict":
                        found_import = True
                        break
    if not found_import:
        violations.append(
            "bridge must import MonotonicTighteningVerdict via "
            "importfrom from adaptation.ledger"
        )
    # Also verify flock_append_line for the advisory persistence
    if "flock_append_line" not in source:
        violations.append(
            "bridge dropped flock_append_line — advisory "
            "persistence cross-process safety is gone"
        )
    return tuple(violations)


def _validate_invariant_drift_auditor_no_disk_writes(
    tree: ast.Module, source: str,
) -> Tuple[str, ...]:
    """The InvariantDriftAuditor primitive (Slice 1) is read-only
    over live process state. Disk writes belong to the store
    module (Slice 2).

    Bytes-pinned for these tokens (any presence is a violation):
      ``.write_text(``, ``.write_bytes(``, ``os.replace(``,
      ``NamedTemporaryFile``.

    AST-pinned for ``open(...)`` calls (any presence is a
    violation).

    Returns tuple of violations; empty tuple means pin holds.
    NEVER raises."""
    violations: List[str] = []
    forbidden_tokens = (
        ".write_text(",
        ".write_bytes(",
        "os.replace(",
        "NamedTemporaryFile",
    )
    for tok in forbidden_tokens:
        if tok in source:
            violations.append(
                f"forbidden disk-write token: {tok!r}"
            )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"line {lineno}: bare open() call — "
                f"InvariantDriftAuditor primitive must remain "
                f"read-only; disk writes belong to "
                f"invariant_drift_store"
            )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Validation engine
# ---------------------------------------------------------------------------


def _resolve_target_path(target_file: str) -> Path:
    """Resolve a repo-relative path against the project root.

    Mirrors the pattern used by ``meta/order2_manifest.py`` —
    project root is the directory containing ``CLAUDE.md`` searched
    walking up from this module's location."""
    here = Path(__file__).resolve().parent
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return cur / target_file
        cur = cur.parent
    # Fall back to CWD-relative
    return Path(target_file)


def validate_invariant(
    inv: ShippedCodeInvariant,
) -> Tuple[InvariantViolation, ...]:
    """Run a single invariant. Returns tuple of violations.
    NEVER raises."""
    if not shipped_code_invariants_enabled():
        return ()
    try:
        path = _resolve_target_path(inv.target_file)
        if not path.exists():
            logger.debug(
                "[ShippedCodeInvariants] target missing: %s", path,
            )
            return ()
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
        details = inv.validate(tree, source)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ShippedCodeInvariants] validator %r raised: %s",
            inv.invariant_name, exc, exc_info=True,
        )
        return ()
    return tuple(
        InvariantViolation(
            invariant_name=inv.invariant_name,
            target_file=inv.target_file,
            detail=str(d),
        )
        for d in details
    )


def validate_all() -> Tuple[InvariantViolation, ...]:
    """Run every registered invariant. Returns the concatenated
    violation list across all pins. NEVER raises.

    Master-flag-gated: when off, returns ``()`` immediately."""
    if not shipped_code_invariants_enabled():
        return ()
    out: List[InvariantViolation] = []
    for inv in list_shipped_code_invariants():
        out.extend(validate_invariant(inv))
    return tuple(out)


def _validate_confidence_threshold_tightener(
    tree: ast.AST, source: str,
) -> Tuple[str, ...]:
    """Gap #2 Slice 2 surface validator wiring pin. Lives inline
    because ``adaptation/`` is NOT in the module-owned discovery
    walk."""
    violations: list = []
    required = (
        ("install_surface_validator",
         "module-level call to install_surface_validator() must "
         "remain (cage entry point)"),
        ("CONFIDENCE_MONITOR_THRESHOLDS",
         "surface enum value must be referenced"),
        ("compute_policy_diff",
         "Slice 1 substrate predicate must be referenced for "
         "predicate parity with the universal cage"),
        ("sha256:",
         "sha256 hash prefix check must remain (provenance)"),
        ("_TIGHTEN_INDICATOR",
         "tighten direction indicator helper must remain"),
        ("MonotonicTighteningVerdict",
         "verdict canonical-string parity must remain (via "
         "compute_policy_diff)"),
    )
    for symbol, reason in required:
        if symbol not in source:
            violations.append(
                f"confidence_threshold_tightener dropped "
                f"{symbol!r} — {reason}"
            )
    # Defense-in-depth: confirm install_surface_validator() is
    # called at MODULE level (not just defined). Walk top-level
    # statements.
    found_call = False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(
            node.value, ast.Call,
        ):
            func = node.value.func
            if isinstance(func, ast.Name) and (
                func.id == "install_surface_validator"
            ):
                found_call = True
                break
    if not found_call:
        violations.append(
            "install_surface_validator() must be called at "
            "module level (auto-registration on import)"
        )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Seed registration
# ---------------------------------------------------------------------------


def _register_seed_invariants() -> None:
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="plan_runner_default_claims_wiring",
            target_file=(
                "backend/core/ouroboros/governance/phase_runners/"
                "plan_runner.py"
            ),
            description=(
                "Every `return PhaseResult(...)` in PLAN runner must "
                "be preceded by a call to "
                "_capture_default_claims_at_plan_exit (Priority A "
                "wiring; without this Phase 2 is theatrical)."
            ),
            validate=_validate_plan_runner_default_claims,
        ),
    )
    # PRD §26.6.1 — cost contract structural pin (post-Phase-12).
    # Bulletproofs the BG/SPEC-never-cascades-to-Claude invariant
    # at the AST level (with the documented Nervous System Reflex
    # read-only escape hatch for BG only).
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="cost_contract_bg_spec_no_unguarded_cascade",
            target_file=(
                "backend/core/ouroboros/governance/candidate_generator.py"
            ),
            description=(
                "_generate_speculative MUST NOT call self._call_fallback "
                "(SPEC never cascades to Claude). _generate_background "
                "MAY call self._call_fallback only inside an is_read_only "
                "guard (Manifesto §5 Nervous System Reflex; cost contract "
                "per project_bg_spec_sealed.md + PRD §26.6)."
            ),
            validate=_validate_cost_contract_bg_spec,
        ),
    )
    # PRD §26.6.2 — runtime assertion wiring presence pin.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="providers_cost_contract_assertion_wired",
            target_file=(
                "backend/core/ouroboros/governance/providers.py"
            ),
            description=(
                "providers.py MUST reference "
                "assert_provider_route_compatible from "
                "cost_contract_assertion — pins Layer 2 runtime "
                "assertion wiring at the dispatch boundary "
                "(PRD §26.6.2)."
            ),
            validate=_validate_providers_dispatch_assertion,
        ),
    )
    # PRD §26.5.1 — Priority 1 Slice 5 graduation seeds.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="confidence_capture_no_authority_imports",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "confidence_capture.py"
            ),
            description=(
                "Slice 1 capture primitive must NOT import "
                "orchestrator / phase_runners / candidate_generator / "
                "iron_gate / change_engine / policy / "
                "semantic_guardian / semantic_firewall / providers / "
                "doubleword_provider — pure-data primitive, structural "
                "read-only on stream events."
            ),
            validate=_validate_confidence_capture_authority,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="confidence_monitor_pure_data_no_io",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "confidence_monitor.py"
            ),
            description=(
                "Slice 2 monitor must NOT do I/O — no subprocess / "
                "socket / urllib / requests / aiohttp imports, no "
                "bare open() calls. Pure-data evaluator means the "
                "monitor cannot become a control-flow surface for "
                "confidence-driven side effects."
            ),
            validate=_validate_confidence_monitor_pure_data,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="confidence_probe_consumer_contract",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "hypothesis_consumers.py"
            ),
            description=(
                "Slice 3 hypothesis_consumers.py MUST contain the "
                "ConfidenceCollapseAction enum (RETRY_WITH_FEEDBACK / "
                "ESCALATE_TO_OPERATOR / INCONCLUSIVE) AND the "
                "probe_confidence_collapse async consumer — pins "
                "the cognitive-cage contract."
            ),
            validate=_validate_confidence_probe_consumer,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="confidence_route_advisor_cost_contract_guard",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "confidence_route_advisor.py"
            ),
            description=(
                "Slice 4 _propose_route_change MUST contain "
                "`raise CostContractViolation(...)` in its body — "
                "structural guard preventing BG/SPEC → STANDARD/"
                "COMPLEX/IMMEDIATE escalation. AST-pinned so future "
                "refactors cannot silently drop the cost contract "
                "guard. Composes with §26.6 four-layer defense-in-"
                "depth."
            ),
            validate=_validate_confidence_route_advisor_cost_guard,
        ),
    )
    # PRD §26.5.2 — Priority 2 Slice 6 graduation seeds.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="causality_dag_no_authority_imports",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "causality_dag.py"
            ),
            description=(
                "Slice 3 graph builder must NOT import "
                "orchestrator / phase_runners / candidate_generator / "
                "iron_gate / change_engine / policy / "
                "semantic_guardian / providers / urgency_router — "
                "pure-data primitive, structural read-only over "
                "the ledger."
            ),
            validate=_validate_causality_dag_no_authority_imports,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="causality_dag_bounded_traversal",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "causality_dag.py"
            ),
            description=(
                "Slice 3 subgraph() MUST accept a max_depth "
                "parameter — bounded BFS traversal contract; without "
                "this bound a pathological DAG could OOM or hang the "
                "navigation surface. AST-pinned so future refactors "
                "cannot silently drop the bound."
            ),
            validate=_validate_causality_dag_bounded_traversal,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="dag_navigation_no_ctx_mutation",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "dag_navigation.py"
            ),
            description=(
                "Slice 4 navigation surface MUST NOT call ctx "
                "mutation methods (ctx.advance / ctx.with_* / "
                "ctx.replace) — read-only contract enforced at "
                "AST-walk time. Future patches cannot silently "
                "introduce a mutation surface via the DAG view."
            ),
            validate=_validate_dag_navigation_no_ctx_mutation,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="dag_replay_cost_contract_preserved",
            target_file=(
                "scripts/ouroboros_battle_test.py"
            ),
            description=(
                "Slice 5 --rerun-from MUST go through the existing "
                "orchestrator entry point (no shortcut bypass of the "
                "§26.6 four-layer cost contract). Bytes-pinned: the "
                "replay path references prepare_replay_from_record + "
                "apply_replay_from_record_env, requires --rerun for "
                "session identity, and contains zero direct provider "
                "construction tokens."
            ),
            validate=_validate_dag_replay_cost_contract_preserved,
        ),
    )

    # PRD §26.5.3 — Pass C (Move 1 graduation 2026-04-29) seeds.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="adaptation_ledger_monotonic_tightening_pin",
            target_file=(
                "backend/core/ouroboros/governance/adaptation/"
                "ledger.py"
            ),
            description=(
                "LOAD-BEARING — the AdaptationLedger MUST contain the "
                "MonotonicTighteningVerdict enum + "
                "validate_monotonic_tightening function + "
                "REJECTED_WOULD_LOOSEN sentinel. These are the safety "
                "spine of all 6 surface miners — without them, "
                "adaptive proposals could weaken existing safety "
                "properties (regression vector)."
            ),
            validate=_validate_adaptation_ledger_monotonic_tightening,
        ),
    )
    for _miner in (
        "semantic_guardian_miner",
        "exploration_floor_tightener",
        "per_order_mutation_budget",
        "risk_tier_extender",
        "category_weight_rebalancer",
        "meta_governor",
    ):
        register_shipped_code_invariant(
            ShippedCodeInvariant(
                invariant_name=(
                    f"adaptation_{_miner}_no_authority_imports"
                ),
                target_file=(
                    f"backend/core/ouroboros/governance/adaptation/"
                    f"{_miner}.py"
                ),
                description=(
                    f"Pass C surface module {_miner!r} is a read-only "
                    "proposal generator. It MUST NOT import any "
                    "authority module (orchestrator / phase_runners / "
                    "candidate_generator / iron_gate / change_engine "
                    "/ policy / semantic_firewall / providers / "
                    "doubleword_provider / urgency_router). Write-"
                    "surface is the ledger; apply happens via "
                    "operator-gated /adapt approve."
                ),
                validate=(
                    _validate_adaptation_miners_no_authority_imports
                ),
            ),
        )

    # Move 5 Slice 5 — Confidence-Aware Probe Loop pins.
    # Three structural pins protect the bounded-probe contract:
    # (1) bridge module never references mutation tool names in
    # code (only docstring mentions allowed);
    # (2) prober's READONLY_TOOL_ALLOWLIST contains only known
    # read-only tools (no mutation tools sneak in);
    # (3) cap structure uses min/max clamps in source so
    # refactors cannot silently loosen K=max_questions /
    # convergence_quorum / max_tool_rounds_per_question.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "confidence_probe_bridge_no_mutation_tools"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "confidence_probe_bridge.py"
            ),
            description=(
                "Move 5 Slice 1 bridge module MUST NOT reference "
                "mutation tool names (edit_file / write_file / "
                "delete_file / run_tests / bash) in code. AST-walk "
                "Name + Attribute nodes; docstring string literals "
                "allowed (they describe what's forbidden)."
            ),
            validate=(
                _validate_confidence_probe_no_mutation_tools
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "readonly_evidence_prober_allowlist_pinned"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "readonly_evidence_prober.py"
            ),
            description=(
                "Move 5 Slice 2 READONLY_TOOL_ALLOWLIST MUST be a "
                "frozenset constant at module scope containing "
                "only known read-only tools. AST-walks the Assign "
                "node, verifies it's a frozenset call with literal "
                "string args, all in the canonical 9-tool set "
                "{read_file, search_code, get_callers, glob_files, "
                "list_dir, list_symbols, git_blame, git_log, "
                "git_diff}. No mutation tools."
            ),
            validate=(
                _validate_readonly_evidence_prober_allowlist
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "confidence_probe_cap_structure_pinned"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "confidence_probe_bridge.py"
            ),
            description=(
                "Move 5 cap-structure pin: max_questions and "
                "max_tool_rounds_per_question MUST use "
                "`min(ceiling, max(floor, value))` clamps in "
                "source. Catches refactors that loosen caps "
                "below structural floor or exceed structural "
                "ceiling. Bytes-pinned for cap helper presence."
            ),
            validate=_validate_confidence_probe_cap_structure,
        ),
    )

    # Move 4 Slice 5 — InvariantDriftAuditor pin.
    # The bridge MUST consume `_propose_action` (not construct
    # AdvisoryAction directly) so the §26.6 cost-contract structural
    # guard is inherited. Pinned here so a future refactor that
    # bypasses the guard is caught at boot validation.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "invariant_drift_bridge_uses_propose_action"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "invariant_drift_auto_action_bridge.py"
            ),
            description=(
                "InvariantDriftAutoActionBridge MUST consume "
                "auto_action_router._propose_action so the §26.6 "
                "cost-contract structural guard is inherited. "
                "Direct AdvisoryAction(...) construction in the "
                "bridge would bypass the guard — this pin catches "
                "any future refactor that does so."
            ),
            validate=(
                _validate_invariant_drift_bridge_uses_propose_action
            ),
        ),
    )
    # The auditor module MUST stay disk-write-free — Slice 1's
    # disk-write contract (no open/.write_text/.write_bytes/
    # os.replace/NamedTemporaryFile in the auditor module). Disk
    # writes belong to the store module.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "invariant_drift_auditor_no_disk_writes"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "invariant_drift_auditor.py"
            ),
            description=(
                "The InvariantDriftAuditor primitive (Slice 1) is "
                "read-only over live process state. Disk writes "
                "belong to the store module (Slice 2). Pinned here "
                "to prevent future refactors from sneaking I/O into "
                "the pure-compute primitive."
            ),
            validate=_validate_invariant_drift_auditor_no_disk_writes,
        ),
    )
    # Move 6 Slice 5 — Generative Quorum graduation pins.
    # Closes §28.5.2 v9 brutal review's two undefended Antivenom
    # bypass vectors (Test-shape gaming + Quine-class hallucination)
    # via independent-roll consensus. These pins protect the
    # structural primitives from refactor drift.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "generative_quorum_no_authority_imports_primitive"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "generative_quorum.py"
            ),
            description=(
                "Slice 1 Quorum primitive must NOT import "
                "orchestrator / phase_runners / iron_gate / "
                "change_engine / policy / candidate_generator / "
                "providers / doubleword_provider / "
                "urgency_router / auto_action_router / "
                "subagent_scheduler / tool_executor / "
                "semantic_guardian / semantic_firewall / "
                "risk_engine — pure-data primitive."
            ),
            validate=_validate_generative_quorum_no_authority_imports,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "generative_quorum_runner_no_authority_imports"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "generative_quorum_runner.py"
            ),
            description=(
                "Slice 3 K-way parallel runner must NOT import "
                "orchestrator-tier modules — transport-agnostic "
                "primitive. Lazy ide_observability_stream import "
                "for SSE is allowed (load-bearing best-effort)."
            ),
            validate=_validate_generative_quorum_no_authority_imports,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="ast_canonical_pure_stdlib",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "ast_canonical.py"
            ),
            description=(
                "Slice 2 AST-normalized signature compute MUST be "
                "stdlib-only — no governance imports. Critical "
                "safety: also AST-pinned no-exec/eval/compile so "
                "the canonicalizer NEVER executes candidate code "
                "(only ast.parses it). Trust boundary."
            ),
            validate=_validate_ast_canonical_pure_stdlib,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "quorum_gate_consumes_cost_gated_routes"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "generative_quorum_gate.py"
            ),
            description=(
                "STRUCTURAL §26.6 cost-contract guard: Slice 4 "
                "gate MUST import COST_GATED_ROUTES from "
                "cost_contract_assertion AND reference it in its "
                "decision tree. Catches a refactor that drops "
                "the BG/SPEC cost-gate structurally BEFORE "
                "shipping."
            ),
            validate=(
                _validate_quorum_gate_consumes_cost_gated_routes
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="quorum_cap_structure_pinned",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "generative_quorum.py"
            ),
            description=(
                "K + agreement-threshold env knobs MUST use "
                "min(ceiling, max(floor, value)) clamps with "
                "named floor/ceiling constants. Catches refactor "
                "that loosens caps (e.g., K=10 through, "
                "threshold=1 single-roll consensus)."
            ),
            validate=_validate_quorum_cap_structure_pinned,
        ),
    )
    # Priority #1 Slice 5 — Coherence Auditor graduation pins.
    # Closes the gestalt-rotation blind spot identified in §28.7
    # brutal review: behavioral drift detection complementing
    # Move 4's structural drift via the same observer / store /
    # bridge architectural mirror. These pins protect the
    # structural primitives from refactor drift.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "coherence_auditor_no_authority_imports_primitive"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "coherence_auditor.py"
            ),
            description=(
                "Slice 1 Coherence Auditor primitive MUST be "
                "PURE-STDLIB — strongest authority invariant. "
                "Zero governance imports of any kind, zero "
                "exec/eval/compile (mirrors Move 6 Slice 2 "
                "ast_canonical's safety pin), no async (Slice 3 "
                "introduces async). Slice 3 observer feeds Slice "
                "1 pre-aggregated WindowData; the primitive "
                "stays decoupled from collection."
            ),
            validate=_validate_coherence_auditor_pure_stdlib,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "coherence_observer_no_authority_imports"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "coherence_observer.py"
            ),
            description=(
                "Slice 3 async observer MUST NOT import "
                "orchestrator-tier modules. Allowed governance "
                "imports: Slice 1 (coherence_auditor), Slice 2 "
                "(coherence_window_store), posture_observer "
                "(read-only), posture_health (Tier 1 #2 lazy), "
                "ide_observability_stream (lazy SSE)."
            ),
            validate=_validate_coherence_observer_no_authority,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="coherence_window_store_uses_flock",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "coherence_window_store.py"
            ),
            description=(
                "STRUCTURAL cross-process safety: Slice 2 store "
                "MUST reference flock_append_line (audit log) "
                "AND flock_critical_section (ring buffer). "
                "Catches refactor that drops cross-process "
                "safety on either persistence path."
            ),
            validate=_validate_coherence_window_store_uses_flock,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "coherence_action_bridge_consumes_adaptation_ledger"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "coherence_action_bridge.py"
            ),
            description=(
                "STRUCTURAL Phase C universal-cage-rule "
                "integration: Slice 4 bridge MUST import "
                "MonotonicTighteningVerdict from "
                "adaptation.ledger AND reference the symbol in "
                "code AND use flock_append_line for advisory "
                "persistence. Catches refactor that bypasses "
                "the universal cage rule or drops cross-process "
                "safety on the advisory log."
            ),
            validate=(
                _validate_coherence_action_bridge_uses_adaptation_ledger
            ),
        ),
    )
    # Priority #2 Slice 5 — PostmortemRecall graduation pins.
    # Closes the recurrence-prevention loop: detection (Move 4 +
    # Priority #1) translates to actual prevention. These 4 pins
    # protect the structural primitives from refactor drift.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="postmortem_recall_pure_stdlib",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "postmortem_recall.py"
            ),
            description=(
                "Slice 1 PostmortemRecord primitive MUST be "
                "PURE-STDLIB — strongest authority invariant. "
                "Zero governance imports. No exec/eval/compile "
                "(canonical safety pin). No async (Slices 1-4 "
                "are sync; Slice 5 wraps via to_thread). Mirrors "
                "Priority #1 Slice 1's discipline."
            ),
            validate=_validate_postmortem_recall_pure_stdlib,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_recall_index_uses_flock"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "postmortem_recall_index.py"
            ),
            description=(
                "STRUCTURAL cross-process safety + zero-"
                "duplication via reuse contract. Slice 2 index "
                "store MUST reference flock_append_line + "
                "flock_critical_section (Tier 1 #3) AND "
                "_sanitize_field + _parse_summary "
                "(LastSessionSummary canonical helpers). Catches "
                "refactor that drops cross-process safety or "
                "duplicates the canonical safety helpers."
            ),
            validate=(
                _validate_postmortem_recall_index_uses_flock
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_recall_injector_authority_free"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "postmortem_recall_injector.py"
            ),
            description=(
                "Slice 3 CONTEXT_EXPANSION injector MUST NOT "
                "import orchestrator-tier modules. MUST "
                "reference _sanitize_field + recall_postmortems "
                "+ read_index (the 3 zero-duplication reuse "
                "contracts). Robust degradation: no orchestrator "
                "coupling means no GENERATE-pipeline raise path."
            ),
            validate=(
                _validate_postmortem_recall_injector_authority_free
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_recall_consumer_uses_adaptation_ledger"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "postmortem_recall_consumer.py"
            ),
            description=(
                "STRUCTURAL Phase C universal-cage-rule "
                "integration: Slice 4 consumer MUST import "
                "MonotonicTighteningVerdict via importfrom from "
                "adaptation.ledger AND reference "
                "read_coherence_advisories (canonical reader "
                "reuse) AND INJECT_POSTMORTEM_RECALL_HINT "
                "(filter target). Catches refactor that bypasses "
                "Phase C vocabulary OR drops the action filter."
            ),
            validate=(
                _validate_postmortem_recall_consumer_uses_adaptation_ledger
            ),
        ),
    )
    # Priority #3 Slice 5 — Counterfactual Replay graduation pins.
    # Closes the policy-evaluation gap (prevention → empirical
    # measurement of effectiveness). These 4 pins protect the
    # structural primitives from refactor drift across the 4-slice
    # pipeline (primitive → engine → comparator → observer).
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="counterfactual_replay_pure_stdlib",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "counterfactual_replay.py"
            ),
            description=(
                "Slice 1 counterfactual_replay primitive MUST be "
                "PURE-STDLIB — strongest authority invariant. Zero "
                "governance imports. No exec/eval/compile (canonical "
                "safety pin). No async (Slice 2's engine wraps via "
                "asyncio.to_thread). Mirrors Priority #1/#2 Slice 1 "
                "discipline — observational, not prescriptive."
            ),
            validate=_validate_counterfactual_replay_pure_stdlib,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_replay_engine_cost_contract"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "counterfactual_replay_engine.py"
            ),
            description=(
                "STRUCTURAL §26.6 cost-contract preservation: "
                "Slice 2 engine MUST NOT import any orchestrator-"
                "tier module (providers / doubleword / "
                "urgency_router / candidate_generator / "
                "orchestrator / tool_executor / phase_runner / "
                "iron_gate / change_engine / auto_action_router / "
                "subagent_scheduler / semantic_guardian / "
                "semantic_firewall / risk_engine). MUST reuse "
                "causality_dag.build_dag + last_session_summary + "
                "DecisionRecord (no JSONL re-implementation). MUST "
                "define COST_CONTRACT_PRESERVED_BY_CONSTRUCTION. "
                "Replay's zero-LLM-cost guarantee is structural."
            ),
            validate=(
                _validate_counterfactual_replay_engine_cost_contract
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_replay_comparator_authority"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "counterfactual_replay_comparator.py"
            ),
            description=(
                "Slice 3 comparator MUST be PURE-DATA aggregator: "
                "no orchestrator-tier imports, no async (Slice 4 "
                "wraps via to_thread), no exec/eval/compile. MUST "
                "reuse Slice 1 closed-taxonomy enums (BranchVerdict, "
                "ReplayOutcome, ReplayVerdict). MUST resolve PASSED "
                "via adaptation.ledger.MonotonicTighteningVerdict "
                "for Phase C cross-stack vocabulary integration. "
                "Catches refactor that breaks zero-duplication or "
                "drops the canonical PASSED stamping."
            ),
            validate=(
                _validate_counterfactual_replay_comparator_authority
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_replay_observer_uses_flock"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "counterfactual_replay_observer.py"
            ),
            description=(
                "STRUCTURAL Tier 1 #3 cross-process safety + Slice "
                "3 reuse + Gap #6 broker reuse: Slice 4 observer "
                "MUST reference flock_append_line + "
                "flock_critical_section AND compare_replay_history "
                "+ stamp_verdict (zero re-aggregation/re-stamping) "
                "AND ide_observability_stream + the 2 new event "
                "vocabulary constants. Catches refactor that drops "
                "cross-process safety OR re-implements the "
                "comparator OR forgets to wire the SSE broker."
            ),
            validate=(
                _validate_counterfactual_replay_observer_uses_flock
            ),
        ),
    )
    # Priority #4 Slice 5 — Speculative Branch Tree graduation pins.
    # Closes the cognitive gap (CC's interleaved-thinking + plan-
    # mode-replan + speculative-branching) via Antivenom-aligned
    # tree topology. These 4 pins protect the structural primitives
    # from refactor drift across the 4-slice pipeline.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="speculative_branch_pure_stdlib",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "speculative_branch.py"
            ),
            description=(
                "Slice 1 speculative_branch primitive MUST be "
                "PURE-STDLIB — strongest authority invariant. Zero "
                "governance imports. No exec/eval/compile (canonical "
                "safety pin). No async (Slice 2's runner wraps via "
                "asyncio.gather). Mirrors Priority #1/#2/#3 Slice 1 "
                "discipline — observational, not prescriptive."
            ),
            validate=_validate_speculative_branch_pure_stdlib,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "speculative_branch_runner_cost_contract"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "speculative_branch_runner.py"
            ),
            description=(
                "STRUCTURAL §26.6 cost-contract preservation: Slice "
                "2 runner MUST NOT import any orchestrator-tier "
                "module (providers / doubleword / urgency_router / "
                "candidate_generator / orchestrator / tool_executor "
                "/ phase_runner / iron_gate / change_engine / "
                "auto_action_router / subagent_scheduler / "
                "semantic_guardian / semantic_firewall / "
                "risk_engine). MUST reuse Slice 1 primitives "
                "(compute_tree_verdict + compute_tree_outcome) AND "
                "Move 5's READONLY_TOOL_ALLOWLIST + "
                "is_tool_allowlisted (defense-in-depth tool "
                "filtering — no re-implementation of the 9-tool "
                "frozenset). MUST define "
                "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION."
            ),
            validate=(
                _validate_speculative_branch_runner_cost_contract
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "speculative_branch_comparator_authority"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "speculative_branch_comparator.py"
            ),
            description=(
                "Slice 3 comparator MUST be PURE-DATA aggregator: "
                "no orchestrator-tier imports, no async (Slice 4 "
                "wraps via to_thread), no exec/eval/compile. MUST "
                "reuse Slice 1 closed-taxonomy enums (TreeVerdict "
                "+ TreeVerdictResult). MUST resolve PASSED via "
                "adaptation.ledger.MonotonicTighteningVerdict for "
                "Phase C cross-stack vocabulary integration (5th "
                "module after Move 6 + Priority #1/#2/#3). Catches "
                "refactor that breaks zero-duplication or drops "
                "the canonical PASSED stamping."
            ),
            validate=(
                _validate_speculative_branch_comparator_authority
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "speculative_branch_observer_uses_flock"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "speculative_branch_observer.py"
            ),
            description=(
                "STRUCTURAL Tier 1 #3 cross-process safety + Slice "
                "3 reuse + Gap #6 broker reuse: Slice 4 observer "
                "MUST reference flock_append_line + "
                "flock_critical_section AND compare_tree_history + "
                "stamp_tree_verdict (zero re-aggregation/re-"
                "stamping) AND ide_observability_stream + the 2 "
                "new SBT event vocabulary constants. Catches "
                "refactor that drops cross-process safety OR re-"
                "implements the comparator OR forgets to wire the "
                "SSE broker."
            ),
            validate=(
                _validate_speculative_branch_observer_uses_flock
            ),
        ),
    )
    # Priority #5 Slice 5 — CIGW graduation pins. Closes the
    # long-horizon semantic gradient drift gap: per-APPLY structural
    # metric sampling vs Move 4's per-snapshot. These 4 pins protect
    # the structural primitives from refactor drift across the
    # 4-slice pipeline (primitive → collector → comparator → observer).
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="gradient_watcher_pure_stdlib",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "gradient_watcher.py"
            ),
            description=(
                "Slice 1 gradient_watcher primitive MUST be "
                "PURE-STDLIB — strongest authority invariant. Zero "
                "governance imports. No exec/eval/compile (canonical "
                "safety pin). No async (Slice 2's collector wraps "
                "via asyncio.to_thread). Mirrors Priority #1/#2/#3/"
                "#4 Slice 1 discipline — observational, not "
                "prescriptive."
            ),
            validate=_validate_gradient_watcher_pure_stdlib,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "gradient_collector_cost_contract"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "gradient_collector.py"
            ),
            description=(
                "STRUCTURAL §26.6 cost-contract preservation: Slice "
                "2 collector MUST NOT import any orchestrator-tier "
                "module. MUST reuse Slice 1 primitives "
                "(InvariantSample + MeasurementKind) AND stdlib "
                "``ast`` for structural-metric extraction. MUST "
                "define COST_CONTRACT_PRESERVED_BY_CONSTRUCTION. "
                "Per-sample cost ≤ Σ(file.read + ast.parse) — no "
                "generation calls."
            ),
            validate=(
                _validate_gradient_collector_cost_contract
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "gradient_comparator_authority"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "gradient_comparator.py"
            ),
            description=(
                "Slice 3 comparator MUST be PURE-DATA aggregator: "
                "no orchestrator-tier imports, no async (Slice 4 "
                "wraps via to_thread), no exec/eval/compile. MUST "
                "reuse Slice 1 closed-taxonomy enums (GradientReport "
                "+ GradientOutcome). MUST resolve PASSED via "
                "adaptation.ledger.MonotonicTighteningVerdict for "
                "Phase C cross-stack vocabulary integration (6th "
                "module after Move 6 + Priority #1/#2/#3/#4). Catches "
                "refactor that breaks zero-duplication or drops the "
                "canonical PASSED stamping."
            ),
            validate=(
                _validate_gradient_comparator_authority
            ),
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "gradient_observer_uses_flock"
            ),
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "gradient_observer.py"
            ),
            description=(
                "STRUCTURAL Tier 1 #3 cross-process safety + Slice "
                "3 reuse + Gap #6 broker reuse: Slice 4 observer "
                "MUST reference flock_append_line + "
                "flock_critical_section AND compare_gradient_history "
                "+ stamp_gradient_report (zero re-aggregation/re-"
                "stamping) AND ide_observability_stream + the 2 "
                "new CIGW event vocabulary constants. Catches "
                "refactor that drops cross-process safety OR "
                "re-implements the comparator OR forgets to wire "
                "the SSE broker."
            ),
            validate=(
                _validate_gradient_observer_uses_flock
            ),
        ),
    )
    # Gap #2 Slice 5 cage close — Slice 2 surface validator wiring.
    # Lives inline (not module-owned) because adaptation/ is NOT in
    # _INVARIANT_PROVIDER_PACKAGES.
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name=(
                "gap2_confidence_threshold_tightener_surface"
            ),
            target_file=(
                "backend/core/ouroboros/governance/adaptation/"
                "confidence_threshold_tightener.py"
            ),
            description=(
                "Gap #2 Slice 2 surface validator: must call "
                "install_surface_validator() at module level, "
                "reference AdaptationSurface."
                "CONFIDENCE_MONITOR_THRESHOLDS, run "
                "compute_policy_diff (predicate parity with cage), "
                "enforce sha256 hash prefix + tighten indicator. "
                "Catches refactor that drops cage entry-point "
                "registration."
            ),
            validate=_validate_confidence_threshold_tightener,
        ),
    )


# ---------------------------------------------------------------------------
# Module-owned invariant discovery (mirrors flag_registry's pattern)
# ---------------------------------------------------------------------------
#
# Curated list of provider PACKAGES whose direct submodules may
# contribute shipped-code invariants via
# ``register_shipped_invariants() -> List[ShippedCodeInvariant]``.
# Adding a NEW invariant inside an existing module requires zero
# edits here — the discovery loop picks it up. Adding an invariant
# in a NEW package requires one entry. Same architectural pattern
# as flag_registry_seed._FLAG_PROVIDER_PACKAGES.
_INVARIANT_PROVIDER_PACKAGES: Tuple[str, ...] = (
    "backend.core.ouroboros.governance",  # top-level (semantic_firewall, etc.)
    "backend.core.ouroboros.governance.verification",  # SBT/CIGW/etc.
)


def _discover_module_provided_invariants() -> int:
    """Walk every package in ``_INVARIANT_PROVIDER_PACKAGES`` for
    direct submodules exposing
    ``register_shipped_invariants() -> List[ShippedCodeInvariant]``.

    Each matching module returns its own invariant list, which is
    then registered via ``register_shipped_code_invariant``. New
    Antivenom v3/v4/... surfaces register their own structural
    pins co-located with the consuming code — no edits to this
    file required.

    NEVER raises. Per-module failures logged + skipped.

    The immune system scales organically: when a new module owns
    a new structural property, it owns the AST validation too."""
    discovered = 0
    try:
        from importlib import import_module
        import pkgutil
        for pkg_name in _INVARIANT_PROVIDER_PACKAGES:
            try:
                pkg_mod = import_module(pkg_name)
                pkg_path = getattr(pkg_mod, "__path__", None)
                if not pkg_path:
                    continue
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[ShippedInvariants] provider package %s unavailable: %s",
                    pkg_name, exc,
                )
                continue
            for _, name, _ispkg in pkgutil.iter_modules(pkg_path):
                full_name = f"{pkg_name}.{name}"
                if full_name == __name__:
                    continue
                try:
                    mod = import_module(full_name)
                    fn = getattr(mod, "register_shipped_invariants", None)
                    if not callable(fn):
                        continue
                    invariants = fn()
                    # Defensive: registrar must return iterable;
                    # garbage returns silently skipped.
                    if not invariants:
                        continue
                    try:
                        invariant_list = list(invariants)
                    except TypeError:
                        continue
                    for inv in invariant_list:
                        try:
                            register_shipped_code_invariant(inv)
                            discovered += 1
                        except Exception as exc:  # noqa: BLE001 — defensive
                            logger.debug(
                                "[ShippedInvariants] register failed for "
                                "%s/%s: %s",
                                full_name,
                                getattr(inv, "invariant_name", "?"),
                                exc,
                            )
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[ShippedInvariants] discover skipped %s: %s",
                        full_name, exc,
                    )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ShippedInvariants] _discover_module_provided_invariants "
            "exc: %s", exc,
        )
    return discovered


_register_seed_invariants()
# Dynamic discovery — invoked AFTER seed so module-owned pins
# compose on top. Idempotent: re-imports during testing replace
# existing invariants via override semantics.
_discover_module_provided_invariants()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "InvariantViolation",
    "SHIPPED_CODE_INVARIANTS_SCHEMA_VERSION",
    "ShippedCodeInvariant",
    "ShippedCodeValidator",
    "list_shipped_code_invariants",
    "register_shipped_code_invariant",
    "reset_registry_for_tests",
    "shipped_code_invariants_enabled",
    "unregister_shipped_code_invariant",
    "validate_all",
    "validate_invariant",
]
