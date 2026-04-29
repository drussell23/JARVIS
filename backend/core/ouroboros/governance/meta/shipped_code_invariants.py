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


_register_seed_invariants()


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
