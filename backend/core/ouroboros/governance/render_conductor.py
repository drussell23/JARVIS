"""RenderConductor — unified rendering substrate for the operator surface.

Closes the architectural fragmentation that produces the 7 CC-parity gaps
identified in the §29 UI REPL audit (streaming reasoning, file:line refs,
vertical rhythm, color palette, esc-to-interrupt, browseable /help, visible
thread). Each of those gaps is, at root, a symptom of the same problem:
three independent render paths (``stream_renderer.py``, ``serpent_flow.py``,
``live_dashboard.py``) that don't share a substrate. Adding capability to
one doesn't reach the others; coordinating screen real-estate across them is
impossible. CC has one substrate; we add one too.

Slice 1 of 7 (Wave 4 #1 — UI REPL parity arc). This file ships the
**primitive only**: closed-taxonomy enums (``ColorRole`` / ``RegionKind`` /
``RenderDensity`` / ``EventKind``), frozen dataclasses (``Region`` /
``RenderEvent``), the ``Theme`` Protocol with hot-swappable
``ThemeRegistry``, the posture-derived density resolver, the
``RenderBackend`` Protocol, and the ``RenderConductor`` state machine.
**No backends are wired and no rendering changes occur in Slice 1.** Slice 2
migrates the three existing renderers to the conductor; Slices 3-6 add the
typed primitive consumers (ReasoningStream, FileRef, ThreadRegion, contextual
help); Slice 7 graduates.

Architectural mandates (each enforced):

1. **No Rich import in this module** — AST-pinned. Backends own Rich; the
   conductor speaks roles + style strings only. Operators can swap rendering
   backends (Rich, prompt_toolkit, plain stdout, JSON-line for IDE pipes)
   without touching this file.
2. **Closed taxonomies, AST-pinned** — every enum's member set is grep-
   verified by ``shipped_code_invariants`` so a future patch cannot silently
   add a NEW region/role/event-kind without coordinated registry updates.
3. **Zero hardcoded values in callers** — the master flag, theme name,
   density override, posture→density map, and palette overlay all flow
   through ``flag_registry.FlagRegistry``. The DefaultTheme palette is the
   *fallback*; operators override via ``JARVIS_RENDER_CONDUCTOR_PALETTE_-
   OVERRIDE`` (JSON) at runtime.
4. **Posture-derived adaptivity** — ``resolve_density`` consumes a posture
   string and returns a density. The mapping is overrideable via
   ``JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP`` (JSON); explicit
   ``JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE`` always wins. Authority-free:
   we accept a posture *string*, never import the ``Posture`` enum.
5. **Async-safe state machine** — ``RenderConductor`` serializes mutations
   under an ``asyncio.Lock``. ``publish`` is sync-non-blocking by contract
   (matches ``stream_renderer.on_token``); backends own their own internal
   queues if they need them.
6. **Module-owned auto-discovery** — exposes ``register_flags(registry)``
   and ``register_shipped_invariants()`` so the FlagRegistry seed and
   ShippedCodeInvariants seed both pick this module up automatically. Zero
   edits to ``flag_registry_seed.py`` or ``shipped_code_invariants.py``.

Authority invariants (AST-pinned at the bottom of this file via
``register_shipped_invariants``):

  * No imports of ``rich`` / ``rich.*``
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider / urgency_router
  * ``ColorRole`` / ``RegionKind`` / ``RenderDensity`` / ``EventKind``
    member sets pinned to the documented vocabulary
  * ``register_flags`` and ``register_shipped_invariants`` symbols present
    (so dynamic discovery never silently breaks)

Kill switch: ``JARVIS_RENDER_CONDUCTOR_ENABLED`` (default ``false`` at
Slice 1; flipped to ``true`` at Slice 7 graduation). When off,
``conductor.publish`` is a no-op (event dropped before backend dispatch);
backends remain registered so a hot-flip mid-session works.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

logger = logging.getLogger(__name__)


RENDER_CONDUCTOR_SCHEMA_VERSION: str = "render_conductor.1"


# ---------------------------------------------------------------------------
# Flag accessors — all values flow through FlagRegistry. Lazy import keeps
# this module authority-free at import time and decouples it from the
# registry's optional graduation state.
# ---------------------------------------------------------------------------


_FLAG_MASTER_ENABLED = "JARVIS_RENDER_CONDUCTOR_ENABLED"
_FLAG_THEME_NAME = "JARVIS_RENDER_CONDUCTOR_THEME_NAME"
_FLAG_DENSITY_OVERRIDE = "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE"
_FLAG_POSTURE_DENSITY_MAP = "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP"
_FLAG_PALETTE_OVERRIDE = "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE"


def _get_registry() -> Any:
    """Lazy import of the FlagRegistry singleton.

    Returns ``None`` if the registry module is unavailable (e.g. during a
    bootstrap test that imports this module standalone). The downstream
    accessors degrade to documented in-code defaults in that case — this
    module never crashes if the registry isn't loaded yet.
    """
    try:
        from backend.core.ouroboros.governance import flag_registry as _fr
        return _fr.ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def is_enabled() -> bool:
    """Master switch. Default ``false`` at Slice 1 — graduates to ``true``
    at Slice 7 once all 7 CC-parity gaps land on the substrate. When off,
    ``RenderConductor.publish`` becomes a no-op (event dropped before
    backend dispatch); backends remain registered so a hot-flip works."""
    reg = _get_registry()
    if reg is None:
        return False
    return reg.get_bool(_FLAG_MASTER_ENABLED, default=False)


def theme_name() -> str:
    """Active theme name. Looked up in :class:`ThemeRegistry`. Defaults
    to ``"default"`` (the in-process :class:`DefaultTheme`). Operators
    swap themes by setting the env var to a registered theme name."""
    reg = _get_registry()
    if reg is None:
        return "default"
    name = reg.get_str(_FLAG_THEME_NAME, default="default").strip()
    return name or "default"


def density_override() -> Optional["RenderDensity"]:
    """Explicit density override (compact|normal|full). When non-empty,
    posture-derived density is bypassed — this is the operator escape
    hatch for "I want to see everything regardless of HARDEN posture"
    (or vice versa). Empty string means "no override; use posture"."""
    reg = _get_registry()
    if reg is None:
        return None
    raw = (reg.get_str(_FLAG_DENSITY_OVERRIDE, default="").strip().lower())
    if not raw:
        return None
    return _density_from_string(raw)


def posture_density_overrides() -> Mapping[str, "RenderDensity"]:
    """Operator overlay on the in-code posture→density mapping. JSON
    object: ``{posture_name: density_name}``. Unspecified postures fall
    back to ``_DEFAULT_POSTURE_DENSITY``. Malformed entries are silently
    skipped (logged at DEBUG)."""
    reg = _get_registry()
    if reg is None:
        return {}
    raw = reg.get_json(_FLAG_POSTURE_DENSITY_MAP, default=None)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, RenderDensity] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        density = _density_from_string(v.strip().lower())
        if density is None:
            logger.debug(
                "[RenderConductor] posture density map: unknown density %r", v,
            )
            continue
        out[k.strip().upper()] = density
    return out


def palette_override() -> Mapping["ColorRole", str]:
    """Operator overlay on the DefaultTheme palette. JSON object mapping
    ColorRole names (METADATA / CONTENT / ...) to Rich style strings.
    Unmapped roles fall back to ``DefaultTheme._BASELINE``. Malformed
    entries silently skipped."""
    reg = _get_registry()
    if reg is None:
        return {}
    raw = reg.get_json(_FLAG_PALETTE_OVERRIDE, default=None)
    if not isinstance(raw, dict):
        return {}
    out: Dict[ColorRole, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        try:
            role = ColorRole(k.strip().upper())
        except ValueError:
            logger.debug(
                "[RenderConductor] palette override: unknown role %r", k,
            )
            continue
        out[role] = v
    return out


# ---------------------------------------------------------------------------
# Closed taxonomies — every enum's member set is AST-pinned at the bottom of
# this file. Adding/removing a value WITHOUT updating the pin will fail the
# shipped_code_invariants validation step at boot.
# ---------------------------------------------------------------------------


class ColorRole(str, enum.Enum):
    """Semantic color role. Producers emit roles; the active Theme
    resolves to a concrete Rich style string. This is the indirection
    that closes Gap #4 (CC's restrained palette) — by AST-pinning the
    closed set, no module can drift back to ``[red]raw[/red]`` strings.

    Inherits ``str`` so values serialize cleanly to JSON.
    """

    METADATA = "METADATA"     # paths, timestamps, op-ids, dim chrome
    CONTENT = "CONTENT"       # body text — model output, narrative
    SUCCESS = "SUCCESS"       # green path — APPLY ok, VERIFY pass
    WARNING = "WARNING"       # yellow — NOTIFY_APPLY, retries, FYI
    ERROR = "ERROR"           # red — failures, exceptions, blocks
    EMPHASIS = "EMPHASIS"     # bold — section headers, key actions
    MUTED = "MUTED"           # very dim — old context, replay


class RegionKind(str, enum.Enum):
    """Persistent terminal regions the conductor coordinates. The 7-slot
    taxonomy maps directly to CC's screen real-estate model: header line,
    conversation thread (Gap #7), main viewport, current-phase reasoning
    stream (Gap #1), status footer, input prompt, and modal overlay
    (Yellow approval prompts, /help, etc).
    """

    HEADER = "HEADER"
    THREAD = "THREAD"             # ConversationBridge consumer (Gap #7)
    VIEWPORT = "VIEWPORT"         # rolling event log
    PHASE_STREAM = "PHASE_STREAM"  # current-phase reasoning (Gap #1)
    STATUS = "STATUS"             # footer / heartbeat / posture
    INPUT = "INPUT"               # operator prompt
    MODAL = "MODAL"               # approval prompts, /help overlay


class RenderDensity(str, enum.Enum):
    """Vertical rhythm setting (Gap #3). Backends honor this when laying
    out events: COMPACT collapses multi-line displays into one-liners;
    FULL keeps box characters and per-event rationale; NORMAL is the
    middle. Density is resolved per-event from the conductor's current
    setting (which is in turn posture-derived unless explicitly
    overridden)."""

    COMPACT = "COMPACT"
    NORMAL = "NORMAL"
    FULL = "FULL"


class EventKind(str, enum.Enum):
    """Closed taxonomy of render events. Every producer emits one of
    these; backends route by kind. The vocabulary is intentionally
    small — Slices 3-6 add typed primitives (ReasoningStream, FileRef,
    ThreadTurn, ...) but those serialize INTO these event kinds at the
    conductor boundary so backends never need a type-switch explosion.
    """

    PHASE_BEGIN = "PHASE_BEGIN"
    PHASE_END = "PHASE_END"
    REASONING_TOKEN = "REASONING_TOKEN"  # Gap #1 streaming
    FILE_REF = "FILE_REF"                # Gap #2 typed file:line
    STATUS_TICK = "STATUS_TICK"          # heartbeat / posture
    MODAL_PROMPT = "MODAL_PROMPT"        # Yellow approval, /help open
    MODAL_DISMISS = "MODAL_DISMISS"
    THREAD_TURN = "THREAD_TURN"          # Gap #7 conversation entry
    BACKEND_RESET = "BACKEND_RESET"      # backend lifecycle


def _density_from_string(value: str) -> Optional[RenderDensity]:
    """Tolerant parse of a density string. Returns ``None`` for empty
    or unknown values (callers decide whether ``None`` means "use
    default" or "error")."""
    if not value:
        return None
    upper = value.strip().upper()
    for member in RenderDensity:
        if member.value == upper:
            return member
    return None


# ---------------------------------------------------------------------------
# Region — frozen dataclass describing one persistent terminal region
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    """One persistent screen region. Regions are configured once at
    conductor construction; events target a region via ``RenderEvent.
    region``. Frozen so a backend can hold a stable reference."""

    kind: RegionKind
    capacity_lines: int = 0          # 0 = unbounded
    scroll_policy: str = "tail"      # "tail" or "manual"
    density_override: Optional[RenderDensity] = None  # None = inherit conductor

    def with_density(self, density: RenderDensity) -> "Region":
        """Return a copy with ``density_override`` set. Frozen-friendly."""
        return Region(
            kind=self.kind,
            capacity_lines=self.capacity_lines,
            scroll_policy=self.scroll_policy,
            density_override=density,
        )


# ---------------------------------------------------------------------------
# RenderEvent — the typed wire format between producers and backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderEvent:
    """One render event. Frozen + hashable — safe to fan out across
    multiple backends without defensive copies. The schema is intentionally
    flat: ``content`` is the only free-form field; everything else is a
    closed taxonomy or a typed primitive.

    ``metadata`` is for backend hints (e.g. file_path/line for FILE_REF
    events) — kept loose so Slices 3-6 can add typed primitives without
    breaking the schema. Defaults to a frozen empty map."""

    kind: EventKind
    region: RegionKind
    role: ColorRole
    content: str
    source_module: str
    op_id: Optional[str] = None
    monotonic_ts: float = field(default_factory=time.monotonic)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "region": self.region.value,
            "role": self.role.value,
            "content": self.content,
            "source_module": self.source_module,
            "op_id": self.op_id,
            "monotonic_ts": self.monotonic_ts,
            "metadata": dict(self.metadata) if self.metadata else {},
            "schema_version": RENDER_CONDUCTOR_SCHEMA_VERSION,
        }


# ---------------------------------------------------------------------------
# Theme — Protocol + DefaultTheme + hot-swappable registry
# ---------------------------------------------------------------------------


@runtime_checkable
class Theme(Protocol):
    """Resolves a (role, density) pair to a backend-agnostic style string.

    Backends interpret the string as Rich-compatible markup
    (``"bold cyan"``, ``"dim white"``, ``""`` for no styling).
    Non-Rich backends MAY ignore the string entirely. The Protocol is
    runtime-checkable so tests can assert duck-typed compliance."""

    name: str

    def resolve(self, role: ColorRole, density: RenderDensity) -> str: ...


class DefaultTheme:
    """CC-aligned restrained palette: ~5 base colors + density modulation.

    The baseline mapping is intentionally minimal (Gap #4 — operators want
    fewer colors, not more). Operators overlay specific roles via the
    ``JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE`` env flag without code
    change. Density modulates intensity: COMPACT suppresses METADATA
    entirely (returns ``""``), NORMAL is balanced, FULL adds emphasis.

    The baseline values are the LAST-RESORT fallback — they exist so the
    theme is functional if no override is provided. The runtime contract
    is "consumers emit roles, theme resolves" — consumer modules MUST NOT
    contain raw color strings (AST-pinned in Slice 2 once consumers
    migrate)."""

    name: str = "default"

    # The baseline palette. EVERY value is overrideable via flag.
    _BASELINE: Mapping[ColorRole, str] = MappingProxyType({
        ColorRole.METADATA: "dim",
        ColorRole.CONTENT: "",         # default terminal color
        ColorRole.SUCCESS: "green",
        ColorRole.WARNING: "yellow",
        ColorRole.ERROR: "red",
        ColorRole.EMPHASIS: "bold",
        ColorRole.MUTED: "dim white",
    })

    def __init__(
        self,
        *,
        baseline: Optional[Mapping[ColorRole, str]] = None,
        overlay: Optional[Mapping[ColorRole, str]] = None,
    ) -> None:
        # Baseline can be swapped for tests. Overlay can be passed in
        # (used internally by ``DefaultTheme.from_flag_registry``).
        self._effective_baseline = (
            MappingProxyType(dict(baseline)) if baseline is not None
            else self._BASELINE
        )
        self._overlay: Mapping[ColorRole, str] = (
            MappingProxyType(dict(overlay)) if overlay else _EMPTY_PALETTE
        )

    @classmethod
    def from_flag_registry(cls) -> "DefaultTheme":
        """Construct a DefaultTheme with the current
        ``JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE`` overlay applied.
        Re-call after env mutation to pick up changes (no in-process
        cache — operators expect ``export X=...; touch nothing`` to
        take effect on the next conductor flush)."""
        return cls(overlay=palette_override())

    def resolve(self, role: ColorRole, density: RenderDensity) -> str:
        """Resolve a role to a style string. Density modulates: COMPACT
        suppresses METADATA / MUTED entirely; FULL adds italic to
        METADATA. NORMAL passes through."""
        base = self._overlay.get(role) or self._effective_baseline.get(role, "")
        if density is RenderDensity.COMPACT:
            if role in (ColorRole.METADATA, ColorRole.MUTED):
                return ""  # suppressed in compact
            return base
        if density is RenderDensity.FULL:
            if role is ColorRole.METADATA and base:
                return f"italic {base}"
            return base
        return base  # NORMAL


_EMPTY_PALETTE: Mapping[ColorRole, str] = MappingProxyType({})


class ThemeRegistry:
    """Process-wide theme directory. Hot-swappable via
    ``register(theme)``; lookup by name via ``get(name)``. The
    ``"default"`` theme is always present (registered at construction).

    Thread-safe (mirrors FlagRegistry's threading.Lock pattern). Kept
    deliberately small — themes are expected to be a handful (default,
    high-contrast, monochrome, JSON-pipe), not hundreds."""

    def __init__(self) -> None:
        self._themes: Dict[str, Theme] = {}
        self._lock = threading.Lock()
        self.register(DefaultTheme())

    def register(self, theme: Theme) -> None:
        """Install a theme. Replaces an existing entry of the same name
        (with a DEBUG log). Theme name is the lookup key — empty name
        is rejected silently."""
        name = getattr(theme, "name", None)
        if not isinstance(name, str) or not name.strip():
            return
        with self._lock:
            if name in self._themes:
                logger.debug(
                    "[RenderConductor] theme %r replacing existing", name,
                )
            self._themes[name] = theme

    def get(self, name: str) -> Theme:
        """Lookup by name. Falls back to the ``"default"`` theme if the
        name is missing — never raises, never returns ``None``."""
        with self._lock:
            theme = self._themes.get(name)
            if theme is not None:
                return theme
            return self._themes["default"]

    def names(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._themes.keys()))

    def clear_for_tests(self) -> None:
        """Drop all themes except a fresh DefaultTheme."""
        with self._lock:
            self._themes.clear()
            self._themes["default"] = DefaultTheme()


# ---------------------------------------------------------------------------
# Density resolution — pure function, posture-driven, override-aware
# ---------------------------------------------------------------------------


_DEFAULT_POSTURE_DENSITY: Mapping[str, RenderDensity] = MappingProxyType({
    "EXPLORE": RenderDensity.FULL,
    "CONSOLIDATE": RenderDensity.NORMAL,
    "HARDEN": RenderDensity.COMPACT,
    "MAINTAIN": RenderDensity.COMPACT,
})


def resolve_density(
    posture: Optional[str],
    *,
    explicit_override: Optional[RenderDensity] = None,
) -> RenderDensity:
    """Resolve the active render density.

    Precedence (strictest wins):
      1. ``explicit_override`` argument (caller-supplied)
      2. ``JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE`` env flag
      3. ``JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP`` per-posture
      4. ``_DEFAULT_POSTURE_DENSITY`` in-code mapping
      5. ``RenderDensity.NORMAL`` ultimate fallback

    Posture is a *string* (``"HARDEN"`` / ``"EXPLORE"`` / ...). We accept
    a string instead of importing the ``Posture`` enum so this module
    stays authority-free of the DirectionInferrer arc — consumers may
    pass ``posture.value`` or ``str(posture)`` interchangeably. Pass
    ``None`` when posture is unknown — falls through to NORMAL.
    """
    if explicit_override is not None:
        return explicit_override
    env_override = density_override()
    if env_override is not None:
        return env_override
    if posture is not None:
        normalized = str(posture).strip().upper()
        overrides = posture_density_overrides()
        if normalized in overrides:
            return overrides[normalized]
        if normalized in _DEFAULT_POSTURE_DENSITY:
            return _DEFAULT_POSTURE_DENSITY[normalized]
    return RenderDensity.NORMAL


# ---------------------------------------------------------------------------
# RenderBackend — Protocol that backends implement (Slice 2 wires the 3
# existing renderers as backends; Slice 1 ships only the Protocol).
# ---------------------------------------------------------------------------


@runtime_checkable
class RenderBackend(Protocol):
    """Render target. Receives events from the conductor.

    Contract:
      * ``notify(event)`` MUST be O(1) non-blocking (mirrors
        ``stream_renderer.on_token``). Backends with heavy work own their
        own internal queue + worker task.
      * ``flush()`` may block briefly to drain pending state. Called on
        phase boundaries and shutdown.
      * ``shutdown()`` releases resources. Called once at conductor
        teardown. Idempotent.
      * NEVER raises out of any method — defensive everywhere. Failures
        log at DEBUG; the conductor swallows backend exceptions so one
        misbehaving backend cannot break others.

    The Protocol is runtime-checkable so tests can ``isinstance`` against
    duck-typed implementations.
    """

    name: str

    def notify(self, event: RenderEvent) -> None: ...
    def flush(self) -> None: ...
    def shutdown(self) -> None: ...


# ---------------------------------------------------------------------------
# RenderConductor — the state machine that owns regions, theme, density,
# and a list of subscribed backends.
# ---------------------------------------------------------------------------


class RenderConductor:
    """Coordinates regions + theme + density + backends.

    Thread/coroutine model:
      * State mutations (``add_backend`` / ``remove_backend`` /
        ``set_theme_name`` / ``set_density_override``) take a
        ``threading.Lock`` — safe from any thread or coroutine.
      * ``publish(event)`` is sync-non-blocking: snapshots the backend
        list under the lock, releases, then calls ``backend.notify(event)``
        on each. Per-backend exceptions are swallowed (logged DEBUG).
      * ``flush()`` and ``shutdown()`` iterate backends sync.

    When ``is_enabled()`` returns False, ``publish`` becomes a no-op (the
    event is dropped before any backend dispatch). Backends remain
    registered so a runtime ``export JARVIS_RENDER_CONDUCTOR_ENABLED=true``
    works without re-wiring producers.
    """

    def __init__(
        self,
        *,
        regions: Optional[Mapping[RegionKind, Region]] = None,
        theme_registry: Optional[ThemeRegistry] = None,
    ) -> None:
        # Region map — every RegionKind gets a default Region if none
        # was supplied. Operators (Slice 2 wiring) supply customized
        # regions when boot-wiring the conductor.
        self._regions: Dict[RegionKind, Region] = {
            kind: Region(kind=kind) for kind in RegionKind
        }
        if regions:
            for kind, region in regions.items():
                if isinstance(kind, RegionKind) and isinstance(region, Region):
                    self._regions[kind] = region

        self._theme_registry = theme_registry or ThemeRegistry()
        self._backends: List[RenderBackend] = []
        self._lock = threading.Lock()
        self._density_override: Optional[RenderDensity] = None
        # Posture is consulted fresh on each publish (so a posture flip
        # mid-session takes effect without a re-wire).
        self._posture_provider: Optional[Callable[[], Optional[str]]] = None

    # -- configuration ----------------------------------------------------

    def add_backend(self, backend: RenderBackend) -> None:
        """Install a backend. Duplicate adds are idempotent (same object
        only counted once)."""
        with self._lock:
            if backend in self._backends:
                return
            self._backends.append(backend)

    def remove_backend(self, backend: RenderBackend) -> bool:
        """Remove a previously-added backend. Returns True if found."""
        with self._lock:
            try:
                self._backends.remove(backend)
                return True
            except ValueError:
                return False

    def backends(self) -> Tuple[RenderBackend, ...]:
        with self._lock:
            return tuple(self._backends)

    def set_density_override(self, density: Optional[RenderDensity]) -> None:
        """Programmatic density override (alongside the env flag).
        Caller-supplied override wins over env wins over posture."""
        with self._lock:
            self._density_override = density

    def set_posture_provider(
        self, provider: Optional[Callable[[], Optional[str]]],
    ) -> None:
        """Inject a callable that returns the current posture string
        (e.g. a closure over the PostureObserver). Called fresh on each
        ``publish`` so posture flips take effect immediately. Pass
        ``None`` to clear. Never imports posture module directly."""
        with self._lock:
            self._posture_provider = provider

    # -- read-only state introspection -----------------------------------

    def region(self, kind: RegionKind) -> Region:
        with self._lock:
            return self._regions.get(kind, Region(kind=kind))

    def regions(self) -> Mapping[RegionKind, Region]:
        with self._lock:
            return MappingProxyType(dict(self._regions))

    def active_theme(self) -> Theme:
        return self._theme_registry.get(theme_name())

    def active_density(self) -> RenderDensity:
        """Resolve the current density using the full precedence chain.
        Re-reads env + posture each call so changes take effect live."""
        with self._lock:
            override = self._density_override
            provider = self._posture_provider
        posture = None
        if provider is not None:
            try:
                posture = provider()
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[RenderConductor] posture_provider raised", exc_info=True,
                )
                posture = None
        return resolve_density(posture, explicit_override=override)

    # -- dispatch --------------------------------------------------------

    def publish(self, event: RenderEvent) -> None:
        """Sync-non-blocking dispatch. No-op when ``is_enabled()`` is
        False. Backend exceptions swallowed (logged DEBUG) so one
        misbehaving backend cannot break others. Caller MUST NOT assume
        synchronous render — backends are free to enqueue."""
        if not is_enabled():
            return
        with self._lock:
            backends = list(self._backends)
        for backend in backends:
            try:
                backend.notify(event)
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[RenderConductor] backend %r notify failed",
                    getattr(backend, "name", "?"), exc_info=True,
                )

    def flush(self) -> None:
        """Best-effort flush of every backend. Safe to call regardless
        of master flag (allows producers to drain on shutdown even if
        a runtime kill flipped the master off mid-session)."""
        with self._lock:
            backends = list(self._backends)
        for backend in backends:
            try:
                backend.flush()
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[RenderConductor] backend %r flush failed",
                    getattr(backend, "name", "?"), exc_info=True,
                )

    def shutdown(self) -> None:
        """Drain + tear down every backend. Idempotent — safe to call
        many times. Backends are NOT removed from the list (so a hot
        re-init can call shutdown then start fresh)."""
        with self._lock:
            backends = list(self._backends)
        for backend in backends:
            try:
                backend.shutdown()
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[RenderConductor] backend %r shutdown failed",
                    getattr(backend, "name", "?"), exc_info=True,
                )


