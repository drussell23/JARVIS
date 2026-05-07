"""Venom V4 — shared tool-name pattern substrate.

Closes the structural-match-scoping gap surfaced 2026-05-07:
today V1 hook + V2 permission registries match by exact tool
name, so operators wanting to bind a callback to a FAMILY of
tools (``mcp_*`` / ``read_*`` / ``web_(search|fetch)``) must
either register N duplicate callbacks OR write per-call regex
inside their callback (error-prone, inconsistent, easy to
mismatch under :func:`asyncio.gather`).

Operator binding 2026-05-07 (verbatim — load-bearing):

  > "Single shared substrate ... 'compiled tool-name filter':
  > optional ``tool_name_pattern`` on registration,
  > ``re.compile`` at ``register()`` time (fail fast on invalid
  > pattern), and ``match(full tool_name)`` at dispatch time.
  > Unify V1 tool-hook registration and V2
  > ``PermissionRegistry.register`` so both use the same
  > matching semantics and ordering rules ... non-match ⇒
  > equivalent to not invoked / DEFER for aggregation, without
  > spawning a task — preserves async robustness and cuts
  > useless wait_for work."

Single source of truth — both
:mod:`lifecycle_hook_registry` and :mod:`tool_permission`
import the SAME ``compile_tool_name_pattern`` +
``matches_tool_name`` primitives so semantics + ordering rules
stay synchronized. AST-pinned: pattern compilation MUST happen
at registration time (no per-call ``re.compile`` allowed
downstream).

Architectural locks (AST-pinned):

  * **Stdlib-only** — module imports nothing from
    ``backend.core.ouroboros`` (substrate is consumed by
    governance modules; reverse import would cycle).
  * **NEVER raises out of dispatch** — :func:`matches_tool_name`
    returns False on garbage; ONLY :func:`compile_tool_name_pattern`
    raises (at registration time, fail-fast operator-misconfig
    surface).
  * **Closed semantics**: ``None`` pattern → universal True;
    ``str`` pattern → :func:`re.Pattern.fullmatch` against
    full tool name. Half-anchored matches are operator-mistakes
    (e.g. ``web_*`` accidentally matching ``prefix_web_x``).
  * **Bounded inputs** — pattern string ≤
    :func:`max_pattern_chars` (env-tunable; default 256).
    Rejects pathological patterns at register time.

Closed taxonomy:

  * :class:`InvalidToolNamePatternError` — registration-time
    misconfig (bad regex, oversized pattern, non-string).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


TOOL_NAME_PATTERN_SCHEMA_VERSION: str = (
    "tool_name_pattern.1"
)


# ---------------------------------------------------------------------------
# Env knobs — bounds, env-driven (no literals sprinkled in
# business logic per repo style)
# ---------------------------------------------------------------------------


def max_pattern_chars() -> int:
    """Hard cap on pattern string length. Defends against
    operator misconfig (pasted multi-line regex, accidental
    glob).  Default 256; env override
    ``JARVIS_TOOL_NAME_PATTERN_MAX_CHARS``. Clamps below 16
    (operator-unintended) and above 4096 (defensive)."""
    raw = os.environ.get(
        "JARVIS_TOOL_NAME_PATTERN_MAX_CHARS", "",
    ).strip()
    if not raw:
        return 256
    try:
        v = int(raw)
        return max(16, min(4096, v))
    except (TypeError, ValueError):
        return 256


# ---------------------------------------------------------------------------
# Closed exception taxonomy
# ---------------------------------------------------------------------------


class InvalidToolNamePatternError(ValueError):
    """Raised by :func:`compile_tool_name_pattern` on
    registration-time misconfig. Propagates to the registry's
    own validation exception (callers MUST translate this to
    their domain-specific error — e.g.,
    :class:`InvalidHookError`,
    :class:`InvalidPermissionCallbackError`).

    Distinct from generic ``ValueError`` so callers can
    isinstance-catch only this case without swallowing
    unrelated value errors from operator code paths."""


# ---------------------------------------------------------------------------
# Compiled pattern — frozen dataclass wrapping re.Pattern
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledToolNamePattern:
    """Frozen wrapper around a pre-compiled :class:`re.Pattern`.
    Carries the original pattern string for projection /
    diagnostics; the compiled object is the load-bearing
    member.

    Construction goes through :func:`compile_tool_name_pattern`;
    direct construction is permitted but operators SHOULD use
    the factory so length+regex validation runs once at
    registration time."""

    schema_version: str
    raw: str
    compiled: "re.Pattern[str]"


# ---------------------------------------------------------------------------
# Compile (registration time, fail-fast) + match (dispatch time)
# ---------------------------------------------------------------------------


def compile_tool_name_pattern(
    pattern: Optional[str],
) -> Optional[CompiledToolNamePattern]:
    """Compile a regex pattern at registration time. Returns:

      * ``None`` when ``pattern`` is ``None`` — the canonical
        universal-match signal (no filter; equivalent to
        unscoped registration).
      * :class:`CompiledToolNamePattern` for any non-None
        pattern.

    Raises :class:`InvalidToolNamePatternError` on:

      * Non-string input.
      * Empty string (operator-mistake — use ``None`` for
        universal match).
      * Length > :func:`max_pattern_chars`.
      * Invalid regex syntax (``re.error``).

    Compile-once contract: callers store the returned
    :class:`CompiledToolNamePattern` on the registration
    record + invoke :func:`matches_tool_name` at dispatch time.
    AST-pinned: dispatch-time ``re.compile`` is forbidden — the
    pattern lives on the registration record exclusively.
    """
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise InvalidToolNamePatternError(
            f"tool_name_pattern must be str or None — got "
            f"{type(pattern).__name__}"
        )
    if pattern == "":
        raise InvalidToolNamePatternError(
            "tool_name_pattern must not be empty — pass None "
            "for universal match"
        )
    cap = max_pattern_chars()
    if len(pattern) > cap:
        raise InvalidToolNamePatternError(
            f"tool_name_pattern exceeds max length "
            f"{cap} chars (got {len(pattern)})"
        )
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise InvalidToolNamePatternError(
            f"tool_name_pattern is not valid regex: {exc} "
            f"(pattern={pattern!r})"
        ) from exc
    return CompiledToolNamePattern(
        schema_version=TOOL_NAME_PATTERN_SCHEMA_VERSION,
        raw=pattern,
        compiled=compiled,
    )


def matches_tool_name(
    compiled: Optional[CompiledToolNamePattern],
    tool_name: str,
) -> bool:
    """Match a tool name against a compiled pattern. Pure
    function. NEVER raises.

    Semantics:

      * ``compiled is None`` → True (universal match — caller
        registered without a pattern, callback fires for every
        tool).
      * ``compiled is`` :class:`CompiledToolNamePattern` →
        :func:`re.Pattern.fullmatch` semantics (pattern must
        match the ENTIRE tool name; half-anchored matches are
        operator-mistakes).
      * Garbage ``tool_name`` (None / non-string) → False.

    Dispatch-time hot path. Designed to be called once per
    registration before deciding whether to spawn a task."""
    if compiled is None:
        return True
    try:
        if not isinstance(tool_name, str):
            return False
        m = compiled.compiled.fullmatch(tool_name)
        return m is not None
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Operator-visible helper — projection-friendly raw string
# ---------------------------------------------------------------------------


def pattern_raw(
    compiled: Optional[CompiledToolNamePattern],
) -> Optional[str]:
    """Return the raw pattern string (for projection / audit /
    SSE telemetry). NEVER raises."""
    if compiled is None:
        return None
    try:
        return str(compiled.raw)
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``tool_name_pattern_authority_asymmetry`` — substrate
         purity. Forbids ANY ``backend.core.ouroboros`` import
         (this is a stdlib-only primitive consumed by the
         governance registries; reverse import would cycle).
      2. ``tool_name_pattern_compile_once_contract`` — the
         module-level :func:`compile_tool_name_pattern` is the
         SOLE caller of :func:`re.compile` in this file; no
         dispatch-time ``re.compile`` is allowed (the compiled
         pattern lives on the registration record).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "tool_name_pattern.py"
    )

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Substrate is stdlib-only at the hot path — NO
        ``backend.core.ouroboros`` imports OUTSIDE the
        ``register_shipped_invariants`` exemption (which
        lazy-imports ``ShippedCodeInvariant`` per the
        Priority #6 closure registration-contract pattern). A
        reverse import elsewhere would create a cycle."""
        violations: list = []
        # Walk top-level + function bodies separately so the
        # registration-contract exemption applies to imports
        # inside ``register_shipped_invariants`` only.
        exempt_function_names = frozenset({
            "register_shipped_invariants",
        })
        # Top-level imports: must be stdlib-only.
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(
                    "backend.core.ouroboros",
                ):
                    violations.append(
                        f"tool_name_pattern.py is stdlib-only "
                        f"at module top — MUST NOT import "
                        f"{module!r} (operator binding 2026-"
                        f"05-07)"
                    )
        # Nested imports: only allowed inside the exempt
        # registration-contract function.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name in exempt_function_names:
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.ImportFrom):
                        module = sub.module or ""
                        if module.startswith(
                            "backend.core.ouroboros",
                        ):
                            violations.append(
                                f"tool_name_pattern.py "
                                f"function {node.name!r} MUST "
                                f"NOT import {module!r} — "
                                f"substrate purity"
                            )
        return tuple(violations)

    def _validate_compile_once_contract(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``re.compile`` MUST appear ONLY inside
        :func:`compile_tool_name_pattern`. Catches refactors
        that accidentally introduce dispatch-time compilation
        (which would defeat the compile-once-at-register
        contract)."""
        violations: list = []
        # Find re.compile call sites + check their enclosing
        # function name.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "compile_tool_name_pattern":
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        if (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "compile"
                            and isinstance(fn.value, ast.Name)
                            and fn.value.id == "re"
                        ):
                            violations.append(
                                f"re.compile() in function "
                                f"{node.name!r} — V4 contract "
                                f"requires compile-once at "
                                f"registration; only "
                                f"compile_tool_name_pattern "
                                f"may invoke re.compile"
                            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "tool_name_pattern_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Venom V4 — substrate purity (stdlib only)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_name_pattern_compile_once_contract"
            ),
            target_file=target,
            description=(
                "Venom V4 — compile-once at registration; "
                "dispatch-time re.compile forbidden."
            ),
            validate=_validate_compile_once_contract,
        ),
    ]


__all__ = [
    "CompiledToolNamePattern",
    "InvalidToolNamePatternError",
    "TOOL_NAME_PATTERN_SCHEMA_VERSION",
    "compile_tool_name_pattern",
    "matches_tool_name",
    "max_pattern_chars",
    "pattern_raw",
    "register_shipped_invariants",
]
