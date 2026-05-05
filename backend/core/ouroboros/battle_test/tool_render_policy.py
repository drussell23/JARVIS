"""DensityPolicy — adaptive tool-result render density.
========================================================

Slice 2 of the **Gap #2 closure arc**. Builds on the Slice 1 substrate
(``tool_render_registry``) by adding a posture × layout × env policy
layer that decides *how much* of a tool result to render.

Root problem
------------

Today's two render paths (``serpent_flow.op_tool_call`` +
``ouroboros_tui.show_tool_call``) hardcode line caps (``8``/``20``/
``200`` chars) that ignore the operator's current focus mode and the
project's strategic posture:

* During a stabilization push (``HARDEN``) the operator wants a tight,
  noise-free log — every additional line of tool output is friction.
* During an exploration cycle (``EXPLORE``) the operator wants to *see*
  what the model touched, with full context.
* When the operator collapses to a single ``focus:diff`` panel they
  have lots of vertical real estate; the body cap should expand. When
  they're in ``split`` mode the available rows are split 3 ways and
  the cap should shrink.

Slice 2 supplies a single deterministic resolver that maps these
signals to a 3-level density policy (``compact`` / ``balanced`` /
``verbose``), with three precedence steps:

  1. Explicit override (test hook / kwarg) — wins absolutely
  2. ``JARVIS_TOOL_RENDER_DENSITY={compact,balanced,verbose}`` env var
  3. Declarative ``(Posture, LayoutKind) -> DensityLevel`` table

Authority boundary
------------------

* §1 deterministic — pure mapping; no LLM, no I/O on the hot path
* §7 fail-closed — every resolution step has a documented fallback;
  unknown inputs degrade to ``BALANCED``, never raise
* §8 observable — every returned policy carries a ``provenance``
  string (e.g. ``"table:HARDEN×split"``, ``"env:verbose"``,
  ``"override:compact"``) so SSE / observability layers can explain
  why a particular density was chosen

DI cage
-------

This module imports:

* :class:`Posture` (closed enum from ``governance.posture``) — pure data
* layout-mode constants from :mod:`layout_controller` — pure constants

It deliberately does NOT import the *stateful* surfaces:

* ``posture_observer`` / ``posture_store`` — runtime hysteresis state
* the live ``LayoutController`` singleton

Production wiring of the live providers happens via lazy imports
inside :class:`DefaultPostureProvider` / :class:`DefaultLayoutProvider`
methods, so the substrate stays import-cheap and the Slice 5 AST pin
can mechanically prove the cage is intact.
"""
from __future__ import annotations

import dataclasses
import enum
import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Tuple, runtime_checkable

from backend.core.ouroboros.battle_test.layout_controller import (
    MODE_FLOW,
    MODE_SPLIT,
    is_focus_mode,
)
from backend.core.ouroboros.governance.posture import Posture

logger = logging.getLogger("Ouroboros.ToolRenderPolicy")


# ===========================================================================
# Schema + closed taxonomies
# ===========================================================================


TOOL_RENDER_POLICY_SCHEMA_VERSION: str = "tool_render_policy.v1"


_DENSITY_ENV_VAR: str = "JARVIS_TOOL_RENDER_DENSITY"


