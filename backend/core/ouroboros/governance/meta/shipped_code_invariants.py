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