# ---------------------------------------------------------------------------
# Process-global singleton — matches OpsDigestObserver / LastSessionSummary /
# StreamRenderer's register/get/reset triplet.
# ---------------------------------------------------------------------------


_DEFAULT_CONDUCTOR: Optional[RenderConductor] = None
_DEFAULT_LOCK = threading.Lock()


def get_render_conductor() -> Optional[RenderConductor]:
    """Return the registered conductor or ``None`` if headless / not
    wired. Producers should defensive-check on every publish."""
    with _DEFAULT_LOCK:
        return _DEFAULT_CONDUCTOR


def register_render_conductor(conductor: Optional[RenderConductor]) -> None:
    """Register the process-global conductor. Pass ``None`` to clear.
    Slice 2 wires this from the harness boot (after backends are added).
    """
    global _DEFAULT_CONDUCTOR
    with _DEFAULT_LOCK:
        _DEFAULT_CONDUCTOR = conductor


def reset_render_conductor() -> None:
    """Test helper — clear the singleton."""
    register_render_conductor(None)


# ---------------------------------------------------------------------------
# FlagRegistry registration — auto-discovered by flag_registry_seed.
# Adding/changing a flag here requires NO edits to the seed file. The
# registry treats this module as a flag provider.
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Register the conductor's flags into ``registry``. Called by
    ``flag_registry_seed._discover_module_provided_flags`` at boot.
    Returns the number of FlagSpecs installed."""
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
            name=_FLAG_MASTER_ENABLED,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for the unified RenderConductor "
                "substrate (Wave 4 #1, Slices 1-7). Default false at "
                "Slice 1 — graduates to true at Slice 7 once all 7 "
                "CC-parity gaps land on the substrate. When false, "
                "RenderConductor.publish drops events before backend "
                "dispatch; backends remain registered so hot-flip works."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/render_conductor.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_THEME_NAME,
            type=FlagType.STR,
            default="default",
            description=(
                "Active theme name — looked up in ThemeRegistry. "
                "Defaults to 'default' (DefaultTheme — restrained "
                "CC-aligned palette). Operators swap themes by setting "
                "this to a registered theme name."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/render_conductor.py"
            ),
            example="default",
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_DENSITY_OVERRIDE,
            type=FlagType.STR,
            default="",
            description=(
                "Explicit density override (compact|normal|full). "
                "When non-empty, posture-derived density resolution is "
                "bypassed — operator escape hatch for 'I want to see "
                "everything regardless of HARDEN posture' or vice versa. "
                "Empty string means 'no override; use posture'."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/render_conductor.py"
            ),
            example="",
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_POSTURE_DENSITY_MAP,
            type=FlagType.JSON,
            default=None,
            description=(
                "Operator overlay on the in-code posture→density "
                "mapping. JSON object: {posture_name: density_name}. "
                "Unspecified postures fall back to the in-code default "
                "(EXPLORE→FULL, CONSOLIDATE→NORMAL, HARDEN→COMPACT, "
                "MAINTAIN→COMPACT). Malformed entries silently skipped."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/render_conductor.py"
            ),
            example='{"EXPLORE": "full", "HARDEN": "compact"}',
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_PALETTE_OVERRIDE,
            type=FlagType.JSON,
            default=None,
            description=(
                "Operator overlay on DefaultTheme palette. JSON object "
                "mapping ColorRole names (METADATA/CONTENT/SUCCESS/"
                "WARNING/ERROR/EMPHASIS/MUTED) to Rich style strings. "
                "Unmapped roles fall back to the in-code baseline. "
                "Malformed entries silently skipped."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/render_conductor.py"
            ),
            example='{"ERROR": "bold red on white"}',
            since="v1.0",
        ),
    ]
    registry.bulk_register(specs, override=True)
    return len(specs)


# ---------------------------------------------------------------------------
# ShippedCodeInvariants registration — auto-discovered by the invariants
# module (mirrors flag discovery). Pure ast.parse walks; never raises.
# ---------------------------------------------------------------------------


# Closed-taxonomy expected member sets (single source of truth for both the
# enum *and* the AST pin).
_EXPECTED_COLOR_ROLE = frozenset(
    {"METADATA", "CONTENT", "SUCCESS", "WARNING",
     "ERROR", "EMPHASIS", "MUTED"}
)
_EXPECTED_REGION_KIND = frozenset(
    {"HEADER", "THREAD", "VIEWPORT", "PHASE_STREAM",
     "STATUS", "INPUT", "MODAL"}
)
_EXPECTED_RENDER_DENSITY = frozenset({"COMPACT", "NORMAL", "FULL"})
_EXPECTED_EVENT_KIND = frozenset(
    {"PHASE_BEGIN", "PHASE_END", "REASONING_TOKEN", "FILE_REF",
     "STATUS_TICK", "MODAL_PROMPT", "MODAL_DISMISS",
     "THREAD_TURN", "BACKEND_RESET"}
)


_FORBIDDEN_RICH_PREFIX: Tuple[str, ...] = ("rich",)
_FORBIDDEN_AUTHORITY_MODULES: Tuple[str, ...] = (
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
)


def _imported_modules(tree: Any) -> List[Tuple[int, str]]:
    """Return list of (lineno, module_name) for every Import / ImportFrom
    in the AST. Module names normalized to dotted form."""
    import ast
    out: List[Tuple[int, str]] = []
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
    """Extract the assigned member names from an Enum-style class body.
    Looks for ``NAME = "VALUE"`` and ``NAME: type = "VALUE"`` patterns."""
    import ast
    found: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id.isupper():
                        found.append(tgt.id)
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id.isupper():
                    found.append(stmt.target.id)
    return found


def _validate_no_rich_import(tree: Any, source: str) -> Tuple[str, ...]:
    """RenderConductor module must NOT import any ``rich.*`` symbol.
    Backends own Rich; the conductor speaks roles + style strings only.
    """
    del source  # AST-only check
    violations: List[str] = []
    for lineno, mod_name in _imported_modules(tree):
        for forbidden in _FORBIDDEN_RICH_PREFIX:
            if mod_name == forbidden or mod_name.startswith(forbidden + "."):
                violations.append(
                    f"line {lineno}: forbidden rich import: {mod_name!r}"
                )
    return tuple(violations)


def _validate_no_authority_imports(
    tree: Any, source: str,
) -> Tuple[str, ...]:
    """RenderConductor must NOT import any authority module — keeps the
    rendering substrate descriptive only, never a control-flow surface
    for orchestrator / policy / gate decisions."""
    del source  # AST-only check
    violations: List[str] = []
    for lineno, mod_name in _imported_modules(tree):
        for forbidden in _FORBIDDEN_AUTHORITY_MODULES:
            if mod_name == forbidden:
                violations.append(
                    f"line {lineno}: forbidden authority import: "
                    f"{mod_name!r}"
                )
    return tuple(violations)


def _validate_color_role_closed(
    tree: Any, source: str,
) -> Tuple[str, ...]:
    """ColorRole enum members must exactly match the documented closed
    set. Adding a new role without coordinated theme + AST pin update
    is a structural drift — caught here at boot."""
    del source  # AST-only check
    found = set(_enum_member_names(tree, "ColorRole"))
    if found != set(_EXPECTED_COLOR_ROLE):
        return (
            f"ColorRole members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_COLOR_ROLE)}",
        )
    return ()


def _validate_region_kind_closed(
    tree: Any, source: str,
) -> Tuple[str, ...]:
    """RegionKind closed-taxonomy pin."""
    del source  # AST-only check
    found = set(_enum_member_names(tree, "RegionKind"))
    if found != set(_EXPECTED_REGION_KIND):
        return (
            f"RegionKind members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_REGION_KIND)}",
        )
    return ()


def _validate_render_density_closed(
    tree: Any, source: str,
) -> Tuple[str, ...]:
    """RenderDensity closed-taxonomy pin."""
    del source  # AST-only check
    found = set(_enum_member_names(tree, "RenderDensity"))
    if found != set(_EXPECTED_RENDER_DENSITY):
        return (
            f"RenderDensity members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_RENDER_DENSITY)}",
        )
    return ()


def _validate_event_kind_closed(
    tree: Any, source: str,
) -> Tuple[str, ...]:
    """EventKind closed-taxonomy pin."""
    del source  # AST-only check
    found = set(_enum_member_names(tree, "EventKind"))
    if found != set(_EXPECTED_EVENT_KIND):
        return (
            f"EventKind members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_EVENT_KIND)}",
        )
    return ()


def _validate_discovery_symbols_present(
    tree: Any, source: str,
) -> Tuple[str, ...]:
    """``register_flags`` and ``register_shipped_invariants`` must be
    defined at module level so the dynamic-discovery loops (in
    ``flag_registry_seed`` and ``shipped_code_invariants``) can find
    this module's contributions."""
    del source  # AST-only check
    import ast
    needed = {"register_flags", "register_shipped_invariants"}
    found: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in needed:
                found.add(node.name)
    missing = needed - found
    if missing:
        return (
            f"missing module-level discovery functions: "
            f"{sorted(missing)}",
        )
    return ()