class DensityLevel(str, enum.Enum):
    """Closed 3-value density vocabulary.

    Extending requires a slice — operators consume these names via
    ``/policy`` REPL + observability surfaces.
    """

    COMPACT = "compact"      # header + summary only, no body block
    BALANCED = "balanced"    # bounded body (10 lines)
    VERBOSE = "verbose"      # generous body (30 lines), wider summaries

    @classmethod
    def coerce(cls, raw: object) -> Optional["DensityLevel"]:
        """Lenient parse — anything not recognized returns ``None``.
        NEVER raises."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return None


class LayoutKind(str, enum.Enum):
    """Closed 3-value layout-shape classification.

    The :class:`LayoutController` mode vocabulary is open
    (``focus:<region>`` family); :class:`LayoutKind` collapses the
    family into the structural shape that matters for density: how
    many other regions are competing for vertical space.
    """

    FLOW = "flow"      # single scrolling panel — full row budget
    SPLIT = "split"    # 3 regions sharing screen — tight row budget
    FOCUS = "focus"    # single region full-frame — generous budget


def classify_layout(mode: object) -> LayoutKind:
    """Classify any :class:`LayoutController` mode string into the
    3-kind taxonomy. NEVER raises — non-string / unknown input
    degrades to :data:`LayoutKind.FLOW` (the safe default that
    matches the controller's own fallback behavior)."""
    if not isinstance(mode, str):
        return LayoutKind.FLOW
    if mode == MODE_FLOW:
        return LayoutKind.FLOW
    if mode == MODE_SPLIT:
        return LayoutKind.SPLIT
    if is_focus_mode(mode):
        return LayoutKind.FOCUS
    return LayoutKind.FLOW


# ===========================================================================
# Frozen DensityPolicy + canonical level instances
# ===========================================================================


@dataclass(frozen=True)
class DensityPolicy:
    """Resolved render-density bundle. Frozen + hashable.

    Fields
    ------
    * ``level`` — the resolved :class:`DensityLevel`
    * ``max_body_lines`` — body line budget passed to
      :func:`tool_render_registry.render`. ``0`` → header+summary only.
    * ``max_summary_chars`` — soft cap on the body-summary line; the
      Slice 4 wiring layer truncates with ``…`` past this width.
    * ``provenance`` — operator-readable explanation of how this
      policy was chosen (``"override:compact"``, ``"env:verbose"``,
      or ``"table:HARDEN×split"``). Goes into the SSE event in
      Slice 4 / Slice 5.
    """

    level: DensityLevel
    max_body_lines: int
    max_summary_chars: int
    provenance: str
    schema_version: str = TOOL_RENDER_POLICY_SCHEMA_VERSION

    @property
    def show_body(self) -> bool:
        return self.max_body_lines > 0

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "max_body_lines": self.max_body_lines,
            "max_summary_chars": self.max_summary_chars,
            "provenance": self.provenance,
            "schema_version": self.schema_version,
        }


# Canonical templates — fields below are tuned for typical 80-col TTYs.
# The provenance slot is filled at resolution time via dataclasses.replace.
_COMPACT = DensityPolicy(
    level=DensityLevel.COMPACT,
    max_body_lines=0,
    max_summary_chars=60,
    provenance="",
)

_BALANCED = DensityPolicy(
    level=DensityLevel.BALANCED,
    max_body_lines=10,
    max_summary_chars=80,
    provenance="",
)

_VERBOSE = DensityPolicy(
    level=DensityLevel.VERBOSE,
    max_body_lines=30,
    max_summary_chars=120,
    provenance="",
)


_LEVEL_TO_TEMPLATE: Mapping[DensityLevel, DensityPolicy] = {
    DensityLevel.COMPACT: _COMPACT,
    DensityLevel.BALANCED: _BALANCED,
    DensityLevel.VERBOSE: _VERBOSE,
}


# ===========================================================================
# Declarative resolution table — Posture × LayoutKind → DensityLevel
# ===========================================================================
#
# Reading guide (rows = posture, columns = layout):
#
#                FLOW       SPLIT      FOCUS
#   HARDEN       compact    compact    balanced    ← stabilization: tight
#   CONSOLIDATE  balanced   compact    verbose     ← finishing: tight in split
#   MAINTAIN     balanced   compact    verbose     ← steady state
#   EXPLORE      verbose    balanced   verbose     ← discovery: show more
#
# Asymmetry rationale:
#   * HARDEN keeps the log spare regardless of layout — explicit
#     stabilization signal trumps operator real-estate
#   * EXPLORE treats SPLIT as a half-step (BALANCED) rather than
#     COMPACT, since the operator opted into the split for reason
#   * FOCUS always goes one step looser than FLOW (operator collapsed
#     to a single region — they want the body)
#
# This table IS the source of truth. The resolver reads it; nothing
# else does. If you want to change a cell, change it here.


