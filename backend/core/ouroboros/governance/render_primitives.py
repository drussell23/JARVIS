"""Typed producer primitives — :class:`ReasoningStream` + :class:`FileRef`.

Slice 3 of the RenderConductor arc (Wave 4 #1). Closes Gap #1 (streaming
reasoning text in viewport) and Gap #2 (file:line refs as a typed
primitive with IDE click-through).

Architectural posture: producers stop calling
``stream_renderer.on_token`` / printing ``"path:line"`` strings directly;
they emit through these typed primitives, which publish into the
``RenderConductor``. The conductor fans out to the three backends wired
in Slice 2. Single source of truth. Single fan-out surface.

Design pillars (each load-bearing):

  1. **Stateless façade for ReasoningStream** — the primitive holds
     ``op_id`` + ``provider`` for the current stream; no buffering,
     no queueing. Buffering belongs to the backend (``stream_renderer``
     already has a 16-ms-coalesced async consumer). The primitive's
     job is to construct typed RenderEvents and publish — O(1) per
     token, no contention with the legacy direct path.
  2. **Frozen FileRef with __post_init__ validation** — every
     :class:`FileRef` is shape-checked at construction (path non-empty,
     line/column non-negative-or-None). Render methods never raise.
     The closed taxonomy of fields ({path, line, column, anchor})
     is AST-pinned so future patches cannot widen the schema without
     coordinated registry update.
  3. **OSC 8 hyperlink rendering, terminal-tolerant** — the
     ``render_hyperlink`` method emits the ANSI hyperlink escape
     sequence supported by VS Code / iTerm2 / Wezterm / GNOME
     Terminal et al. Operators on legacy terminals disable via
     ``JARVIS_FILE_REF_HYPERLINK_ENABLED=false`` — the primitive
     falls through to plain ``"path:line:col"`` rendering.
  4. **No hardcoded values in callers** — the master flags (gating
     ReasoningStream emission and OSC 8 hyperlinks), the conductor
     lookup, and the EventKind/Region/Role tags all flow through
     the existing FlagRegistry + closed taxonomies. Callers carry
     no string literals.
  5. **Conductor lookup is lazy + None-tolerant** — primitives obtain
     the conductor via ``get_render_conductor()`` per-call (no cached
     reference). When the conductor isn't yet wired (boot ordering)
     or is detached (test isolation), publish degrades to a documented
     no-op; the producer keeps running.
  6. **Defensive everywhere** — every public method swallows
     exceptions and logs DEBUG. A producer mid-stream cannot crash
     because rendering glue threw.

Authority invariants (AST-pinned via ``register_shipped_invariants``):

  * No imports of ``rich`` / ``rich.*`` (substrate hygiene mirror).
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router. Producer-side primitives are descriptive only.
  * :class:`FileRef` field set is exactly ``{path, line, column,
    anchor}`` — AST-pinned closed taxonomy.
  * :class:`ReasoningStream` defines the lifecycle triplet
    (``start`` / ``on_token`` / ``end``) — pinned so a refactor
    cannot silently drop a method.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switches:

  * ``JARVIS_REASONING_STREAM_ENABLED`` — master gate for
    ReasoningStream emission. Default ``false`` at Slice 3;
    graduates with the conductor at Slice 7.
  * ``JARVIS_FILE_REF_HYPERLINK_ENABLED`` — toggles OSC 8 hyperlink
    rendering vs. plain ``path:line``. Default ``true`` (most
    modern terminals support it). Operators on legacy terminals
    disable via env.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote as _url_quote

logger = logging.getLogger(__name__)


RENDER_PRIMITIVES_SCHEMA_VERSION: str = "render_primitives.1"


_FLAG_REASONING_STREAM_ENABLED = "JARVIS_REASONING_STREAM_ENABLED"
_FLAG_FILE_REF_HYPERLINK_ENABLED = "JARVIS_FILE_REF_HYPERLINK_ENABLED"


# ---------------------------------------------------------------------------
# Flag accessors — lazy registry import (mirrors render_conductor pattern)
# ---------------------------------------------------------------------------


def _get_registry() -> Any:
    try:
        from backend.core.ouroboros.governance import flag_registry as _fr
        return _fr.ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def reasoning_stream_enabled() -> bool:
    """Master gate for ReasoningStream emission. Graduated default
    ``true`` at Slice 7 follow-up #4 — the substrate is now the
    single producer of REASONING_TOKEN events. When ``false``,
    producer-side calls become no-ops (legacy direct stream_renderer
    path remains the active rendering route — hot-revert).

    Note: even when ``true``, the conductor's master flag
    ``JARVIS_RENDER_CONDUCTOR_ENABLED`` must ALSO be ``true`` for events
    to reach backends — emission and dispatch are independently gated
    so operators can A/B without ambiguity."""
    reg = _get_registry()
    if reg is None:
        return True
    return reg.get_bool(_FLAG_REASONING_STREAM_ENABLED, default=True)


def file_ref_hyperlink_enabled() -> bool:
    """Toggle OSC 8 hyperlink rendering. Default ``true``. Set to
    ``false`` for legacy terminals that don't recognize the escape
    sequence (will display the escape bytes instead of rendering as
    a clickable link)."""
    reg = _get_registry()
    if reg is None:
        return True  # default true even without registry
    return reg.get_bool(_FLAG_FILE_REF_HYPERLINK_ENABLED, default=True)


# ---------------------------------------------------------------------------
# FileRef — frozen typed primitive with shape validation + render methods
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRef:
    """Typed file:line reference. Closes Gap #2: file:line refs as a
    typed primitive instead of scattered strings.

    Frozen + hashable so producers can pass instances around (e.g. into
    ``ContextResolver`` rankings) without defensive copies. All four
    fields together define the addressable location:

      * ``path`` — repo-relative or absolute path string. Required.
      * ``line`` — 1-indexed line number; ``None`` means "whole file".
      * ``column`` — 1-indexed column; ``None`` means "whole line".
      * ``anchor`` — symbol/section anchor (e.g. ``"ClassName.method"``);
        ``None`` means "no anchor". Used by IDE backends that prefer
        symbol-based navigation over line offsets.

    Validation runs at construction via ``__post_init__``; invalid
    shapes raise ``ValueError`` so producers catch the bug at emission
    time, not at render time.
    """

    path: str
    line: Optional[int] = None
    column: Optional[int] = None
    anchor: Optional[str] = None

    def __post_init__(self) -> None:
        # Validate shape — fail fast at the producer, not at render time.
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError(
                f"FileRef.path must be a non-empty string, got {self.path!r}"
            )
        if self.line is not None:
            if not isinstance(self.line, int) or self.line < 0:
                raise ValueError(
                    f"FileRef.line must be a non-negative int or None, "
                    f"got {self.line!r}"
                )
        if self.column is not None:
            if not isinstance(self.column, int) or self.column < 0:
                raise ValueError(
                    f"FileRef.column must be a non-negative int or None, "
                    f"got {self.column!r}"
                )
        if self.anchor is not None and not isinstance(self.anchor, str):
            raise ValueError(
                f"FileRef.anchor must be a string or None, "
                f"got {self.anchor!r}"
            )

    def render_plain(self) -> str:
        """Return the canonical ``path[:line[:column]][#anchor]`` form.
        Used as the visible text inside an OSC 8 link, and as the full
        rendering when hyperlinks are disabled."""
        parts: List[str] = [self.path]
        if self.line is not None:
            parts.append(f":{self.line}")
            if self.column is not None:
                parts.append(f":{self.column}")
        out = "".join(parts)
        if self.anchor:
            out = f"{out}#{self.anchor}"
        return out

    def render_hyperlink(self, *, base_dir: Optional[str] = None) -> str:
        """Render as an OSC 8 hyperlink escape sequence.

        Format: ``\\x1b]8;;<uri>\\x1b\\\\<display>\\x1b]8;;\\x1b\\\\``

        VS Code / Cursor / iTerm2 / Wezterm / GNOME Terminal recognize
        this and render the ``<display>`` text as a clickable link. Other
        terminals show the display text and treat the escapes as no-ops.

        ``base_dir`` (optional): when supplied, relative paths are
        absolutized against it before the URI is built. Without it,
        relative paths produce ``file://<relative>`` URIs that some
        editors interpret as relative to the workspace root.

        When ``file_ref_hyperlink_enabled()`` returns ``false``, this
        method returns ``render_plain()`` instead — operator escape
        hatch for legacy terminals."""
        if not file_ref_hyperlink_enabled():
            return self.render_plain()

        # Build the file:// URI. Always quote the path for spaces /
        # unicode safety. Line + column travel as a query fragment that
        # VS Code's URI handler understands.
        path_for_uri = self.path
        if base_dir and not path_for_uri.startswith("/"):
            base = base_dir.rstrip("/")
            path_for_uri = f"{base}/{path_for_uri}"

        uri = f"file://{_url_quote(path_for_uri, safe='/:')}"
        if self.line is not None:
            uri = f"{uri}#L{self.line}"
            if self.column is not None:
                uri = f"{uri}C{self.column}"

        display = self.render_plain()

        # OSC 8 sequence: \x1b]8;;URI\x1b\\TEXT\x1b]8;;\x1b\\
        # The empty parameter set (``;;``) is required per the spec.
        esc_open = f"\x1b]8;;{uri}\x1b\\"
        esc_close = "\x1b]8;;\x1b\\"
        return f"{esc_open}{display}{esc_close}"

    def to_metadata(self) -> Dict[str, Any]:
        """Serialize for embedding in :class:`RenderEvent.metadata`.
        Preserves all four fields + a schema_version for backend
        consumers that pin a contract."""
        return {
            "schema_version": RENDER_PRIMITIVES_SCHEMA_VERSION,
            "kind": "file_ref",
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "anchor": self.anchor,
        }

    @classmethod
    def from_metadata(cls, payload: Mapping[str, Any]) -> Optional["FileRef"]:
        """Inverse of :meth:`to_metadata`. Returns ``None`` on malformed
        payload — never raises. Backend-side helper for reconstructing a
        :class:`FileRef` from a RenderEvent's metadata dict."""
        try:
            if not isinstance(payload, Mapping):
                return None
            path = payload.get("path")
            if not isinstance(path, str) or not path.strip():
                return None
            line = payload.get("line")
            column = payload.get("column")
            anchor = payload.get("anchor")
            if line is not None and not isinstance(line, int):
                line = None
            if column is not None and not isinstance(column, int):
                column = None
            if anchor is not None and not isinstance(anchor, str):
                anchor = None
            return cls(path=path, line=line, column=column, anchor=anchor)
        except Exception:  # noqa: BLE001 — defensive
            return None


