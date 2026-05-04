"""ContextualHelpResolver — browseable + contextual help surface.

Slice 6 of the RenderConductor arc (Wave 4 #1). Closes Gap #6: O+V's
``/help`` is verb-driven (operator must know what to ask); CC's help
is browseable + contextual (current state ranks suggestions). Slice 6
adds the contextual layer over the existing typed registries —
:class:`VerbRegistry` (help_dispatcher), :class:`FlagRegistry`
(flag_registry), and :class:`KeyActionRegistry` (Slice 4) — without
duplicating any storage.

Architectural pillars:

  1. **Pure aggregation, zero new storage** — the resolver reads from
     three existing typed registries via lazy import. No new in-memory
     directory; no new disk artifact. Each query is a fresh read,
     so live registry mutations (operator-registered verbs / flags
     mid-session) surface immediately.
  2. **Closed-taxonomy HelpKind** — ``{VERB, FLAG, KEY_ACTION, DOC,
     TIP}`` AST-pinned. Adding a kind requires coordinated registry
     update. Each kind has a dedicated source-of-truth registry.
  3. **No hardcoded weights** — the substring / posture / recent-verb /
     phase-affinity weights default in-code but are overrideable via
     ``JARVIS_HELP_RANKING_WEIGHTS`` (JSON). Operators tune ranking
     without code change.
  4. **Read-only** — :class:`ContextualHelpResolver` never mutates any
     registry. Posture / phase / recent-verbs are passed in as call-
     time context (operator registry-driven, not module-global state).
     Pure function semantics: same inputs → same ranking.
  5. **Defensive everywhere** — every method swallows exceptions and
     returns a degraded result (empty page, default ranking) instead
     of raising. A registry that raises mid-iteration cannot break
     the resolver; a malformed flag overlay falls back to defaults.
  6. **Pagination by typed primitive** — :class:`HelpPage` carries
     ``offset / limit / total / has_more`` so backends paginate
     deterministically. Operators page via repeated ``HELP_OPEN``
     events with offset metadata; the conductor's MODAL region
     surfaces them via :class:`EventKind.MODAL_PROMPT`.

Authority invariants (AST-pinned via ``register_shipped_invariants``):

  * No imports of ``rich`` / ``rich.*``.
  * No top-level imports of authority modules OR registries
    (``help_dispatcher`` / ``flag_registry`` / ``posture`` / etc.) —
    each consult is via lazy import inside the resolver.
  * :class:`HelpKind` member set is the documented closed set.
  * :class:`HelpEntry` field set is closed.
  * :class:`HelpPage` field set is closed.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switches:

  * ``JARVIS_CONTEXTUAL_HELP_ENABLED`` — master gate. Default false
    at Slice 6; graduates with conductor at Slice 7.
  * ``JARVIS_HELP_RANKING_WEIGHTS`` — JSON object overlay on the
    default scoring weights. Empty / missing falls through.
  * ``JARVIS_HELP_PAGE_SIZE`` — default page size for resolve() when
    ``limit`` arg omitted.
"""
from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


RENDER_HELP_SCHEMA_VERSION: str = "render_help.1"


_FLAG_CONTEXTUAL_HELP_ENABLED = "JARVIS_CONTEXTUAL_HELP_ENABLED"
_FLAG_HELP_RANKING_WEIGHTS = "JARVIS_HELP_RANKING_WEIGHTS"
_FLAG_HELP_PAGE_SIZE = "JARVIS_HELP_PAGE_SIZE"


# ---------------------------------------------------------------------------
# Flag accessors
# ---------------------------------------------------------------------------


def _get_registry() -> Any:
    try:
        from backend.core.ouroboros.governance import flag_registry as _fr
        return _fr.ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def is_enabled() -> bool:
    """Master gate. Graduated default ``true`` at Slice 7 follow-up
    #4 — ContextualHelpResolver returns ranked pages from the typed
    registry aggregation; ``?`` keypress (Slice 4 binding default)
    publishes a MODAL_PROMPT page. Hot-revert via
    ``JARVIS_CONTEXTUAL_HELP_ENABLED=false`` → ``resolve`` returns an
    empty page (resolver stays alive so callers can hold a reference;
    only the rendering surface is gated)."""
    reg = _get_registry()
    if reg is None:
        return True
    return reg.get_bool(_FLAG_CONTEXTUAL_HELP_ENABLED, default=True)