_RESOLUTION_TABLE: Mapping[Tuple[Posture, LayoutKind], DensityLevel] = {
    (Posture.HARDEN, LayoutKind.FLOW): DensityLevel.COMPACT,
    (Posture.HARDEN, LayoutKind.SPLIT): DensityLevel.COMPACT,
    (Posture.HARDEN, LayoutKind.FOCUS): DensityLevel.BALANCED,
    (Posture.CONSOLIDATE, LayoutKind.FLOW): DensityLevel.BALANCED,
    (Posture.CONSOLIDATE, LayoutKind.SPLIT): DensityLevel.COMPACT,
    (Posture.CONSOLIDATE, LayoutKind.FOCUS): DensityLevel.VERBOSE,
    (Posture.MAINTAIN, LayoutKind.FLOW): DensityLevel.BALANCED,
    (Posture.MAINTAIN, LayoutKind.SPLIT): DensityLevel.COMPACT,
    (Posture.MAINTAIN, LayoutKind.FOCUS): DensityLevel.VERBOSE,
    (Posture.EXPLORE, LayoutKind.FLOW): DensityLevel.VERBOSE,
    (Posture.EXPLORE, LayoutKind.SPLIT): DensityLevel.BALANCED,
    (Posture.EXPLORE, LayoutKind.FOCUS): DensityLevel.VERBOSE,
}


# Defensive: at import time, prove the table covers every (posture, kind)
# pair. A missing cell would fall back silently to BALANCED at runtime —
# fine for safety, but we want it loud during development.
_EXPECTED_KEYS = frozenset(
    (p, k) for p in Posture for k in LayoutKind
)
_TABLE_KEYS = frozenset(_RESOLUTION_TABLE.keys())
_MISSING_KEYS = _EXPECTED_KEYS - _TABLE_KEYS
assert not _MISSING_KEYS, (  # noqa: S101 — intentional import-time guard
    f"_RESOLUTION_TABLE missing cells: {sorted(_MISSING_KEYS)}"
)


# ===========================================================================
# Env override — read once per call, never cached
# ===========================================================================


def read_env_override() -> Optional[DensityLevel]:
    """Read ``JARVIS_TOOL_RENDER_DENSITY`` and parse to a level.

    Returns ``None`` when unset, blank, or unrecognized — caller
    falls through to the table lookup. Logs a one-time warning
    (debug-level) on bad input rather than warning every call. NEVER
    raises."""
    raw = os.environ.get(_DENSITY_ENV_VAR, "")
    if not raw or not raw.strip():
        return None
    parsed = DensityLevel.coerce(raw)
    if parsed is None:
        logger.debug(
            "[ToolRenderPolicy] ignoring %s=%r — not in {%s}",
            _DENSITY_ENV_VAR, raw,
            ", ".join(m.value for m in DensityLevel),
        )
    return parsed


# ===========================================================================
# Resolution — the load-bearing function
# ===========================================================================


def resolve_density(
    posture: object,
    layout_mode: object,
    *,
    explicit_override: Optional[DensityLevel] = None,
    skip_env: bool = False,
) -> DensityPolicy:
    """Resolve a :class:`DensityPolicy` via 3-step precedence.

    Precedence (highest first):

      1. ``explicit_override`` kwarg — for test hooks and the
         ``/policy override`` REPL verb (Slice 5+ surface).
      2. ``JARVIS_TOOL_RENDER_DENSITY`` env var — operator override
         that survives across REPL sessions.
      3. ``(posture, layout_kind)`` table lookup — the "intelligent
         default" path that adapts to current operating context.

    Fallbacks (silent, observability-only):

      * ``posture`` not a :class:`Posture` member → treat as
        :data:`Posture.MAINTAIN` (steady-state default).
      * ``layout_mode`` not a recognized string →
        :data:`LayoutKind.FLOW` (most common shape).
      * Missing table cell → :data:`DensityLevel.BALANCED`.

    NEVER raises.
    """
    # Step 1 — explicit override
    if isinstance(explicit_override, DensityLevel):
        return _stamp(_LEVEL_TO_TEMPLATE[explicit_override],
                      f"override:{explicit_override.value}")

    # Step 2 — env var
    if not skip_env:
        env_level = read_env_override()
        if env_level is not None:
            return _stamp(_LEVEL_TO_TEMPLATE[env_level],
                          f"env:{env_level.value}")

    # Step 3 — table lookup with safe coercions
    posture_safe: Posture = (
        posture if isinstance(posture, Posture) else Posture.MAINTAIN
    )
    layout_kind = classify_layout(layout_mode)
    level = _RESOLUTION_TABLE.get(
        (posture_safe, layout_kind), DensityLevel.BALANCED,
    )
    return _stamp(
        _LEVEL_TO_TEMPLATE[level],
        f"table:{posture_safe.value}×{layout_kind.value}",
    )


