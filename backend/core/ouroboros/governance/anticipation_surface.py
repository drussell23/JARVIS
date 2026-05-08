"""§38.11-C — Proactive intervention banners + Anticipatory
pre-fetch indicator (PRD v2.66 to v2.67, 2026-05-08).

Composes canonical sources only:
  * ``narrative_channel.NarrativeChannel`` — read API for the
    INTENT prose that the model already emits at op_started
    (§38.11-B precedent: never produce parallel prose).
  * ``ide_observability_stream`` SSE broker — single event
    ring; we register two new event types
    (``intervention_banner_raised`` + ``prefetch_scheduled``)
    in the canonical ``_VALID_EVENT_TYPES`` frozenset.
  * ``intent.signals.SignalSource`` — canonical sensor source
    identifiers (no parallel sensor enum).

Two surfaces in ONE substrate per §38.11.5a.5 single-canonical
discipline:

  1. **Intervention banner** — when a sensor enqueues an op
     that intervenes in operator's flow, surface a banner so
     the operator knows WHO decided this and WHY. Composes
     NarrativeChannel INTENT frame for the prose; this
     module never emits prose itself.

  2. **Anticipatory pre-fetch indicator** — when the PLAN
     phase / Venom tool loop schedules tool calls before
     GENERATE writes a patch, surface what files / searches
     are about to fire so the operator sees the organism's
     attention BEFORE the op produces any artifact.

§33 patterns invoked:

  * §33.1 graduation contract — master flag default-FALSE.
  * §33.3 naming-cage — ``continuity_repl.py`` precedent;
    ``anticipate_repl.py`` (sibling module) auto-discovers
    via §32.11 Slice 4 ``repl_dispatch_registry``.
  * §33.5 versioned artifact — frozen
    :class:`InterventionBannerEvent` + :class:`PrefetchEvent`
    carry ``schema_version``.

Authority asymmetry: this module has ZERO authority. It
NEVER calls orchestrator, NEVER mutates risk-tier, NEVER
publishes to provider chains. It only OBSERVES + RENDERS.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional, Tuple

logger = logging.getLogger(__name__)


ANTICIPATION_SURFACE_SCHEMA_VERSION: str = "anticipation_surface.1"


_ENV_MASTER = "JARVIS_ANTICIPATION_SURFACE_ENABLED"
_ENV_SUB_BANNERS = "JARVIS_ANTICIPATION_BANNERS_ENABLED"
_ENV_SUB_PREFETCH = "JARVIS_ANTICIPATION_PREFETCH_ENABLED"
_ENV_BANNER_RING_SIZE = "JARVIS_ANTICIPATION_BANNER_RING_SIZE"
_ENV_PREFETCH_RING_SIZE = "JARVIS_ANTICIPATION_PREFETCH_RING_SIZE"

_DEFAULT_BANNER_RING_SIZE = 20
_DEFAULT_PREFETCH_RING_SIZE = 30
_MIN_RING = 4
_MAX_RING = 256


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class BannerKind(str, enum.Enum):
    """Closed 4-value vocabulary for proactive interventions.

    Each value maps to a distinct sensor / autonomy origin.
    Closed taxonomy — adding a new kind requires a slice.
    """

    SENSOR_INTERVENTION = "sensor_intervention"  # generic sensor-fired op
    PROACTIVE_CURIOSITY = "proactive_curiosity"  # ProactiveExplorationSensor
    CAPABILITY_GAP = "capability_gap"            # CapabilityGapSensor
    OPPORTUNITY = "opportunity"                  # OpportunityMinerSensor

    @classmethod
    def coerce(cls, raw: object) -> "BannerKind":
        """Lenient parse — anything not recognized becomes
        :data:`SENSOR_INTERVENTION`. NEVER raises."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.SENSOR_INTERVENTION


