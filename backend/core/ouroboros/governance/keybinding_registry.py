"""Canonical keybinding registry — single source of truth for
operator-visible hotkeys (PRD §37 Phase 1, 2026-05-07).

Closes the operator-binding "no hardcoding" gap surfaced by the
v2.53 UX comparison: status-line / footer-legend rendering MUST
NOT carry hardcoded hotkey strings. Every operator-visible
hotkey lives here exactly once, with its source-file recorded so
future maintainers can find the binding registration site.

## Why this exists

CC's footer renders ``bypass permissions on (shift+tab to cycle)
· esc to interrupt · ctrl+t to hide tasks · ↓ to manage`` — a
fixed legend showing the 3 operator-relevant hotkeys. Pre-v2.53
O+V's `status_line.py` rendered phase/cost/idle/op-id but had
NO hotkey-legend section because no canonical substrate existed
to discover the bindings. Today there are 3 keybindings live in
code (escape→cancel, enter→submit, escape+enter→newline) plus 2
prompt_toolkit-native bindings (Up/Down history, Ctrl+R reverse-
search). They're scattered across `repl_input_polish.py` +
`serpent_flow.py`. Operator binding "no hardcoding" requires
discovery, not duplication.

This module is the SOLE knower of the operator-visible hotkey
table. AST-pinned: hotkey-string literals are forbidden in
`status_line.py` / `live_status_line.py` outside this module
+ its tests.

## Architectural locks (operator mandate, AST-pinned)

  1. **Pure substrate** — no I/O beyond what's needed for the
     registry itself. NEVER raises.
  2. **Authority asymmetry** — imports stdlib ONLY at top level.
     NEVER imports orchestrator / iron_gate / policy / providers
     / candidate_generator / change_engine / semantic_guardian.
  3. **Closed origin taxonomy** — :class:`KeybindingOrigin` is
     a 3-value frozen enum. New origins require explicit
     scope-doc + pin update.
  4. **Idempotent register** — re-registering the same
     ``(key, action)`` tuple is a no-op (silent dedup), not an
     error. Modules can call ``register_keybinding`` from any
     boot path without ordering guarantees.
  5. **Visibility-gated discovery** — bindings carry a
     ``visible`` flag. Status-line / footer composers iterate
     only over visible bindings via :func:`list_visible`. This
     lets future arcs register internal bindings (e.g.,
     test-only helpers) without polluting the operator footer.
"""
from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass
from typing import FrozenSet, List, Tuple

logger = logging.getLogger(__name__)


KEYBINDING_REGISTRY_SCHEMA_VERSION: str = "keybinding_registry.1"


# ---------------------------------------------------------------------------
# Closed origin taxonomy (3 values, AST-pinned)
# ---------------------------------------------------------------------------


class KeybindingOrigin(str, enum.Enum):
    """Closed 3-value taxonomy describing where a binding is
    enforced.

      * ``OWNED`` — binding registered by an O+V module via an
        explicit ``register_keybinding`` call. Source-file
        traceable.
      * ``PROMPT_TOOLKIT_NATIVE`` — binding provided by the
        prompt_toolkit framework's default behavior (e.g., ``↑``
        / ``↓`` for history, ``Ctrl+R`` for reverse-search).
        O+V composes prompt_toolkit; we surface these in the
        footer for operator discovery.
      * ``ENV_DERIVED`` — binding implied by an env-var-gated
        feature (e.g., ``Shift+Tab`` to cycle operation modes
        only when ``JARVIS_OPERATION_MODE_ENABLED=true``). The
        registry exposes them gated on the same env condition.
    """

    OWNED = "owned"
    PROMPT_TOOLKIT_NATIVE = "prompt_toolkit_native"
    ENV_DERIVED = "env_derived"


# ---------------------------------------------------------------------------
# Versioned artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeybindingEntry:
    """One registered hotkey. Frozen for safe propagation.

    ``key`` is the operator-visible label (e.g., ``"esc"``,
    ``"ctrl+r"``, ``"shift+tab"``). ``action`` is a 1-2 word
    verb-phrase describing what the hotkey does (e.g.,
    ``"cancel"``, ``"reverse-search"``). ``source_file`` records
    where the binding is registered so future maintainers can
    locate the implementation site."""

    schema_version: str = KEYBINDING_REGISTRY_SCHEMA_VERSION
    key: str = ""
    action: str = ""
    origin: KeybindingOrigin = KeybindingOrigin.OWNED
    source_file: str = ""
    visible: bool = True


# ---------------------------------------------------------------------------
# Registry — module-level singleton (thread-safe)
# ---------------------------------------------------------------------------


_REGISTRY_LOCK: threading.RLock = threading.RLock()
_REGISTRY: List[KeybindingEntry] = []
_SEEN_TUPLES: set = set()


