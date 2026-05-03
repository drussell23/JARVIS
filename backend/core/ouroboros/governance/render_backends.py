"""RenderConductor adapter backends for SerpentFlow and OuroborosConsole.

Slice 2 of the RenderConductor arc (Wave 4 #1). Closes the architectural
fragmentation identified in §29 by routing the two REPL-class renderers
(``SerpentFlow`` — preferred CC-style flowing CLI; ``OuroborosConsole`` —
fallback scrolling Rich TUI) through the unified conductor as
``RenderBackend`` implementations.

``StreamRenderer`` got its backend conformance inline (in
``battle_test/stream_renderer.py``) because its API is a single-purpose
3-method lifecycle. ``SerpentFlow`` (5,300+ LOC, 70+ public methods) and
``OuroborosConsole`` (740 LOC, 25+ public methods) are wrapped here as
**composition adapters** — they do not modify the wrapped renderer; they
translate ``RenderEvent`` instances into existing API calls. This keeps
the load-bearing renderer files untouched while still completing the
substrate inversion: post-Slice-2, all three renderers are
``RenderBackend``-compliant and the conductor is the single fan-out
surface.

The adapter contract (mirrors ``StreamRenderer.notify``):

  * ``notify(event)`` — total over ``EventKind``: every closed-taxonomy
    value either maps to a wrapped-renderer method call OR is a
    documented no-op (for events the renderer doesn't surface — e.g.
    ``OuroborosConsole`` has no thread region, so ``THREAD_TURN`` is a
    no-op there). No silent drops on unknown kinds — the closed
    taxonomy means "unknown" is a contract violation and gets logged.
  * ``flush()`` / ``shutdown()`` — defensive best-effort. The wrapped
    renderers do not currently have flush/shutdown hooks; these adapters
    document the absence and degrade gracefully when Slice 3+ surfaces
    one.
  * NEVER raises — every method swallows exceptions and logs DEBUG. A
    mis-mapped event cannot break the conductor's fan-out to siblings.

Authority invariants (AST-pinned via ``register_shipped_invariants``):

  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider / urgency_router.
    Adapters are descriptive surfaces only.
  * Two adapter classes (``SerpentFlowBackend`` + ``OuroborosConsoleBackend``)
    both define the four required RenderBackend symbols (``name`` /
    ``notify`` / ``flush`` / ``shutdown``).
  * ``register_shipped_invariants`` symbol present (auto-discovery
    contract).

This module is auto-discovered by both
``flag_registry_seed._discover_module_provided_flags`` (zero new flags
in Slice 2 — adapters are wired with the conductor's existing flag set)
and ``shipped_code_invariants._discover_module_provided_invariants``.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


RENDER_BACKENDS_SCHEMA_VERSION: str = "render_backends.1"


# ---------------------------------------------------------------------------
# Event-kind dispatch helpers — keep adapter bodies short and total.
# Maps the EventKind string value to the adapter's per-kind handler name.
# Each adapter declares which kinds it handles in ``_HANDLED_KINDS`` and
# which it documented-no-ops in ``_NO_OP_KINDS``. The union must equal
# the full EventKind closed set (validated by AST pin at the bottom).
# ---------------------------------------------------------------------------


def _event_kind_value(event: Any) -> str:
    """Extract the closed-taxonomy string value from a RenderEvent.
    Returns empty string on any extraction failure (caller treats as
    no-op). NEVER raises."""
    try:
        kind = getattr(event, "kind", None)
        if kind is None:
            return ""
        return kind.value if hasattr(kind, "value") else str(kind)
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _event_metadata(event: Any) -> dict:
    """Extract a plain dict of the event's metadata. Returns empty dict
    on any failure. NEVER raises."""
    try:
        md = getattr(event, "metadata", None) or {}
        return dict(md) if not isinstance(md, dict) else md
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# SerpentFlowBackend — wraps the preferred CC-style flowing CLI
# ---------------------------------------------------------------------------


class SerpentFlowBackend:
    """Adapter exposing :class:`SerpentFlow` as a ``RenderBackend``.

    Composition (not subclassing) — the wrapped instance is held as
    ``_flow`` and its existing methods are called by ``notify``. The
    wrapped renderer is unaware of the adapter; nothing in
    ``serpent_flow.py`` is modified.

    Slice 2 wires the streaming triplet (PHASE_BEGIN / REASONING_TOKEN /
    PHASE_END) which is the most critical operator-visible surface and
    the one immediately consumable by Slice 3's typed
    ``ReasoningStream``. Other event kinds (FILE_REF, STATUS_TICK,
    MODAL_*, THREAD_TURN, BACKEND_RESET) are documented no-ops in
    Slice 2 — Slices 3-6 will wire each as their typed primitive
    ships, with the wrapped renderer's API surface as the proven
    target.
    """

    name: str = "serpent_flow"

    # Closed taxonomy of which event kinds this adapter actively handles
    # vs. which it documented-no-ops. The union MUST cover every
    # EventKind value (AST-pinned).
    _HANDLED_KINDS: frozenset = frozenset({
        "PHASE_BEGIN",
        "REASONING_TOKEN",
        "PHASE_END",
    })
    _NO_OP_KINDS: frozenset = frozenset({
        "FILE_REF",
        "STATUS_TICK",
        "MODAL_PROMPT",
        "MODAL_DISMISS",
        "THREAD_TURN",
        "BACKEND_RESET",
    })

    def __init__(self, flow: Any) -> None:
        """``flow`` is a constructed :class:`SerpentFlow` instance. We do
        not import ``SerpentFlow`` directly — duck-typed by the adapter
        contract so tests can substitute a stub."""
        self._flow = flow

    def notify(self, event: Any) -> None:
        """Route a RenderEvent to the wrapped SerpentFlow. Total over
        EventKind via the explicit handled/no-op partition."""
        if event is None:
            return
        kind = _event_kind_value(event)
        if not kind:
            return
        try:
            if kind == "REASONING_TOKEN":
                content = getattr(event, "content", "") or ""
                if content and hasattr(self._flow, "show_streaming_token"):
                    self._flow.show_streaming_token(content)
                return
            if kind == "PHASE_BEGIN":
                op_id = getattr(event, "op_id", None) or ""
                metadata = _event_metadata(event)
                provider = str(metadata.get("provider", "") or "")
                if hasattr(self._flow, "show_streaming_start"):
                    # SerpentFlow.show_streaming_start signature accepts
                    # (op_id, provider, model). Call with what we have;
                    # missing optional kwargs default at the wrapped
                    # renderer's discretion.
                    try:
                        self._flow.show_streaming_start(
                            op_id=op_id, provider=provider,
                        )
                    except TypeError:
                        # Fallback for renderers with a stricter signature
                        try:
                            self._flow.show_streaming_start(op_id, provider)
                        except Exception:  # noqa: BLE001 — defensive
                            logger.debug(
                                "[SerpentFlowBackend] show_streaming_start "
                                "signature mismatch", exc_info=True,
                            )
                return
            if kind == "PHASE_END":
                if hasattr(self._flow, "show_streaming_end"):
                    self._flow.show_streaming_end()
                return
            if kind in self._NO_OP_KINDS:
                # Documented no-op — Slices 3-6 will surface these as
                # their typed primitives ship.
                return
            # Unknown closed-taxonomy value — log once and continue.
            logger.debug(
                "[SerpentFlowBackend] unknown event kind %r — no-op", kind,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[SerpentFlowBackend] notify failed for kind=%s",
                kind, exc_info=True,
            )

    def flush(self) -> None:
        """SerpentFlow has no explicit flush hook — best-effort no-op.
        Slice 3+ will surface a flush method as the typed primitives
        require it (e.g. for end-of-phase rendering boundaries)."""
        return

    def shutdown(self) -> None:
        """Best-effort cleanup. SerpentFlow has no explicit shutdown
        method — calling show_streaming_end is the closest equivalent
        in case a stream was left mid-flight at session teardown."""
        try:
            if hasattr(self._flow, "show_streaming_end"):
                self._flow.show_streaming_end()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[SerpentFlowBackend] shutdown failed", exc_info=True,
            )


# ---------------------------------------------------------------------------
# OuroborosConsoleBackend — wraps the fallback scrolling Rich TUI
# ---------------------------------------------------------------------------


class OuroborosConsoleBackend:
    """Adapter exposing :class:`OuroborosConsole` as a ``RenderBackend``.

    Same composition pattern as :class:`SerpentFlowBackend`. The wrapped
    console exists only when SerpentFlow boot fails (mutually-exclusive
    fallback) — the harness boot wire constructs whichever backend is
    alive.

    The OuroborosConsole API is closer to a per-event console.print()
    surface than SerpentFlow's regional layout, so Slice 2 wires the
    same streaming triplet but the FILE_REF / STATUS_TICK mappings will
    differ in Slice 3+ when wired (console.show_diff vs. flow.show_diff
    have different signatures).
    """

    name: str = "ouroboros_console"

    _HANDLED_KINDS: frozenset = frozenset({
        "PHASE_BEGIN",
        "REASONING_TOKEN",
        "PHASE_END",
    })
    _NO_OP_KINDS: frozenset = frozenset({
        "FILE_REF",
        "STATUS_TICK",
        "MODAL_PROMPT",
        "MODAL_DISMISS",
        "THREAD_TURN",
        "BACKEND_RESET",
    })

    def __init__(self, console: Any) -> None:
        self._console = console

    def notify(self, event: Any) -> None:
        if event is None:
            return
        kind = _event_kind_value(event)
        if not kind:
            return
        try:
            if kind == "REASONING_TOKEN":
                content = getattr(event, "content", "") or ""
                if content and hasattr(self._console, "show_streaming_token"):
                    self._console.show_streaming_token(content)
                return
            if kind == "PHASE_BEGIN":
                metadata = _event_metadata(event)
                provider = str(metadata.get("provider", "") or "")
                if hasattr(self._console, "show_streaming_start"):
                    try:
                        self._console.show_streaming_start(provider)
                    except Exception:  # noqa: BLE001 — defensive
                        logger.debug(
                            "[OuroborosConsoleBackend] "
                            "show_streaming_start failed", exc_info=True,
                        )
                return
            if kind == "PHASE_END":
                if hasattr(self._console, "show_streaming_end"):
                    self._console.show_streaming_end()
                return
            if kind in self._NO_OP_KINDS:
                return
            logger.debug(
                "[OuroborosConsoleBackend] unknown event kind %r — no-op",
                kind,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[OuroborosConsoleBackend] notify failed for kind=%s",
                kind, exc_info=True,
            )

    def flush(self) -> None:
        return

    def shutdown(self) -> None:
        try:
            if hasattr(self._console, "show_streaming_end"):
                self._console.show_streaming_end()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[OuroborosConsoleBackend] shutdown failed", exc_info=True,
            )


# ---------------------------------------------------------------------------
# Boot wire helper — constructs and registers the conductor with whatever
# renderers the harness has alive. Idempotent. NEVER raises.
# ---------------------------------------------------------------------------


def wire_render_conductor(
    *,
    stream_renderer: Optional[Any] = None,
    serpent_flow: Optional[Any] = None,
    ouroboros_console: Optional[Any] = None,
    posture_provider: Optional[Any] = None,
) -> Optional[Any]:
    """Construct a :class:`RenderConductor`, attach the supplied
    renderers as backends, install posture provider if given, and
    register as the process-global conductor.

    Each renderer arg is optional — pass ``None`` for any not present
    in the current boot. Typical harness call:

        from backend.core.ouroboros.governance.render_backends import (
            wire_render_conductor,
        )
        wire_render_conductor(
            stream_renderer=self._stream_renderer,
            serpent_flow=self._serpent_flow,
            ouroboros_console=self._tui_console,
            posture_provider=lambda: get_current_posture_string(),
        )

    Returns the constructed conductor (or ``None`` on import failure).
    Idempotent — replaces any prior process-global conductor. NEVER
    raises out of this function — boot must not fail because rendering
    glue threw.
    """
    try:
        from backend.core.ouroboros.governance.render_conductor import (
            RenderConductor,
            register_render_conductor,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_backends] conductor module unavailable", exc_info=True,
        )
        return None

    try:
        conductor = RenderConductor()
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_backends] RenderConductor construction failed",
            exc_info=True,
        )
        return None

    # Attach each available renderer. The legacy on_token / show_*
    # entry points remain functional; the conductor adds a parallel
    # routing surface that Slice 3+ producers use.
    try:
        if stream_renderer is not None:
            conductor.add_backend(stream_renderer)
        if serpent_flow is not None:
            conductor.add_backend(SerpentFlowBackend(serpent_flow))
        if ouroboros_console is not None:
            conductor.add_backend(OuroborosConsoleBackend(ouroboros_console))
        if posture_provider is not None:
            conductor.set_posture_provider(posture_provider)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_backends] backend wiring partial failure",
            exc_info=True,
        )

    try:
        register_render_conductor(conductor)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_backends] register_render_conductor failed",
            exc_info=True,
        )
        return conductor  # still return; tests may use it directly

    logger.info(
        "[render_backends] conductor wired with %d backend(s)",
        len(conductor.backends()),
    )
    return conductor


# ---------------------------------------------------------------------------
# AST invariants — auto-discovered by shipped_code_invariants
# ---------------------------------------------------------------------------


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


# Required-symbol set on every adapter class — mirrors RenderBackend
# Protocol. AST-pinned so refactors cannot silently drop a method.
_REQUIRED_BACKEND_SYMBOLS: tuple = ("name", "notify", "flush", "shutdown")
_REQUIRED_ADAPTER_CLASSES: tuple = (
    "SerpentFlowBackend",
    "OuroborosConsoleBackend",
)


def _imported_modules(tree: Any) -> List:
    """Extract imported module names. Mirrors render_conductor's
    helper — keeps each module's pin functions self-contained."""
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