class PrefetchKind(str, enum.Enum):
    """Closed 5-value vocabulary for pre-fetch tool calls."""

    READ_FILE = "read_file"
    SEARCH_CODE = "search_code"
    GET_CALLERS = "get_callers"
    GLOB_FILES = "glob_files"
    OTHER = "other"

    @classmethod
    def coerce(cls, raw: object) -> "PrefetchKind":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.OTHER


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class InterventionBannerEvent:
    """One proactive intervention banner.

    Frozen + hashable. Carries schema_version per §33.5
    versioned-artifact contract.
    """

    banner_kind: BannerKind
    signal_source: str = ""        # canonical SignalSource value
    summary: str = ""              # short one-liner for banner
    op_id: str = ""                # orchestrator op id (may be empty)
    risk_tier_label: str = ""      # SAFE_AUTO / NOTIFY_APPLY / ...
    queued_at_unix: float = field(default_factory=time.time)
    schema_version: str = ANTICIPATION_SURFACE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "banner_kind": self.banner_kind.value,
            "signal_source": self.signal_source,
            "summary": self.summary,
            "op_id": self.op_id,
            "risk_tier_label": self.risk_tier_label,
            "queued_at_unix": self.queued_at_unix,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PrefetchEvent:
    """One anticipatory pre-fetch tool call."""

    op_id: str
    prefetch_kind: PrefetchKind
    tool_name: str = ""
    arg_summary: str = ""           # short, redacted
    scheduled_at_unix: float = field(default_factory=time.time)
    schema_version: str = ANTICIPATION_SURFACE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "op_id": self.op_id,
            "prefetch_kind": self.prefetch_kind.value,
            "tool_name": self.tool_name,
            "arg_summary": self.arg_summary,
            "scheduled_at_unix": self.scheduled_at_unix,
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Master / sub-flag helpers
# ===========================================================================


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def banners_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_BANNERS, default=True)


def prefetch_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_PREFETCH, default=True)


def _read_ring_size(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    if n < _MIN_RING:
        return _MIN_RING
    if n > _MAX_RING:
        return _MAX_RING
    return n


# ===========================================================================
# AnticipationSurface — singleton, thread-safe
# ===========================================================================


class AnticipationSurface:
    """Bounded ring of recent banners + pre-fetches.

    Composes NarrativeChannel INTENT frames (read-only) when
    rendering banners; never produces prose itself.
    """

    def __init__(self) -> None:
        self._banners: Deque[InterventionBannerEvent] = deque(
            maxlen=_read_ring_size(
                _ENV_BANNER_RING_SIZE,
                _DEFAULT_BANNER_RING_SIZE,
            ),
        )
        self._prefetches: Deque[PrefetchEvent] = deque(
            maxlen=_read_ring_size(
                _ENV_PREFETCH_RING_SIZE,
                _DEFAULT_PREFETCH_RING_SIZE,
            ),
        )
        self._lock = threading.RLock()

    # ---- record API (best-effort; NEVER raises) ----------------------

    def record_banner(
        self, event: InterventionBannerEvent,
    ) -> bool:
        if not banners_enabled():
            return False
        try:
            with self._lock:
                self._banners.append(event)
            _publish_banner_event(event)
            return True
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "anticipation: record_banner failed",
                exc_info=True,
            )
            return False

    def record_prefetch(self, event: PrefetchEvent) -> bool:
        if not prefetch_enabled():
            return False
        try:
            with self._lock:
                self._prefetches.append(event)
            _publish_prefetch_event(event)
            return True
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "anticipation: record_prefetch failed",
                exc_info=True,
            )
            return False

    # ---- read API (pure; NEVER raises) -------------------------------

    def recent_banners(
        self, *, limit: int = 8,
    ) -> Tuple[InterventionBannerEvent, ...]:
        try:
            n = max(1, min(int(limit), _MAX_RING))
        except (TypeError, ValueError):
            n = 8
        with self._lock:
            items = list(self._banners)
        if n >= len(items):
            return tuple(items)
        return tuple(items[-n:])

    def recent_prefetches(
        self, *, limit: int = 8,
    ) -> Tuple[PrefetchEvent, ...]:
        try:
            n = max(1, min(int(limit), _MAX_RING))
        except (TypeError, ValueError):
            n = 8
        with self._lock:
            items = list(self._prefetches)
        if n >= len(items):
            return tuple(items)
        return tuple(items[-n:])

    def reset_for_tests(self) -> None:
        with self._lock:
            self._banners.clear()
            self._prefetches.clear()