def default_page_size() -> int:
    reg = _get_registry()
    if reg is None:
        return 10
    return max(1, reg.get_int(_FLAG_HELP_PAGE_SIZE, default=10, minimum=1))


# Default scoring weights — overrideable via JARVIS_HELP_RANKING_WEIGHTS.
_DEFAULT_WEIGHTS: Mapping[str, float] = {
    "substring_name":          10.0,  # query is substring of entry name
    "substring_one_line":       5.0,  # ... of one_line / description
    "substring_body":           2.0,  # ... of full body / help_text
    "posture_critical":         5.0,  # entry tagged CRITICAL for posture
    "posture_relevant":         2.0,  # ... tagged RELEVANT
    "recent_verb_proximity":    3.0,  # entry name is in recent_verbs
    "phase_affinity":           2.0,  # entry's phase tag matches current
    "kind_verb_baseline":       1.0,  # baseline for VERB entries
    "kind_flag_baseline":       0.5,  # baseline for FLAG entries
    "kind_key_action_baseline": 1.5,  # baseline for KEY_ACTION entries
}


def ranking_weights() -> Mapping[str, float]:
    """Resolved scoring weights. Operator overlay layered on defaults."""
    out: Dict[str, float] = dict(_DEFAULT_WEIGHTS)
    reg = _get_registry()
    if reg is None:
        return out
    raw = reg.get_json(_FLAG_HELP_RANKING_WEIGHTS, default=None)
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            logger.debug(
                "[render_help] non-numeric weight override for %s: %r",
                k, v,
            )
            continue
    return out


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class HelpKind(str, enum.Enum):
    """Closed taxonomy of help-entry kinds.

    Each kind has a dedicated source registry — :class:`VerbRegistry`
    for VERB, :class:`FlagRegistry` for FLAG, :class:`KeyActionRegistry`
    for KEY_ACTION. DOC + TIP are reserved for future static-content
    + heuristic-tip injection (operator-curated, not registry-derived).
    """

    VERB = "VERB"
    FLAG = "FLAG"
    KEY_ACTION = "KEY_ACTION"
    DOC = "DOC"
    TIP = "TIP"


# ---------------------------------------------------------------------------
# HelpEntry — frozen typed primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HelpEntry:
    """One ranked help entry. Frozen + hashable — safe to fan out to
    multiple backends + paginate without defensive copies.

    Field semantics (closed-taxonomy field set, AST-pinned):

      * ``kind`` — :class:`HelpKind` rendering category
      * ``name`` — canonical identifier (verb name, flag name, action)
      * ``one_line`` — short description for index views
      * ``body`` — full help text for detail views (may be empty)
      * ``source_module`` — origin file (for /help X "see also" links)
      * ``score`` — final ranking score (higher = more relevant)
    """

    kind: HelpKind
    name: str
    one_line: str
    body: str = ""
    source_module: str = ""
    score: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(
                f"HelpEntry.name must be non-empty string, got {self.name!r}"
            )
        if not isinstance(self.one_line, str):
            raise ValueError(
                f"HelpEntry.one_line must be string, got {self.one_line!r}"
            )

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "schema_version": RENDER_HELP_SCHEMA_VERSION,
            "entry_kind": self.kind.value,
            "name": self.name,
            "one_line": self.one_line,
            "body": self.body,
            "source_module": self.source_module,
            "score": self.score,
        }


# ---------------------------------------------------------------------------
# HelpPage — paginated result wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HelpPage:
    """One paginated slice of ranked help entries. Frozen.

    ``has_more`` is derived: ``offset + len(entries) < total``.
    Operators page via repeated HELP_OPEN events with incremented
    offset until ``has_more`` is ``False``."""

    entries: Tuple[HelpEntry, ...]
    offset: int
    limit: int
    total: int

    def __post_init__(self) -> None:
        if not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError(
                f"HelpPage.offset must be non-negative int, "
                f"got {self.offset!r}"
            )
        if not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError(
                f"HelpPage.limit must be >=1, got {self.limit!r}"
            )
        if not isinstance(self.total, int) or self.total < 0:
            raise ValueError(
                f"HelpPage.total must be non-negative int, "
                f"got {self.total!r}"
            )

    @property
    def has_more(self) -> bool:
        return (self.offset + len(self.entries)) < self.total

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "schema_version": RENDER_HELP_SCHEMA_VERSION,
            "page_kind": "help_page",
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "has_more": self.has_more,
            "entries": [e.to_metadata() for e in self.entries],
        }