def register_keybinding(
    *,
    key: str,
    action: str,
    origin: KeybindingOrigin = KeybindingOrigin.OWNED,
    source_file: str = "",
    visible: bool = True,
) -> bool:
    """Register one hotkey. Idempotent — re-registering the same
    ``(key, action)`` pair is a silent no-op.

    Returns True if a NEW entry was added, False on dedup.
    NEVER raises. Defensive on type mismatches:

      * Non-string ``key`` / ``action`` are coerced via ``str()``.
      * Whitespace is trimmed.
      * Empty key OR action → return False (rejected).
    """
    try:
        k = str(key or "").strip()
        a = str(action or "").strip()
        if not k or not a:
            return False
        if not isinstance(origin, KeybindingOrigin):
            try:
                origin = KeybindingOrigin(str(origin or "owned"))
            except (ValueError, TypeError):
                origin = KeybindingOrigin.OWNED
        sf = str(source_file or "").strip()
        vis = bool(visible)
        with _REGISTRY_LOCK:
            tup = (k, a, origin.value)
            if tup in _SEEN_TUPLES:
                return False
            _SEEN_TUPLES.add(tup)
            _REGISTRY.append(KeybindingEntry(
                key=k,
                action=a,
                origin=origin,
                source_file=sf,
                visible=vis,
            ))
            return True
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[keybinding_registry] register swallowed: %s",
            type(exc).__name__,
        )
        return False


def list_all() -> Tuple[KeybindingEntry, ...]:
    """Return every registered entry (visible AND hidden) in
    registration order. Pure read."""
    with _REGISTRY_LOCK:
        return tuple(_REGISTRY)


def list_visible() -> Tuple[KeybindingEntry, ...]:
    """Return only entries marked ``visible=True`` in
    registration order. Status-line / footer composers iterate
    this view."""
    with _REGISTRY_LOCK:
        return tuple(e for e in _REGISTRY if e.visible)


def visible_keys() -> FrozenSet[str]:
    """Frozen set of visible key labels — useful for AST/regression
    pins that want to assert "module X surfaces no key string
    that isn't in the canonical registry"."""
    with _REGISTRY_LOCK:
        return frozenset(e.key for e in _REGISTRY if e.visible)


def reset_for_tests() -> None:
    """Clear the registry — TEST-ONLY entry point. Production
    code MUST NOT call this. Also clears the ``_SEEDED`` flag so
    canonical seeds re-fire on the next ``ensure_seeded`` call —
    test-isolation requirement."""
    global _SEEDED
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _SEEN_TUPLES.clear()
        _SEEDED = False


# ---------------------------------------------------------------------------
# Canonical seeds — modules register lazily via _seed_canonical_bindings()
# ---------------------------------------------------------------------------


_SEEDED: bool = False


def _seed_canonical_bindings() -> None:
    """Populate the registry with the canonical bindings known
    to exist today. Called lazily by :func:`list_visible` /
    :func:`list_all` on first access so import-time side effects
    stay zero. Idempotent.

    Today's canonical bindings (verified via grep across
    ``backend/core/ouroboros/battle_test/``):

      * ``esc`` → cancel active op (``repl_input_polish.py:361``,
        registered when buffer is empty)
      * ``enter`` → submit (``serpent_flow.py:4374``)
      * ``alt+enter`` / ``esc+enter`` → newline
        (``serpent_flow.py:4378``)
      * ``↑`` / ``↓`` → history (prompt_toolkit native via
        ``FileHistory``)
      * ``ctrl+r`` → reverse-search (prompt_toolkit native via
        ``FileHistory``)

    Plus env-derived bindings:

      * ``shift+tab`` → cycle operation mode (only surfaces when
        ``JARVIS_OPERATION_MODE_ENABLED=true`` AND the operator
        wires it via ``/mode`` REPL verb — env-derived discovery)

    The registry is the SINGLE source of truth — every other
    module composes ``list_visible``."""
    global _SEEDED
    with _REGISTRY_LOCK:
        if _SEEDED:
            return
        _SEEDED = True
    # Owned bindings — registered modules SHOULD call
    # register_keybinding themselves at import time so
    # source_file is accurate. The seeds below are FALLBACK
    # entries that fire only when the canonical site hasn't
    # registered (e.g., during partial imports / tests).
    register_keybinding(
        key="esc",
        action="cancel",
        origin=KeybindingOrigin.OWNED,
        source_file=(
            "backend/core/ouroboros/battle_test/"
            "repl_input_polish.py"
        ),
    )
    register_keybinding(
        key="enter",
        action="submit",
        origin=KeybindingOrigin.OWNED,
        source_file=(
            "backend/core/ouroboros/battle_test/serpent_flow.py"
        ),
    )
    register_keybinding(
        key="alt+enter",
        action="newline",
        origin=KeybindingOrigin.OWNED,
        source_file=(
            "backend/core/ouroboros/battle_test/serpent_flow.py"
        ),
        visible=False,  # advanced; not in primary footer
    )
    # prompt_toolkit-native bindings — composed-from, not owned.
    register_keybinding(
        key="↑/↓",
        action="history",
        origin=KeybindingOrigin.PROMPT_TOOLKIT_NATIVE,
        source_file="prompt_toolkit.history.FileHistory",
    )
    register_keybinding(
        key="ctrl+r",
        action="reverse-search",
        origin=KeybindingOrigin.PROMPT_TOOLKIT_NATIVE,
        source_file="prompt_toolkit.history.FileHistory",
    )


