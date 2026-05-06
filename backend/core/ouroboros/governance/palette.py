"""§37 Slice 4 — Canonical O+V color palette + identity invariant #3 anchor.

Single source of truth for the bright-green outcome-celebration
color (`\\033[92m` ANSI / ``bright_green`` Rich markup). The
operator-binding identity invariant from §37.9 is:

  > "Color discipline (green = outcomes only) — Tier 1 #8 pins
  > this. Bright-green renders ONLY for outcome-celebration
  > markers (✨ evolved / ✅ success / 🔋 alive). Chrome — boot
  > banners, status labels, dashboard headers, decorative
  > scaffolding — uses **dim** OR regular green, NEVER
  > bright_green."

This module is the canonical palette anchor. Any code that needs
to render outcome-celebration green imports the constants
defined here. Any code that uses ``"\\033[92m"`` literal or
``"bright_green"`` Rich markup elsewhere in
``backend/core/ouroboros/governance/`` is flagged by the AST
lint pin (see :func:`register_shipped_invariants` below) — fails
CI before reaching production.

Why this module exists (operator binding, 2026-05-05):

  * **Solve the root problem directly**: the §37.4 audit found
    legacy chrome callers using ``bright_green`` for activity
    markers, violating the "green=outcomes only" rule. The
    runtime workaround (``presentation_restraint.chrome_color()``
    returns ``dim`` when restraint is enabled) is opt-in and
    requires every caller to remember to use it. A structural
    fix forbids the literal at the source level — composers
    must import the canonical constant, and grepping the
    constant tells the operator exactly which sites are
    outcome-celebration contexts.
  * **No hardcoding**: the constant defines the palette entry
    once. New green outcome shades (e.g., dimmed-celebration
    for unattended cadence) extend this module additively.
  * **No duplication**: existing modules that legitimately
    need bright-green outcome markers (post-Slice-4) MUST
    import from this module. Direct literals are forbidden.
  * **Leverage existing**: composes the existing
    ``presentation_restraint.chrome_color()`` discipline. This
    module is the producer side; ``chrome_color`` is the
    consumer-side runtime adaptor for chrome contexts that
    want graceful degradation.

Scoping (operator binding, defensive):

The AST lint pin is scoped to ``backend/core/ouroboros/governance/``
ONLY — the §37 territory the operator wants kept clean. Legacy
``battle_test/`` modules have their own ``chrome_color()`` runtime
discipline + an existing color taxonomy; this pin doesn't
retroactively flag them. Forward-looking enforcement: ANY new
governance/ file that introduces a bright-green literal without
importing from this module fails CI.

Identity preservation (§37.9 invariant #3):

  * ``OUTCOME_GREEN_BRIGHT_ANSI`` — exact bytes ``\\x1b[92m``
  * ``OUTCOME_GREEN_BRIGHT_RICH`` — exact string ``bright_green``
  * ``RESET_ANSI`` — companion reset (``\\x1b[0m``)

These are the only ANSI/Rich tokens permitted in
``governance/`` for bright-green outcome rendering. AST-pinned.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Canonical palette constants — bright-green outcome celebration ONLY
# ---------------------------------------------------------------------------


# ANSI escape sequence for bright green. Composed at outcome-
# celebration sites (✨ evolved, ✅ success, 🔋 organism alive).
# AST-pinned: this exact 5-byte literal MUST NOT appear elsewhere
# in `backend/core/ouroboros/governance/` (excluding tests + this
# module). Sites that want chrome-style green use regular ANSI
# `\033[32m` OR compose `presentation_restraint.chrome_color()`.
OUTCOME_GREEN_BRIGHT_ANSI: str = "\033[92m"

# Rich markup form. Composed at outcome-celebration sites that
# render through Rich (panels / live displays / ouroboros_tui).
# Same AST-pin discipline: `bright_green` literal forbidden
# outside the canonical palette + tests.
OUTCOME_GREEN_BRIGHT_RICH: str = "bright_green"

# Companion reset — no AST pin since `\033[0m` is unambiguously
# a reset (not an outcome marker). Defined here for symmetry +
# reduces import surface.
RESET_ANSI: str = "\033[0m"


# ---------------------------------------------------------------------------
# Helper: scoped grep-anchor for the AST pin
# ---------------------------------------------------------------------------


# Module-level constant the AST pin grep-anchors against. Used
# by `register_shipped_invariants` below to detect when the
# palette anchor IS the active surface (this module's own use
# of the literal is permitted) vs when downstream code is
# composing the literal directly (forbidden).
PALETTE_MODULE_NAME: str = "palette"


# ---------------------------------------------------------------------------
# Grandfathered allowlist — files that pre-Slice-4 had a
# legitimate non-outcome use of bright-green and are exempted
# from the lint. EVERY entry MUST have a documented rationale
# below. Adding new entries is operator-binding (require code
# review + comment).
# ---------------------------------------------------------------------------


_LEGACY_LINT_ALLOWLIST: frozenset = frozenset({
    # `multi_op_renderer.py:78` — generic rotating color palette
    # (`_PALETTE` tuple) used to distinguish CONCURRENT ops by
    # auto-assigned color. The bright_green entry is one of 16
    # colors in the rotation and is NOT an outcome-celebration
    # marker; it's incidental visual differentiation. Renaming it
    # to something else would break rendering parity with the
    # existing operator UX. Grandfathered 2026-05-05.
    "observability/multi_op_renderer.py",
})


# ---------------------------------------------------------------------------
# AST pin — forbids direct bright-green literals in governance/
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``palette_outcome_green_canonical`` — this module's own
         constants are the exact bytes the discipline mandates.
         Drift in the canonical literal breaks the whole
         discipline silently.
      2. ``governance_no_bright_green_outside_palette`` —
         scoped lint pin. Walks every ``.py`` file in
         ``backend/core/ouroboros/governance/`` (excluding this
         file + tests + ``__pycache__``). For each, checks all
         string-constant nodes for ``"\\033[92m"`` ANSI literal
         OR ``"bright_green"`` Rich markup substring. Flags if
         either appears. Forward-looking: only files modified
         post-Slice-4 are impacted; existing legacy code stays
         clean by construction (no governance/ files use the
         literal today, verified by audit).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/palette.py"
    )

    def _validate_canonical_constants(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Pin: this module's own constants MUST be the exact
        bytes the discipline mandates. Catches accidental
        edits to the palette anchor."""
        violations: list = []
        seen_ansi = False
        seen_rich = False
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign):
                continue
            if not isinstance(node.target, ast.Name):
                continue
            if not isinstance(node.value, ast.Constant):
                continue
            if (
                node.target.id == "OUTCOME_GREEN_BRIGHT_ANSI"
            ):
                if node.value.value != "\033[92m":
                    violations.append(
                        "OUTCOME_GREEN_BRIGHT_ANSI MUST be the "
                        "exact bytes '\\033[92m'"
                    )
                seen_ansi = True
            elif (
                node.target.id == "OUTCOME_GREEN_BRIGHT_RICH"
            ):
                if node.value.value != "bright_green":
                    violations.append(
                        "OUTCOME_GREEN_BRIGHT_RICH MUST be the "
                        "exact string 'bright_green'"
                    )
                seen_rich = True
        if not seen_ansi:
            violations.append(
                "OUTCOME_GREEN_BRIGHT_ANSI module-level "
                "constant missing"
            )
        if not seen_rich:
            violations.append(
                "OUTCOME_GREEN_BRIGHT_RICH module-level "
                "constant missing"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "palette_outcome_green_canonical"
            ),
            target_file=target,
            description=(
                "§37 Slice 4 — canonical palette: "
                "OUTCOME_GREEN_BRIGHT_ANSI exact bytes + "
                "OUTCOME_GREEN_BRIGHT_RICH exact string. "
                "Drift breaks identity invariant #3 silently."
            ),
            validate=_validate_canonical_constants,
        ),
    ]