# ---------------------------------------------------------------------------
# ContextualHelpResolver — read-only ranking over typed registries
# ---------------------------------------------------------------------------


class ContextualHelpResolver:
    """Aggregates VerbRegistry + FlagRegistry + KeyActionRegistry into
    a single ranked help index.

    Stateless beyond its own threading.Lock for cache invalidation.
    Each :meth:`resolve` is a fresh read of the underlying registries —
    operator-registered verbs / flags mid-session surface immediately.

    Scoring (each weight from :func:`ranking_weights`):

      * substring matches in name / one_line / body
      * posture-relevance for FLAG entries (consults FlagRegistry's
        ``relevant_to_posture(posture)`` index)
      * recent-verb proximity (entry name in ``recent_verbs`` boosts)
      * phase-affinity (entry's source_module path contains current
        phase string — heuristic for "verbs about CURRENT phase win")
      * kind baseline (different default weight per HelpKind so VERB
        beats FLAG beats DOC at equal substring score)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def resolve(
        self,
        query: str = "",
        *,
        offset: int = 0,
        limit: Optional[int] = None,
        posture: Optional[str] = None,
        current_phase: Optional[str] = None,
        recent_verbs: Tuple[str, ...] = (),
    ) -> HelpPage:
        """Resolve a help query. Returns a paginated :class:`HelpPage`.

        ``query`` may be empty (return all entries ranked by context).
        ``posture`` / ``current_phase`` / ``recent_verbs`` are
        operator-derived context — caller is responsible for sourcing
        them (e.g. from PostureStore + comm_protocol + REPL history).
        """
        if not is_enabled():
            return HelpPage(
                entries=(), offset=0,
                limit=limit if limit is not None else default_page_size(),
                total=0,
            )
        page_limit = (
            limit if (isinstance(limit, int) and limit > 0)
            else default_page_size()
        )
        try:
            candidates = self._gather_candidates(posture=posture)
            scored = self._rank(
                candidates,
                query=query,
                posture=posture,
                current_phase=current_phase,
                recent_verbs=recent_verbs,
            )
            total = len(scored)
            start = max(0, int(offset))
            end = start + page_limit
            page_entries = tuple(scored[start:end])
            return HelpPage(
                entries=page_entries,
                offset=start,
                limit=page_limit,
                total=total,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[render_help] resolve failed", exc_info=True,
            )
            return HelpPage(
                entries=(), offset=0, limit=page_limit, total=0,
            )

    # -- internals -----------------------------------------------------

    def _gather_candidates(
        self, *, posture: Optional[str],
    ) -> List[HelpEntry]:
        """Pull every registered verb / flag / key-action into a flat
        list. Lazy imports keep render_help free of top-level coupling
        to the registries. NEVER raises — partial failures degrade to
        the registries that DID load."""
        entries: List[HelpEntry] = []
        # -- VerbRegistry --------------------------------------------
        try:
            from backend.core.ouroboros.governance import (
                help_dispatcher as _hd,
            )
            verbs = _hd.get_default_verb_registry().list_all()
            for v in verbs:
                try:
                    entries.append(HelpEntry(
                        kind=HelpKind.VERB,
                        name=str(getattr(v, "name", "") or ""),
                        one_line=str(getattr(v, "one_line", "") or ""),
                        body=v.resolve_help() if hasattr(v, "resolve_help")
                        else "",
                        source_module="help_dispatcher",
                    ))
                except Exception:  # noqa: BLE001 — defensive
                    continue
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[render_help] verb registry unavailable", exc_info=True,
            )
        # -- FlagRegistry --------------------------------------------
        try:
            from backend.core.ouroboros.governance import (
                flag_registry as _fr,
            )
            reg = _fr.ensure_seeded()
            flag_specs = reg.list_all()
            for spec in flag_specs:
                try:
                    entries.append(HelpEntry(
                        kind=HelpKind.FLAG,
                        name=str(getattr(spec, "name", "") or ""),
                        one_line=str(getattr(spec, "description", "") or "")[:120],
                        body=str(getattr(spec, "description", "") or ""),
                        source_module=str(
                            getattr(spec, "source_file", "") or ""
                        ),
                    ))
                except Exception:  # noqa: BLE001 — defensive
                    continue
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[render_help] flag registry unavailable", exc_info=True,
            )
        # -- KeyActionRegistry (Slice 4) -----------------------------
        try:
            from backend.core.ouroboros.governance import key_input as _ki
            ctrl = _ki.get_input_controller()
            if ctrl is not None:
                bindings = _ki.resolve_bindings()
                for key_name, action in bindings.items():
                    try:
                        entries.append(HelpEntry(
                            kind=HelpKind.KEY_ACTION,
                            name=f"{key_name.value}",
                            one_line=(
                                f"Bound to action {action.value} "
                                f"(keyboard shortcut)"
                            ),
                            body=(
                                f"Pressing {key_name.value} fires "
                                f"the {action.value} action."
                            ),
                            source_module="key_input",
                        ))
                    except Exception:  # noqa: BLE001 — defensive
                        continue
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[render_help] key input registry unavailable",
                exc_info=True,
            )
        # Posture filtering for FLAG entries — boost critical/relevant
        # via ``relevant_to_posture`` lookup. We do this in _rank to
        # keep _gather_candidates pure-collection.
        return entries

    def _posture_relevance_set(
        self, posture: Optional[str],
    ) -> Tuple[FrozenSet[str], FrozenSet[str]]:
        """Return (critical_flag_names, relevant_flag_names) for the
        posture. Returns empty frozensets when posture is None or the
        registry isn't available."""
        if not posture:
            return frozenset(), frozenset()
        try:
            from backend.core.ouroboros.governance import (
                flag_registry as _fr,
            )
            reg = _fr.ensure_seeded()
            critical = reg.relevant_to_posture(
                posture, min_relevance=_fr.Relevance.CRITICAL,
            )
            all_relevant = reg.relevant_to_posture(
                posture, min_relevance=_fr.Relevance.RELEVANT,
            )
            crit_names = frozenset(s.name for s in critical)
            rel_names = frozenset(
                s.name for s in all_relevant
            ) - crit_names
            return crit_names, rel_names
        except Exception:  # noqa: BLE001 — defensive
            return frozenset(), frozenset()

    def _rank(
        self,
        candidates: List[HelpEntry],
        *,
        query: str,
        posture: Optional[str],
        current_phase: Optional[str],
        recent_verbs: Tuple[str, ...],
    ) -> List[HelpEntry]:
        """Score and sort candidates. Higher score wins; ties broken
        by name (stable alphabetical)."""
        weights = ranking_weights()
        crit_flags, rel_flags = self._posture_relevance_set(posture)
        recent_set = frozenset(
            v.strip() for v in recent_verbs if isinstance(v, str)
        )
        phase_norm = (current_phase or "").strip().upper()
        q_norm = (query or "").strip().lower()

        scored: List[HelpEntry] = []
        for entry in candidates:
            score = self._kind_baseline(entry, weights)
            name_lower = entry.name.lower()
            one_line_lower = entry.one_line.lower()
            body_lower = entry.body.lower() if entry.body else ""

            if q_norm:
                if q_norm in name_lower:
                    score += float(weights.get("substring_name", 0.0))
                if q_norm in one_line_lower:
                    score += float(weights.get("substring_one_line", 0.0))
                if body_lower and q_norm in body_lower:
                    score += float(weights.get("substring_body", 0.0))

            if entry.kind is HelpKind.FLAG:
                if entry.name in crit_flags:
                    score += float(weights.get("posture_critical", 0.0))
                elif entry.name in rel_flags:
                    score += float(weights.get("posture_relevant", 0.0))

            if recent_set and entry.name in recent_set:
                score += float(weights.get("recent_verb_proximity", 0.0))

            if phase_norm and entry.body:
                # Phase affinity: entry mentions phase name in body.
                if phase_norm in entry.body.upper():
                    score += float(weights.get("phase_affinity", 0.0))

            # Construct the scored copy (frozen — replace via new instance).
            scored.append(HelpEntry(
                kind=entry.kind, name=entry.name, one_line=entry.one_line,
                body=entry.body, source_module=entry.source_module,
                score=score,
            ))
        scored.sort(key=lambda e: (-e.score, e.name.lower()))
        return scored

    def _kind_baseline(
        self, entry: HelpEntry, weights: Mapping[str, float],
    ) -> float:
        if entry.kind is HelpKind.VERB:
            return float(weights.get("kind_verb_baseline", 0.0))
        if entry.kind is HelpKind.FLAG:
            return float(weights.get("kind_flag_baseline", 0.0))
        if entry.kind is HelpKind.KEY_ACTION:
            return float(weights.get("kind_key_action_baseline", 0.0))
        return 0.0