# ---- module singleton --------------------------------------------------

_default_surface: Optional[AnticipationSurface] = None
_singleton_lock = threading.Lock()


def get_default_surface() -> AnticipationSurface:
    global _default_surface
    with _singleton_lock:
        if _default_surface is None:
            _default_surface = AnticipationSurface()
        return _default_surface


def reset_surface_for_tests() -> None:
    global _default_surface
    with _singleton_lock:
        if _default_surface is not None:
            _default_surface.reset_for_tests()
        _default_surface = None


# ===========================================================================
# Producer-bridge helpers (§33.2) — best-effort, NEVER raises
# ===========================================================================


def emit_banner(
    *,
    banner_kind: object,
    signal_source: str = "",
    summary: str = "",
    op_id: str = "",
    risk_tier_label: str = "",
) -> bool:
    """Sensor-side hook: register an intervention banner.

    Lazy-importable from intake/sensors so a failure to import
    this module never breaks signal ingestion.
    """
    try:
        ev = InterventionBannerEvent(
            banner_kind=BannerKind.coerce(banner_kind),
            signal_source=str(signal_source or ""),
            summary=_truncate(summary, max_chars=200),
            op_id=str(op_id or ""),
            risk_tier_label=str(risk_tier_label or ""),
        )
        return get_default_surface().record_banner(ev)
    except Exception:  # noqa: BLE001
        return False


def emit_prefetch(
    *,
    op_id: str,
    prefetch_kind: object,
    tool_name: str = "",
    arg_summary: str = "",
) -> bool:
    """PlanGenerator/Venom-side hook: register a pre-fetch
    tool call about to fire."""
    try:
        ev = PrefetchEvent(
            op_id=str(op_id or ""),
            prefetch_kind=PrefetchKind.coerce(prefetch_kind),
            tool_name=str(tool_name or ""),
            arg_summary=_truncate(arg_summary, max_chars=120),
        )
        return get_default_surface().record_prefetch(ev)
    except Exception:  # noqa: BLE001
        return False


def _truncate(s: object, *, max_chars: int) -> str:
    try:
        text = str(s or "")
    except Exception:  # noqa: BLE001
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


# ===========================================================================
# SSE composition — uses canonical broker ONLY
# ===========================================================================