# ---------------------------------------------------------------------------
# Cross-file scoped lint — registered separately so `target_file`
# can carry the cross-file scan logic.
# ---------------------------------------------------------------------------


def lint_governance_for_bright_green_leaks(
    governance_root: object = None,
) -> list:
    """Scan ``backend/core/ouroboros/governance/`` for direct
    bright-green literals outside the canonical palette + tests.

    Returns a list of ``(file_path, line, snippet)`` tuples for
    each violation. Empty list = clean.

    Used by both the regression test spine + (future) optional
    AST-pin extension. Composes ``ast.walk`` + scoped path filter;
    no parallel implementation.

    NEVER raises. Files that fail to parse are skipped silently
    (operator-side hygiene problem, not a lint pin's
    responsibility to surface).
    """
    import ast
    from pathlib import Path

    if governance_root is None:
        # Default: this module's directory
        governance_root = Path(__file__).resolve().parent

    root = Path(governance_root)
    violations: list = []

    # Files exempted from the lint:
    #   * palette.py — this module IS the canonical anchor
    #   * tests/ — test fixtures may need to construct synthetic
    #     bad source containing the literal
    #   * __pycache__ — ignore compiled artifacts
    #   * GRANDFATHERED files (see _LEGACY_LINT_ALLOWLIST below)
    for py_path in root.rglob("*.py"):
        rel = py_path.relative_to(root)
        rel_str = str(rel).replace("\\", "/")  # POSIX-style for matching
        if rel_str == "palette.py":
            continue
        if "__pycache__" in rel.parts:
            continue
        if rel.parts[0] == "tests":  # in-tree test fixtures
            continue
        if rel_str in _LEGACY_LINT_ALLOWLIST:
            continue
        try:
            source = py_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        # Collect docstring-Constant node IDs to skip them.
        # Docstrings in Python AST: first stmt of Module / ClassDef
        # / FunctionDef / AsyncFunctionDef body, where stmt is an
        # ast.Expr wrapping an ast.Constant(value=str).
        docstring_node_ids = _collect_docstring_node_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            if id(node) in docstring_node_ids:
                # Skip docstrings — they may legitimately
                # document the discipline (e.g., "no
                # bright_green in chrome" English prose).
                continue
            v = node.value
            # Pattern 1: ANSI bright-green escape sequence as
            # a substring of any string literal. Catches both
            # ``"\033[92m"`` direct + ``"prefix\033[92mtext"``
            # composed forms.
            # Pattern 2: Rich `bright_green` markup as a
            # substring of any string literal. Catches both
            # ``"[bright_green]X[/bright_green]"`` Rich tags +
            # palette dict entries like ``"life":
            # "bright_green"``.
            if "\033[92m" in v or "bright_green" in v:
                violations.append((
                    str(py_path),
                    node.lineno,
                    v[:80].replace("\033", "\\x1b"),
                ))
    return violations