# ---------------------------------------------------------------------------
# publish_help_panel — producer-side helper
# ---------------------------------------------------------------------------


def publish_help_panel(
    page: HelpPage,
    *,
    source_module: str = "render_help.publish_help_panel",
) -> bool:
    """Publish a MODAL_PROMPT event carrying the help page.

    Backends route the event to the MODAL region. The page metadata
    contains every entry plus pagination state so the backend can
    render a paginated list without re-querying the resolver.
    """
    try:
        from backend.core.ouroboros.governance.render_conductor import (
            ColorRole,
            EventKind,
            RegionKind,
            RenderEvent,
            get_render_conductor,
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    conductor = get_render_conductor()
    if conductor is None:
        return False
    try:
        # Render a simple text body for backends that don't decode the
        # metadata payload — one line per entry.
        body_lines: List[str] = []
        for entry in page.entries:
            line = f"{entry.kind.value:<10} {entry.name:<40} {entry.one_line}"
            body_lines.append(line)
        body = "\n".join(body_lines)
        event = RenderEvent(
            kind=EventKind.MODAL_PROMPT,
            region=RegionKind.MODAL,
            role=ColorRole.CONTENT,
            content=body,
            source_module=source_module,
            metadata=page.to_metadata(),
        )
        conductor.publish(event)
        return True
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_help] publish_help_panel failed", exc_info=True,
        )
        return False