def _stamp(template: DensityPolicy, provenance: str) -> DensityPolicy:
    """Return a copy of ``template`` with ``provenance`` filled in."""
    return dataclasses.replace(template, provenance=provenance)


# ===========================================================================
# Provider Protocols — DI seams for live wiring
# ===========================================================================


@runtime_checkable
class PostureProvider(Protocol):
    """Read the current strategic posture.

    Implementations:
      * :class:`DefaultPostureProvider` — production; reads
        ``posture_store.load_current()``.
      * Test fixtures construct a tiny stub class that returns a
        fixed value.
    """

    def current(self) -> Optional[Posture]:
        ...


@runtime_checkable
class LayoutModeProvider(Protocol):
    """Read the current :class:`LayoutController` mode string.

    Implementations:
      * :class:`DefaultLayoutModeProvider` — production; reads
        ``get_default_layout_controller().mode``.
      * Test fixtures inject a stub returning a fixed mode.
    """

    def current(self) -> Optional[str]:
        ...


def resolve_density_via_providers(
    posture_provider: PostureProvider,
    layout_provider: LayoutModeProvider,
    *,
    explicit_override: Optional[DensityLevel] = None,
    skip_env: bool = False,
) -> DensityPolicy:
    """Convenience wrapper — pulls the current state from providers
    and delegates to :func:`resolve_density`. NEVER raises (provider
    exceptions degrade to ``None``, which then falls back to the
    safe defaults inside ``resolve_density``)."""
    posture: Optional[Posture] = None
    try:
        posture = posture_provider.current()
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ToolRenderPolicy] posture_provider raised; treating as None",
            exc_info=True,
        )

    layout: Optional[str] = None
    try:
        layout = layout_provider.current()
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ToolRenderPolicy] layout_provider raised; treating as None",
            exc_info=True,
        )

    return resolve_density(
        posture, layout,
        explicit_override=explicit_override,
        skip_env=skip_env,
    )


# ===========================================================================
# Default production providers — lazy imports keep policy.py import-cheap
# ===========================================================================


class DefaultPostureProvider:
    """Reads via ``posture_store.load_current()``.

    Lazy import: the *stateful* runtime modules (``posture_store`` /
    ``posture_observer``) are imported inside :meth:`current` so
    that ``tool_render_policy`` itself imports cheaply and the
    Slice 5 AST cage pin can mechanically verify no top-level
    dependency on those surfaces.
    """

    def current(self) -> Optional[Posture]:
        # Lazy import — see DI cage docstring at module top.
        try:
            from backend.core.ouroboros.governance.posture_observer import (
                get_default_store,
            )
        except ImportError:
            return None
        try:
            store = get_default_store()
            reading = store.load_current()
            if reading is None:
                return None
            posture = getattr(reading, "posture", None)
            return posture if isinstance(posture, Posture) else None
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ToolRenderPolicy] DefaultPostureProvider failed",
                exc_info=True,
            )
            return None


class DefaultLayoutModeProvider:
    """Reads via :func:`layout_controller.get_default_layout_controller`."""

    def current(self) -> Optional[str]:
        try:
            from backend.core.ouroboros.battle_test.layout_controller import (
                get_default_layout_controller,
            )
        except ImportError:
            return None
        try:
            return get_default_layout_controller().mode
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ToolRenderPolicy] DefaultLayoutModeProvider failed",
                exc_info=True,
            )
            return None


__all__ = [
    "DefaultLayoutModeProvider",
    "DefaultPostureProvider",
    "DensityLevel",
    "DensityPolicy",
    "LayoutKind",
    "LayoutModeProvider",
    "PostureProvider",
    "TOOL_RENDER_POLICY_SCHEMA_VERSION",
    "classify_layout",
    "read_env_override",
    "resolve_density",
    "resolve_density_via_providers",
]