def _publish_banner_event(event: InterventionBannerEvent) -> None:
    """Publish to canonical SSE broker. Best-effort; NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_INTERVENTION_BANNER_RAISED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_INTERVENTION_BANNER_RAISED,
            event.op_id or event.signal_source or "",
            event.to_dict(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "anticipation: SSE banner publish failed",
            exc_info=True,
        )


def _publish_prefetch_event(event: PrefetchEvent) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PREFETCH_SCHEDULED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_PREFETCH_SCHEDULED,
            event.op_id or "",
            event.to_dict(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "anticipation: SSE prefetch publish failed",
            exc_info=True,
        )


# ===========================================================================
# NarrativeChannel composition — INTENT prose lookup (canonical)
# ===========================================================================


def lookup_intent_prose(*, op_id: str) -> str:
    """Compose canonical NarrativeChannel INTENT frame for ``op_id``.

    Returns the empty string when:
      * op_id is empty
      * no INTENT frame exists for op_id
      * NarrativeChannel is unavailable

    NEVER raises. The §38.11-B/C contract: this module
    NEVER produces prose; it only LOOKS UP prose that the
    canonical NarrativeChannel substrate already produced.
    """
    if not op_id:
        return ""
    try:
        from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
            FrameState,
            NarrativeKind,
            get_default_channel,
        )
    except Exception:  # noqa: BLE001
        return ""
    try:
        ch = get_default_channel()
        frames = ch.frames_by_op_kind(
            op_id=str(op_id),
            kind=NarrativeKind.INTENT,
            states=(FrameState.COMMITTED,),
        )
    except Exception:  # noqa: BLE001
        return ""
    if not frames:
        return ""
    # Return the most-recent COMMITTED frame's prose, truncated.
    return _truncate(frames[-1].prose, max_chars=180)


# ===========================================================================
# Renderers — pure; NEVER raise
# ===========================================================================


def format_intervention_banner_panel(
    *,
    banners: Optional[
        Tuple[InterventionBannerEvent, ...]
    ] = None,
    limit: int = 5,
) -> str:
    if not banners_enabled():
        return ""
    if banners is None:
        banners = get_default_surface().recent_banners(
            limit=limit,
        )
    if not banners:
        return ""
    parts = ["[bright_yellow]🌐 Recently queued by autonomy:[/]"]
    for b in banners[-limit:]:
        glyph = {
            BannerKind.SENSOR_INTERVENTION: "🌐",
            BannerKind.PROACTIVE_CURIOSITY: "🔭",
            BannerKind.CAPABILITY_GAP: "🧩",
            BannerKind.OPPORTUNITY: "💡",
        }.get(b.banner_kind, "•")
        risk = (
            f" [{b.risk_tier_label}]"
            if b.risk_tier_label else ""
        )
        src = (
            f" ({b.signal_source})"
            if b.signal_source else ""
        )
        prose_extra = ""
        if b.op_id:
            prose = lookup_intent_prose(op_id=b.op_id)
            if prose:
                prose_extra = f" — 💭 {prose}"
        parts.append(
            f"  {glyph} {b.summary or '(no summary)'}{src}{risk}"
            f"{prose_extra}"
        )
    return "\n".join(parts)


def format_prefetch_indicator(
    *,
    prefetches: Optional[Tuple[PrefetchEvent, ...]] = None,
    limit: int = 5,
) -> str:
    if not prefetch_enabled():
        return ""
    if prefetches is None:
        prefetches = get_default_surface().recent_prefetches(
            limit=limit,
        )
    if not prefetches:
        return ""
    glyphs = {
        PrefetchKind.READ_FILE: "📄",
        PrefetchKind.SEARCH_CODE: "🔍",
        PrefetchKind.GET_CALLERS: "🔗",
        PrefetchKind.GLOB_FILES: "🗂",
        PrefetchKind.OTHER: "•",
    }
    parts = ["[bright_cyan]🔍 Pre-fetching:[/]"]
    for p in prefetches[-limit:]:
        g = glyphs.get(p.prefetch_kind, "•")
        arg = f" {p.arg_summary}" if p.arg_summary else ""
        parts.append(f"  {g} {p.tool_name}{arg}")
    return "\n".join(parts)


def format_anticipation_panel() -> str:
    """Composite render: banners (above) + prefetch (below)."""
    if not master_enabled():
        return ""
    sections = []
    bp = format_intervention_banner_panel()
    if bp:
        sections.append(bp)
    pf = format_prefetch_indicator()
    if pf:
        sections.append(pf)
    return "\n\n".join(sections)


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§38.11-C anticipation surface master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_SUB_BANNERS, "bool",
            "Enable proactive intervention banners surface "
            "(default TRUE when master on).",
            "true",
        ),
        (
            _ENV_SUB_PREFETCH, "bool",
            "Enable anticipatory pre-fetch indicator surface "
            "(default TRUE when master on).",
            "true",
        ),
        (
            _ENV_BANNER_RING_SIZE, "int",
            "Bounded ring size for intervention banners "
            "(default 20; clamped 4..256).",
            "20",
        ),
        (
            _ENV_PREFETCH_RING_SIZE, "int",
            "Bounded ring size for pre-fetch events "
            "(default 30; clamped 4..256).",
            "30",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name,
                type=typ,
                category="ux",
                description=desc,
                example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "anticipation_surface.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ===========================================================================
# AST pins — §33.1 + authority asymmetry + closed taxonomies +
# canonical-source composition.
# ===========================================================================


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    # ---- Pin 1: master_default_false -------------------------------------

    def _master_default_false(tree: ast.AST, src: str):
        """The master-flag helper MUST default to ``False``.
        Drift would silently flip §33.1 graduation."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                # Walk body looking for `_flag(_ENV_MASTER, default=False)`
                ok = False
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
                                ok = True
                if not ok:
                    return [
                        "master_enabled() must call _flag(...) "
                        "with default=False"
                    ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11c_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — master flag stays "
            "default-False until evidence ladder closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "anticipation_surface.py"
        ),
        validate=_master_default_false,
    ))

    # ---- Pin 2: authority_asymmetry --------------------------------------

    def _authority_asymmetry(tree: ast.AST, src: str):
        """This module has ZERO orchestrator/risk-tier authority.
        Imports of those modules indicate authority leak."""
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11c_authority_asymmetry"
        ),
        description=(
            "Substrate purity — module composes canonical "
            "read APIs only; no orchestrator/risk-tier "
            "imports."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "anticipation_surface.py"
        ),
        validate=_authority_asymmetry,
    ))

    # ---- Pin 3: banner_kind_taxonomy_4_values ----------------------------

    def _banner_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "BannerKind"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "SENSOR_INTERVENTION",
                    "PROACTIVE_CURIOSITY",
                    "CAPABILITY_GAP",
                    "OPPORTUNITY",
                }
                missing = expected - names
                if missing:
                    return [
                        f"BannerKind missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["BannerKind class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11c_banner_kind_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value BannerKind taxonomy — adding a "
            "new kind requires a slice."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "anticipation_surface.py"
        ),
        validate=_banner_taxonomy,
    ))

    # ---- Pin 4: prefetch_kind_taxonomy_5_values --------------------------

    def _prefetch_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PrefetchKind"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "READ_FILE",
                    "SEARCH_CODE",
                    "GET_CALLERS",
                    "GLOB_FILES",
                    "OTHER",
                }
                missing = expected - names
                if missing:
                    return [
                        f"PrefetchKind missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["PrefetchKind class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11c_prefetch_kind_taxonomy_5_values"
        ),
        description=(
            "Closed 5-value PrefetchKind taxonomy."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "anticipation_surface.py"
        ),
        validate=_prefetch_taxonomy,
    ))

    # ---- Pin 5: composes_canonical_narrative_channel ---------------------

    def _composes_narrative(tree: ast.AST, src: str):
        """Banner prose lookup MUST compose canonical
        NarrativeChannel — no parallel prose substrate.
        Bytes-pin via substring search."""
        if "battle_test.narrative_channel" not in src:
            return [
                "must lazy-import battle_test.narrative_channel "
                "for INTENT prose lookup"
            ]
        if "NarrativeKind.INTENT" not in src:
            return [
                "must reference NarrativeKind.INTENT "
                "(canonical kind for proactive prose)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11c_composes_canonical_narrative_channel"
        ),
        description=(
            "Banner prose lookup composes canonical "
            "NarrativeChannel INTENT frames; module never "
            "produces parallel prose."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "anticipation_surface.py"
        ),
        validate=_composes_narrative,
    ))

    return pins


__all__ = [
    "ANTICIPATION_SURFACE_SCHEMA_VERSION",
    "BannerKind",
    "PrefetchKind",
    "InterventionBannerEvent",
    "PrefetchEvent",
    "AnticipationSurface",
    "master_enabled",
    "banners_enabled",
    "prefetch_enabled",
    "get_default_surface",
    "reset_surface_for_tests",
    "emit_banner",
    "emit_prefetch",
    "lookup_intent_prose",
    "format_intervention_banner_panel",
    "format_prefetch_indicator",
    "format_anticipation_panel",
    "register_flags",
    "register_shipped_invariants",
]