def publish_help_dismiss(
    *, source_module: str = "render_help.publish_help_dismiss",
) -> bool:
    """Publish a MODAL_DISMISS event to close the help overlay."""
    try:
        from backend.core.ouroboros.governance.render_conductor import (
            ColorRole,
            EventKind,
            RegionKind,
            RenderEvent,
            get_render_conductor,
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    conductor = get_render_conductor()
    if conductor is None:
        return False
    try:
        conductor.publish(RenderEvent(
            kind=EventKind.MODAL_DISMISS,
            region=RegionKind.MODAL,
            role=ColorRole.METADATA,
            content="",
            source_module=source_module,
        ))
        return True
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Optional KeyAction binding wire — Slice 4 + Slice 6 integration
# ---------------------------------------------------------------------------


def register_help_action_handlers(
    resolver: ContextualHelpResolver,
    *,
    posture_provider: Any = None,
) -> bool:
    """Register HELP_OPEN / HELP_CLOSE handlers in the
    :class:`KeyActionRegistry` (Slice 4) so a `?` keypress resolves +
    publishes a help page; an `Esc` (or operator-bound key) closes it.

    Returns ``True`` when handlers were registered; ``False`` when the
    KeyActionRegistry isn't reachable (Slice 4 not wired) or master
    flag is off.

    ``posture_provider`` (optional): callable returning current posture
    string. When supplied, HELP_OPEN consults it for posture-aware
    ranking. When omitted, posture context is unavailable (resolver
    skips the posture-relevance scoring).
    """
    if not is_enabled():
        return False
    try:
        from backend.core.ouroboros.governance import key_input as _ki
        ctrl = _ki.get_input_controller()
        if ctrl is None:
            return False
        registry = ctrl.registry
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_help] key input registry not reachable", exc_info=True,
        )
        return False

    def _open(_event: Any) -> None:
        try:
            posture: Optional[str] = None
            if callable(posture_provider):
                try:
                    raw = posture_provider()
                    posture = str(raw) if raw is not None else None
                except Exception:  # noqa: BLE001 — defensive
                    posture = None
            page = resolver.resolve(posture=posture)
            publish_help_panel(page)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[render_help] HELP_OPEN handler failed", exc_info=True,
            )

    def _close(_event: Any) -> None:
        try:
            publish_help_dismiss()
        except Exception:  # noqa: BLE001 — defensive
            pass

    try:
        registry.register(_ki.KeyAction.HELP_OPEN, _open)
        registry.register(_ki.KeyAction.HELP_CLOSE, _close)
        return True
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Singleton triplet — mirrors RenderConductor / InputController /
# ThreadObserver pattern
# ---------------------------------------------------------------------------