def publish_file_ref(
    file_ref: FileRef,
    *,
    source_module: str,
    region: Optional[Any] = None,
    role: Optional[Any] = None,
    op_id: Optional[str] = None,
    extra_metadata: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Publish a FILE_REF event into the conductor.

    Returns ``True`` when an event reached the conductor's publish
    surface (regardless of whether the master flag let it through to
    backends — that's the conductor's gate). Returns ``False`` only on
    boot-ordering edge cases (no conductor registered yet) or on
    construction failure.

    ``region`` / ``role`` default to the natural slots
    (:class:`RegionKind.VIEWPORT` and :class:`ColorRole.METADATA`) but
    callers may target alternative regions (e.g. a specific FILE_REF
    inside a phase block emitting into PHASE_STREAM)."""
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

    target_region = region if region is not None else RegionKind.VIEWPORT
    target_role = role if role is not None else ColorRole.METADATA

    metadata = dict(file_ref.to_metadata())
    if extra_metadata:
        try:
            metadata.update(dict(extra_metadata))
        except Exception:  # noqa: BLE001 — defensive
            pass

    try:
        event = RenderEvent(
            kind=EventKind.FILE_REF,
            region=target_region,
            role=target_role,
            content=file_ref.render_plain(),
            source_module=source_module,
            op_id=op_id,
            metadata=metadata,
        )
        conductor.publish(event)
        return True
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_primitives] publish_file_ref failed", exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# ReasoningStream — producer-side façade for REASONING_TOKEN events
# ---------------------------------------------------------------------------


@dataclass
class ReasoningStream:
    """Producer-side typed primitive for streaming model reasoning.

    Closes Gap #1: closes the per-token data-flow gap by giving
    producers (providers.py, generate_runner.py) a typed API that
    publishes into the conductor instead of calling
    ``stream_renderer.on_token`` directly.

    Lifecycle (each method is sync-non-blocking, O(1)):

      1. ``start(op_id, provider)`` — publishes PHASE_BEGIN, marks
         the stream active. Idempotent: a re-start auto-ends the
         prior stream.
      2. ``on_token(text)`` — publishes one REASONING_TOKEN event.
         Hot path. Drops empty strings silently; never awaits.
      3. ``end()`` — publishes PHASE_END, clears active flag.

    State is per-instance (not module-global), so concurrent streams
    on different ops are independent. The conductor singleton is
    looked up lazily on each publish — boot ordering is forgiving.

    When ``reasoning_stream_enabled()`` returns ``false``, all three
    methods become no-ops (legacy direct path is the active route).
    """

    source_module: str = "render_primitives.ReasoningStream"
    _op_id: str = field(default="", init=False, repr=False)
    _provider: str = field(default="", init=False, repr=False)
    _active: bool = field(default=False, init=False, repr=False)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def provider(self) -> str:
        return self._provider

    def start(self, op_id: str, provider: str = "") -> bool:
        """Begin a streaming session. Returns ``True`` if the event
        was published (or would be — i.e. the master flag is on AND
        the conductor is registered). Idempotent: a second start
        auto-ends the prior session before opening the new one."""
        if not reasoning_stream_enabled():
            return False
        try:
            if self._active:
                self.end()
            self._op_id = str(op_id) if op_id else ""
            self._provider = str(provider) if provider else ""
            self._active = True
            return self._publish_lifecycle("PHASE_BEGIN")
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ReasoningStream] start failed", exc_info=True,
            )
            return False

    def on_token(self, text: str) -> bool:
        """Publish one token event. Hot path — O(1). Returns ``True``
        when the event was constructed and conductor.publish was
        called. Returns ``False`` for empty / falsy text (silently
        skipped) or when the master flag is off."""
        if not reasoning_stream_enabled():
            return False
        if not text:
            return False
        if not self._active:
            # Tolerate token-without-start (some producers stream
            # before the explicit start hook). Mark active implicitly
            # so subsequent end() works.
            self._active = True
        try:
            return self._publish_token(text)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ReasoningStream] on_token failed", exc_info=True,
            )
            return False

    def end(self) -> bool:
        """Finalize the stream. Idempotent — a second end is a no-op
        (returns ``False`` because nothing was published)."""
        if not reasoning_stream_enabled():
            return False
        if not self._active:
            return False
        try:
            published = self._publish_lifecycle("PHASE_END")
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ReasoningStream] end failed", exc_info=True,
            )
            published = False
        self._active = False
        self._op_id = ""
        self._provider = ""
        return published

    # -- internal helpers ----------------------------------------------

    def _publish_token(self, text: str) -> bool:
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
        event = RenderEvent(
            kind=EventKind.REASONING_TOKEN,
            region=RegionKind.PHASE_STREAM,
            role=ColorRole.CONTENT,
            content=text,
            source_module=self.source_module,
            op_id=self._op_id or None,
            metadata={"provider": self._provider} if self._provider else {},
        )
        conductor.publish(event)
        return True

    def _publish_lifecycle(self, kind_name: str) -> bool:
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
            kind = EventKind(kind_name)
        except ValueError:
            return False
        event = RenderEvent(
            kind=kind,
            region=RegionKind.PHASE_STREAM,
            role=ColorRole.METADATA,
            content="",
            source_module=self.source_module,
            op_id=self._op_id or None,
            metadata={"provider": self._provider} if self._provider else {},
        )
        conductor.publish(event)
        return True


# ---------------------------------------------------------------------------
# Producer-side helper — providers.py et al call this to get a wired
# token callback. Returns None when ReasoningStream is disabled OR no
# conductor is registered (caller falls back to legacy direct path).
# ---------------------------------------------------------------------------


def get_reasoning_stream_callback(
    op_id: str, provider: str = "",
) -> Optional[Any]:
    """Return a callable ``(text: str) -> None`` that publishes each
    token through a fresh :class:`ReasoningStream` (already started).
    Caller is responsible for calling the returned callback's
    ``end_callback`` attribute when the stream completes.

    Returns ``None`` when the master flag is off OR the conductor
    isn't registered — caller falls back to its legacy direct path.

    Usage::

        cb = get_reasoning_stream_callback(op_id, provider="claude")
        if cb is not None:
            for tok in stream:
                cb(tok)
            cb.end_callback()
        else:
            # legacy direct path
            ...
    """
    if not reasoning_stream_enabled():
        return None
    try:
        from backend.core.ouroboros.governance.render_conductor import (
            get_render_conductor,
        )
        if get_render_conductor() is None:
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None

    stream = ReasoningStream()
    if not stream.start(op_id=op_id, provider=provider):
        return None

    def _on_token(text: str) -> None:
        try:
            stream.on_token(text)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ReasoningStream] callback on_token failed", exc_info=True,
            )

    def _end() -> None:
        try:
            stream.end()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ReasoningStream] callback end failed", exc_info=True,
            )

    setattr(_on_token, "end_callback", _end)
    setattr(_on_token, "stream", stream)
    return _on_token


# ---------------------------------------------------------------------------
# FlagRegistry registration — auto-discovered
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Register Slice 3's flags into ``registry``. Auto-discovered
    by ``flag_registry_seed._discover_module_provided_flags``."""
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
            name=_FLAG_REASONING_STREAM_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master gate for ReasoningStream emission (Wave 4 #1, "
                "Slice 3). Graduated default true at Slice 7 follow-up "
                "#4 — substrate is the single producer of "
                "REASONING_TOKEN events. Hot-revert via "
                "JARVIS_REASONING_STREAM_ENABLED=false → providers.py "
                "falls back to legacy direct stream_renderer.on_token "
                "path."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/render_primitives.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_FILE_REF_HYPERLINK_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Toggle OSC 8 hyperlink rendering for FileRef. Default "
                "true (most modern terminals support it: VS Code / "
                "iTerm2 / Wezterm / GNOME Terminal). Set false for "
                "legacy terminals that display the escape bytes "
                "instead of rendering as a clickable link — "
                "FileRef.render_hyperlink falls back to render_plain."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/render_primitives.py"
            ),
            example="true",
            since="v1.0",
        ),
    ]
    registry.bulk_register(specs, override=True)
    return len(specs)