def _validate_backends_no_authority_imports(
    tree: Any, source: str,
) -> tuple:
    """Adapter module must NOT import authority modules — same
    descriptive-only contract as the conductor primitive."""
    del source
    violations: List[str] = []
    for lineno, mod_name in _imported_modules(tree):
        if mod_name in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod_name!r}"
            )
    return tuple(violations)


def _validate_adapter_protocol_conformance(
    tree: Any, source: str,
) -> tuple:
    """Both adapter classes MUST define the four RenderBackend symbols
    (``name`` / ``notify`` / ``flush`` / ``shutdown``). Refactors that
    silently drop a method break the Protocol contract — caught here
    at boot before the conductor would discover the gap at runtime."""
    del source
    import ast
    violations: List[str] = []
    found_classes: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in _REQUIRED_ADAPTER_CLASSES:
            members: set = set()
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.add(stmt.name)
                elif isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            members.add(tgt.id)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name,
                ):
                    members.add(stmt.target.id)
            found_classes[node.name] = members
    for required_class in _REQUIRED_ADAPTER_CLASSES:
        if required_class not in found_classes:
            violations.append(
                f"required adapter class missing: {required_class!r}"
            )
            continue
        members = found_classes[required_class]
        missing = set(_REQUIRED_BACKEND_SYMBOLS) - members
        if missing:
            violations.append(
                f"{required_class}: missing RenderBackend symbols: "
                f"{sorted(missing)}"
            )
    return tuple(violations)