_DEFAULT_RESOLVER: Optional[ContextualHelpResolver] = None
_DEFAULT_LOCK = threading.Lock()


def get_help_resolver() -> Optional[ContextualHelpResolver]:
    with _DEFAULT_LOCK:
        return _DEFAULT_RESOLVER


def register_help_resolver(
    resolver: Optional[ContextualHelpResolver],
) -> None:
    global _DEFAULT_RESOLVER
    with _DEFAULT_LOCK:
        _DEFAULT_RESOLVER = resolver


def reset_help_resolver() -> None:
    register_help_resolver(None)


# ---------------------------------------------------------------------------
# FlagRegistry registration — auto-discovered
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    all_postures_relevant = {
        "EXPLORE": Relevance.RELEVANT,
        "CONSOLIDATE": Relevance.RELEVANT,
        "HARDEN": Relevance.RELEVANT,
        "MAINTAIN": Relevance.RELEVANT,
    }
    specs = [
        FlagSpec(
            name=_FLAG_CONTEXTUAL_HELP_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master gate for the ContextualHelpResolver substrate "
                "(Wave 4 #1, Slice 6). Graduated default true at "
                "Slice 7 follow-up #4 — resolve() returns ranked "
                "pages over the typed registry aggregation; '?' "
                "keypress publishes MODAL_PROMPT pages. Hot-revert "
                "via false → empty page (resolver stays alive)."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/render_help.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_HELP_RANKING_WEIGHTS,
            type=FlagType.JSON,
            default=None,
            description=(
                "Operator overlay on default scoring weights "
                "(substring_name, substring_one_line, substring_body, "
                "posture_critical, posture_relevant, "
                "recent_verb_proximity, phase_affinity, "
                "kind_*_baseline). JSON object mapping weight name to "
                "float. Unmapped weights fall back to defaults; "
                "non-numeric values silently skipped."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/render_help.py"
            ),
            example='{"substring_name": 20.0}',
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_HELP_PAGE_SIZE,
            type=FlagType.INT,
            default=10,
            description=(
                "Default page size for resolve() when limit arg "
                "omitted. Min 1 (clamp). Operators tune for terminal "
                "height — 5 for compact density, 20 for full."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/render_help.py"
            ),
            example="10",
            since="v1.0",
        ),
    ]
    registry.bulk_register(specs, override=True)
    return len(specs)


# ---------------------------------------------------------------------------
# AST invariants — auto-discovered
# ---------------------------------------------------------------------------


_FORBIDDEN_RICH_PREFIX: tuple = ("rich",)
_FORBIDDEN_AUTHORITY_MODULES: tuple = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.risk_tier",
    "backend.core.ouroboros.governance.risk_tier_floor",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.gate",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.urgency_router",
    "backend.core.ouroboros.governance.cancel_token",
    "backend.core.ouroboros.governance.conversation_bridge",
    "backend.core.ouroboros.governance.help_dispatcher",
    "backend.core.ouroboros.governance.posture",
    "backend.core.ouroboros.governance.posture_observer",
    "backend.core.ouroboros.governance.posture_store",
)


_EXPECTED_HELP_KIND = frozenset({
    "VERB", "FLAG", "KEY_ACTION", "DOC", "TIP",
})
_EXPECTED_HELP_ENTRY_FIELDS = frozenset({
    "kind", "name", "one_line", "body", "source_module", "score",
})
_EXPECTED_HELP_PAGE_FIELDS = frozenset({
    "entries", "offset", "limit", "total",
})


def _imported_modules(tree: Any) -> List:
    import ast
    out: List = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                out.append((node.lineno, mod))
    return out