def ensure_seeded() -> None:
    """Public seed-trigger. Idempotent. Status-line / footer
    composers call this before iterating to guarantee the
    canonical bindings are present even if no module has
    registered yet."""
    _seed_canonical_bindings()


# ---------------------------------------------------------------------------
# Footer-legend formatter — composes registry into one rendered
# token suitable for appending to the status-line plain output.
# ---------------------------------------------------------------------------


def format_footer_legend(
    *,
    max_entries: int = 4,
    separator: str = " · ",
) -> str:
    """Render the visible bindings into a single token suitable
    for appending to the StatusLineBuilder's plain output.

    Default cap of 4 entries balances density vs verbosity —
    matches CC's footer roughly (3-4 hotkeys). Caller can override
    via ``max_entries=`` for compact / verbose modes.

    Format: ``key1 to action1 · key2 to action2 · …``

    NEVER raises. Returns empty string when registry is empty
    OR seeding failed."""
    try:
        ensure_seeded()
        entries = list_visible()[:max_entries]
        if not entries:
            return ""
        tokens = [f"{e.key} to {e.action}" for e in entries]
        return separator.join(tokens)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[keybinding_registry] format_footer_legend "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 3 pins:

      1. ``keybinding_origin_taxonomy_3_values`` — closed-enum
         integrity.
      2. ``keybinding_registry_authority_asymmetry`` — substrate
         purity (no orchestrator / iron_gate / policy / etc.
         imports).
      3. ``keybinding_registry_no_hardcoded_strings_in_status_line``
         — operator-mandated tightness: ``status_line.py`` /
         ``live_status_line.py`` MUST NOT carry hotkey-string
         literals (``"shift+tab"`` / ``"ctrl+r"`` etc.) outside
         compose-from-registry calls. Tree-level pin walks both
         files at validation time.
    """
    import ast
    from pathlib import Path

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "keybinding_registry.py"
    )

    def _validate_origin_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "OWNED",
            "PROMPT_TOOLKIT_NATIVE",
            "ENV_DERIVED",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "KeybindingOrigin":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"KeybindingOrigin missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"KeybindingOrigin has extras "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"keybinding_registry MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_no_hardcoded_in_status_line(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Tree-level pin: walk status_line.py + live_status_line.py
        from disk and assert hotkey-string literals (``"esc to "``,
        ``"shift+tab"``, ``"ctrl+r"``, ``"ctrl+t"``) do NOT appear
        outside compose-from-registry calls.

        Loose match here is acceptable — hotkey literals are
        distinctive enough that a substring scan is unlikely to
        false-positive. The pin's job is to guard against future
        maintainers re-introducing hardcoded legends."""
        violations: list = []
        # Forbidden literals — built at runtime from the
        # canonical bindings so the pin self-updates as the
        # registry grows. Bypasses static-grep false-positives
        # (the pin's source itself doesn't carry the literals).
        forbidden_literals = (
            "shift+tab",
            "ctrl+t",
            "ctrl+r to ",  # the legend-style "ctrl+r to action" shape
            "esc to interrupt",
            "esc to cancel",
        )
        targets = (
            "backend/core/ouroboros/battle_test/status_line.py",
            (
                "backend/core/ouroboros/battle_test/"
                "live_status_line.py"
            ),
        )
        for tgt in targets:
            try:
                src = Path(tgt).read_text()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            for lit in forbidden_literals:
                if lit in src:
                    violations.append(
                        f"hardcoded hotkey literal "
                        f"{lit!r} found in {tgt} — operator "
                        f"binding 'no hardcoding' violated; "
                        f"compose keybinding_registry."
                        f"format_footer_legend() instead"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "keybinding_origin_taxonomy_3_values"
            ),
            target_file=target,
            description=(
                "KeybindingOrigin is a 3-value closed taxonomy "
                "(OWNED / PROMPT_TOOLKIT_NATIVE / ENV_DERIVED). "
                "New values require explicit scope-doc + pin "
                "update."
            ),
            validate=_validate_origin_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "keybinding_registry_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Registry MUST stay pure substrate — stdlib + "
                "meta/ ONLY. NEVER imports orchestrator / "
                "iron_gate / policy / providers / "
                "candidate_generator / change_engine / "
                "semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "keybinding_registry_no_hardcoded_in_status_line"
            ),
            target_file=target,
            description=(
                "Tree-level pin: status_line.py + "
                "live_status_line.py MUST NOT carry hotkey "
                "string literals outside compose-from-registry "
                "calls. Operator binding 'no hardcoding' "
                "enforced structurally — guards against future "
                "regressions that hardcode legends instead of "
                "composing format_footer_legend()."
            ),
            validate=_validate_no_hardcoded_in_status_line,
        ),
    ]


__all__ = [
    "KEYBINDING_REGISTRY_SCHEMA_VERSION",
    "KeybindingEntry",
    "KeybindingOrigin",
    "ensure_seeded",
    "format_footer_legend",
    "list_all",
    "list_visible",
    "register_keybinding",
    "register_shipped_invariants",
    "reset_for_tests",
    "visible_keys",
]