# ---------------------------------------------------------------------------
# AST invariants — auto-discovered by shipped_code_invariants
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
)

_EXPECTED_FILEREF_FIELDS = frozenset({"path", "line", "column", "anchor"})
_EXPECTED_REASONING_STREAM_METHODS = frozenset(
    {"start", "on_token", "end"}
)


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
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_fileref_closed_taxonomy(tree: Any, source: str) -> tuple:
    """FileRef dataclass MUST contain exactly the four documented fields.
    Adding/removing without coordinated to_metadata + AST pin update is
    structural drift caught here."""
    del source
    import ast
    found: set = set()
    seen_class = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "FileRef":
            seen_class = True
            for stmt in node.body:
                # Annotated field declarations: `path: str` or
                # `line: Optional[int] = None`.
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name,
                ):
                    found.add(stmt.target.id)
    if not seen_class:
        return ("FileRef class not found",)
    if found != _EXPECTED_FILEREF_FIELDS:
        return (
            f"FileRef fields {sorted(found)} != expected "
            f"{sorted(_EXPECTED_FILEREF_FIELDS)}",
        )
    return ()


def _validate_reasoning_stream_lifecycle(
    tree: Any, source: str,
) -> tuple:
    """ReasoningStream MUST define the lifecycle triplet
    (start / on_token / end). Refactor that drops a method silently
    breaks producers — caught here at boot."""
    del source
    import ast
    seen_class = False
    found: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ReasoningStream":
            seen_class = True
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(stmt.name)
    if not seen_class:
        return ("ReasoningStream class not found",)
    missing = _EXPECTED_REASONING_STREAM_METHODS - found
    if missing:
        return (
            f"ReasoningStream missing lifecycle methods: {sorted(missing)}",
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


_TARGET_FILE = "backend/core/ouroboros/governance/render_primitives.py"


def register_shipped_invariants() -> List:
    """Auto-discovered by shipped_code_invariants."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="render_primitives_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "render_primitives.py MUST NOT import rich.* — "
                "primitives are producer-side typed wrappers; rendering "
                "lives in backends. Substrate hygiene mirror of "
                "render_conductor's pin."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="render_primitives_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "render_primitives.py MUST NOT import any authority "
                "module (orchestrator / policy / iron_gate / risk_tier / "
                "change_engine / candidate_generator / gate / "
                "semantic_guardian / semantic_firewall / providers / "
                "doubleword_provider / urgency_router). Producer-side "
                "primitives stay descriptive only."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="render_primitives_fileref_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "FileRef field set MUST be exactly {path, line, column, "
                "anchor}. Adding/removing without coordinated "
                "to_metadata + closed-taxonomy pin update is structural "
                "drift — caught here at boot."
            ),
            validate=_validate_fileref_closed_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "render_primitives_reasoning_stream_lifecycle"
            ),
            target_file=_TARGET_FILE,
            description=(
                "ReasoningStream MUST define start / on_token / end. "
                "Refactors that drop a lifecycle method silently break "
                "producers — caught at AST."
            ),
            validate=_validate_reasoning_stream_lifecycle,
        ),
        ShippedCodeInvariant(
            invariant_name="render_primitives_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must be "
                "module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "FileRef",
    "RENDER_PRIMITIVES_SCHEMA_VERSION",
    "ReasoningStream",
    "file_ref_hyperlink_enabled",
    "get_reasoning_stream_callback",
    "publish_file_ref",
    "reasoning_stream_enabled",
    "register_flags",
    "register_shipped_invariants",
]