def _enum_member_names(tree: Any, class_name: str) -> List[str]:
    import ast
    out: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id.isupper():
                        out.append(tgt.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(
                stmt.target, ast.Name,
            ):
                if stmt.target.id.isupper():
                    out.append(stmt.target.id)
    return out


def _dataclass_field_names(tree: Any, class_name: str) -> List[str]:
    """Pull annotated field names from a dataclass body. Skips
    ClassVar-style assignments (which are not fields)."""
    import ast
    out: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name,
                ):
                    out.append(stmt.target.id)
    return out


def _validate_no_rich_import(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        for forbidden in _FORBIDDEN_RICH_PREFIX:
            if mod == forbidden or mod.startswith(forbidden + "."):
                violations.append(
                    f"line {lineno}: forbidden rich import: {mod!r}"
                )
    return tuple(violations)


def _validate_no_authority_imports(tree: Any, source: str) -> tuple:
    """render_help MUST NOT import any authority module OR any of the
    typed registries it consumes. The registries are accessed via lazy
    import inside the resolver — keeps the substrate descriptive only
    and ensures fresh-read semantics on each resolve()."""
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_help_kind_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_enum_member_names(tree, "HelpKind"))
    if found != set(_EXPECTED_HELP_KIND):
        return (
            f"HelpKind members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_HELP_KIND)}",
        )
    return ()


def _validate_help_entry_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_dataclass_field_names(tree, "HelpEntry"))
    if not found:
        return ("HelpEntry class not found",)
    if found != _EXPECTED_HELP_ENTRY_FIELDS:
        return (
            f"HelpEntry fields {sorted(found)} != expected "
            f"{sorted(_EXPECTED_HELP_ENTRY_FIELDS)}",
        )
    return ()


def _validate_help_page_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_dataclass_field_names(tree, "HelpPage"))
    if not found:
        return ("HelpPage class not found",)
    if found != _EXPECTED_HELP_PAGE_FIELDS:
        return (
            f"HelpPage fields {sorted(found)} != expected "
            f"{sorted(_EXPECTED_HELP_PAGE_FIELDS)}",
        )
    return ()


def _validate_discovery_symbols_present(
    tree: Any, source: str,
) -> tuple:
    del source
    import ast
    needed = {"register_flags", "register_shipped_invariants"}
    found: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in needed:
                found.add(node.name)
    missing = needed - found
    if missing:
        return (f"missing discovery symbols: {sorted(missing)}",)
    return ()


_TARGET_FILE = "backend/core/ouroboros/governance/render_help.py"


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="render_help_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "render_help.py MUST NOT import rich.* — substrate "
                "speaks HelpEntry / HelpPage primitives only; rendering "
                "belongs to backends consuming MODAL_PROMPT events."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="render_help_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "render_help.py MUST NOT import any authority module OR "
                "any typed registry (help_dispatcher / flag_registry / "
                "posture / posture_observer / posture_store) at top "
                "level. Each registry is consulted via lazy import "
                "inside the resolver — keeps the substrate descriptive "
                "only and ensures fresh-read semantics."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="render_help_help_kind_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "HelpKind enum members must exactly match the documented "
                "5-value closed set (VERB / FLAG / KEY_ACTION / DOC / "
                "TIP). Adding a kind requires coordinated registry "
                "update."
            ),
            validate=_validate_help_kind_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_help_help_entry_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "HelpEntry field set MUST be exactly {kind, name, "
                "one_line, body, source_module, score}. Adding/removing "
                "without coordinated to_metadata + closed-taxonomy pin "
                "update is structural drift."
            ),
            validate=_validate_help_entry_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_help_help_page_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "HelpPage field set MUST be exactly {entries, offset, "
                "limit, total}. has_more is derived; should not be a "
                "stored field."
            ),
            validate=_validate_help_page_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_help_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must be "
                "module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "ContextualHelpResolver",
    "HelpEntry",
    "HelpKind",
    "HelpPage",
    "RENDER_HELP_SCHEMA_VERSION",
    "default_page_size",
    "get_help_resolver",
    "is_enabled",
    "publish_help_dismiss",
    "publish_help_panel",
    "ranking_weights",
    "register_flags",
    "register_help_action_handlers",
    "register_help_resolver",
    "register_shipped_invariants",
    "reset_help_resolver",
]