_TARGET_FILE = "backend/core/ouroboros/governance/render_conductor.py"


def register_shipped_invariants() -> List[Any]:
    """Auto-discovered by ``shipped_code_invariants._discover_module_-
    provided_invariants``. Returns the AST pins that protect this
    module's structural shape."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="render_conductor_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "RenderConductor module MUST NOT import rich.* — Rich "
                "lives in backends only. Pinned so a future patch "
                "cannot silently couple the substrate to one render "
                "library, breaking the backend swap-out contract."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="render_conductor_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "RenderConductor MUST NOT import any authority module "
                "(orchestrator / policy / iron_gate / risk_tier / "
                "change_engine / candidate_generator / gate / "
                "semantic_guardian / semantic_firewall / providers / "
                "doubleword_provider / urgency_router). Rendering is "
                "descriptive-only; it cannot become a control-flow "
                "surface."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="render_conductor_color_role_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "ColorRole enum members must exactly match the "
                "documented 7-value closed set. Adding a role without "
                "coordinated theme + pin update is structural drift."
            ),
            validate=_validate_color_role_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_conductor_region_kind_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "RegionKind enum members must exactly match the "
                "documented 7-value closed set."
            ),
            validate=_validate_region_kind_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_conductor_render_density_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "RenderDensity enum members must exactly match the "
                "documented 3-value closed set (COMPACT/NORMAL/FULL)."
            ),
            validate=_validate_render_density_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_conductor_event_kind_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "EventKind enum members must exactly match the "
                "documented 9-value closed set."
            ),
            validate=_validate_event_kind_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_conductor_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags and register_shipped_invariants MUST "
                "be defined at module level so the dynamic-discovery "
                "loops in flag_registry_seed and shipped_code_invariants "
                "find this module's contributions. Without these, "
                "flag and pin registration silently degrades to no-op."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "ColorRole",
    "DefaultTheme",
    "EventKind",
    "RENDER_CONDUCTOR_SCHEMA_VERSION",
    "Region",
    "RegionKind",
    "RenderBackend",
    "RenderConductor",
    "RenderDensity",
    "RenderEvent",
    "Theme",
    "ThemeRegistry",
    "density_override",
    "get_render_conductor",
    "is_enabled",
    "palette_override",
    "posture_density_overrides",
    "register_flags",
    "register_render_conductor",
    "register_shipped_invariants",
    "reset_render_conductor",
    "resolve_density",
    "theme_name",
]