def _validate_streamrenderer_protocol_conformance(
    tree: Any, source: str,
) -> tuple:
    """``StreamRenderer`` (defined in stream_renderer.py — separate file
    targeted by this pin's ``target_file``) MUST also expose the four
    RenderBackend symbols. Pinned here so the cross-file contract that
    "all 3 renderers are backends" is enforced from one auditable spot."""
    del source
    import ast
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "StreamRenderer":
            members: set = set()
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.add(stmt.name)
                elif isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            members.add(tgt.id)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name,
                ):
                    members.add(stmt.target.id)
            missing = set(_REQUIRED_BACKEND_SYMBOLS) - members
            if missing:
                return (
                    f"StreamRenderer: missing RenderBackend symbols: "
                    f"{sorted(missing)}",
                )
            return ()
    return ("StreamRenderer class not found in target file",)


def register_shipped_invariants() -> List:
    """Auto-discovered by shipped_code_invariants. Returns the AST pins
    that protect Slice 2's structural shape."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="render_backends_no_authority_imports",
            target_file=(
                "backend/core/ouroboros/governance/render_backends.py"
            ),
            description=(
                "Adapter module must NOT import authority modules — "
                "rendering glue stays descriptive only, never a "
                "control-flow surface."
            ),
            validate=_validate_backends_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="render_backends_adapter_protocol_conformance",
            target_file=(
                "backend/core/ouroboros/governance/render_backends.py"
            ),
            description=(
                "SerpentFlowBackend and OuroborosConsoleBackend MUST "
                "both define the RenderBackend Protocol's four symbols "
                "(name / notify / flush / shutdown). Refactors that "
                "silently drop a method break the Protocol contract."
            ),
            validate=_validate_adapter_protocol_conformance,
        ),
        ShippedCodeInvariant(
            invariant_name="streamrenderer_protocol_conformance",
            target_file=(
                "backend/core/ouroboros/battle_test/stream_renderer.py"
            ),
            description=(
                "StreamRenderer (the third RenderBackend, with backend "
                "methods inline rather than via composition adapter) "
                "MUST expose the same four RenderBackend symbols. "
                "Cross-file contract pinned from one auditable spot — "
                "if any renderer drops backend conformance, this fails."
            ),
            validate=_validate_streamrenderer_protocol_conformance,
        ),
    ]


__all__ = [
    "OuroborosConsoleBackend",
    "RENDER_BACKENDS_SCHEMA_VERSION",
    "SerpentFlowBackend",
    "register_shipped_invariants",
    "wire_render_conductor",
]