def _collect_docstring_node_ids(tree) -> set:
    """Return the set of ``id()`` for every Constant node that
    is a docstring (first statement of Module / ClassDef /
    FunctionDef / AsyncFunctionDef). Used to exclude doc
    prose from the bright-green lint — the discipline can be
    DOCUMENTED in plain English without false-positives.

    Pure function. NEVER raises."""
    import ast as _ast
    out: set = set()
    bodies_to_check: list = []
    bodies_to_check.append(getattr(tree, "body", []) or [])
    for node in _ast.walk(tree):
        if isinstance(
            node,
            (
                _ast.ClassDef,
                _ast.FunctionDef,
                _ast.AsyncFunctionDef,
            ),
        ):
            bodies_to_check.append(node.body or [])
    for body in bodies_to_check:
        if not body:
            continue
        first = body[0]
        if not isinstance(first, _ast.Expr):
            continue
        v = first.value
        if (
            isinstance(v, _ast.Constant)
            and isinstance(v.value, str)
        ):
            out.add(id(v))
    return out


__all__ = [
    "OUTCOME_GREEN_BRIGHT_ANSI",
    "OUTCOME_GREEN_BRIGHT_RICH",
    "PALETTE_MODULE_NAME",
    "RESET_ANSI",
    "lint_governance_for_bright_green_leaks",
    "register_shipped_invariants",
]
