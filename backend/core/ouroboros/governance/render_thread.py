"""ThreadTurn primitive + ThreadObserver — visible conversational thread.

Slice 5 of the RenderConductor arc (Wave 4 #1). Closes Gap #7:
``ConversationBridge`` already stores sanitized dialogue in a ring
buffer with Tier -1 redaction, but there is no rendering surface —
operators can't see "you said X / I responded Y / now Z" the way CC
shows it. Slice 5 wires the bridge's existing ``register_turn_observer``
push-fan-out into the conductor by translating each
:class:`ConversationTurn` into a typed :class:`ThreadTurn` and
publishing a ``THREAD_TURN`` event into the
:class:`RegionKind.THREAD` slot. Slice 2 already reserved the region
and event-kind for this purpose; backends route THREAD_TURN events
to the appropriate visual treatment at Slice 7.

Architectural pillars:

  1. **Push, not poll** — ConversationBridge already has
     ``register_turn_observer(callable)`` (sync fan-out, advisory,
     swallows exceptions). Slice 5 plugs in via that contract; no
     parallel polling task. Bridge's own never-raise guarantee
     subsumes the observer's failure mode.
  2. **Closed-taxonomy Speaker** — frozen 5-value enum
     ({USER, ASSISTANT, TOOL, POSTMORTEM, SYSTEM}) maps the bridge's
     ``Role`` × ``source`` cross-product into a stable rendering
     category. AST-pinned. Adding a Speaker requires coordinated
     registry update.
  3. **No authority imports + no hard bridge import** — the substrate
     does not import :mod:`conversation_bridge` at module level.
     :class:`ThreadObserver` performs a lazy import inside
     :meth:`start`; if the bridge module isn't available (test
     isolation, partial install), start returns ``False`` and the
     substrate stays inert.
  4. **No hardcoded values** — speaker mapping table is in-code
     default; operator overlay via ``JARVIS_THREAD_SPEAKER_MAPPING``
     JSON map (``{source: speaker}``). Master flag
     ``JARVIS_THREAD_OBSERVER_ENABLED`` default false at Slice 5;
     graduates with the conductor at Slice 7.
  5. **Defensive everywhere** — translate / publish methods swallow
     exceptions and log DEBUG. The observer's contract with the
     bridge is "never raise"; substrate honors it by construction.
  6. **Bridge stays alive when observer disabled** — descriptive vs
     authoritative split. The bridge owns the storage; the observer
     owns the rendering surface. Either can be enabled / disabled
     independently. Identical pattern to the conductor / backend
     graduation cycle in Slice 1-2.

Authority invariants (AST-pinned via ``register_shipped_invariants``):

  * No imports of ``rich`` / ``rich.*``.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / cancel_token / conversation_bridge. The bridge
    binding is via lazy import inside :meth:`ThreadObserver.start`,
    not a top-level import — keeps the substrate descriptive only.
  * :class:`Speaker` member set is the documented closed set.
  * :class:`ThreadTurn` field set is exactly
    ``{speaker, content, source, op_id, monotonic_ts}``.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switches:

  * ``JARVIS_THREAD_OBSERVER_ENABLED`` — master gate. Default false.
    Graduates with conductor at Slice 7.
  * ``JARVIS_THREAD_SPEAKER_MAPPING`` — JSON overlay on default
    source→speaker map. Empty / missing falls through to defaults.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


RENDER_THREAD_SCHEMA_VERSION: str = "render_thread.1"


_FLAG_THREAD_OBSERVER_ENABLED = "JARVIS_THREAD_OBSERVER_ENABLED"
_FLAG_THREAD_SPEAKER_MAPPING = "JARVIS_THREAD_SPEAKER_MAPPING"


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
    """Master gate. Default ``false`` at Slice 5 — graduates with the
    conductor at Slice 7. When off, :meth:`ThreadObserver.start` returns
    immediately without registering on the bridge; the bridge keeps
    storing turns for its CONTEXT_EXPANSION consumer (descriptive vs
    rendering split)."""
    reg = _get_registry()
    if reg is None:
        return False
    return reg.get_bool(_FLAG_THREAD_OBSERVER_ENABLED, default=False)


def speaker_mapping_override() -> Mapping[str, "Speaker"]:
    """Operator overlay on default source→speaker mapping. JSON object
    mapping ConversationBridge source strings to :class:`Speaker`
    values. Unmapped sources fall back to the in-code default. Malformed
    entries silently skipped (logged DEBUG)."""
    reg = _get_registry()
    if reg is None:
        return {}
    raw = reg.get_json(_FLAG_THREAD_SPEAKER_MAPPING, default=None)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Speaker] = {}
    for src, spk in raw.items():
        if not isinstance(src, str) or not isinstance(spk, str):
            continue
        try:
            speaker = Speaker(spk.strip().upper())
        except ValueError:
            logger.debug(
                "[render_thread] unknown Speaker in override: %s", spk,
            )
            continue
        out[src.strip()] = speaker
    return out


# ---------------------------------------------------------------------------
# Speaker — closed-taxonomy rendering category (AST-pinned)
# ---------------------------------------------------------------------------


class Speaker(str, enum.Enum):
    """Closed taxonomy of thread-turn speakers. Maps ConversationBridge's
    ``Role`` × ``source`` cross-product into a stable rendering slot.

    The mapping is intentional, not exhaustive — adding a Speaker
    requires updating the AST pin AND the in-code default mapping.
    Operators may override the source→Speaker map via
    ``JARVIS_THREAD_SPEAKER_MAPPING`` (JSON), but the Speaker set
    itself is closed."""

    USER = "USER"           # operator-authored turns (tui, voice, ask_human_a)
    ASSISTANT = "ASSISTANT"  # model-authored turns (ask_human_q)
    TOOL = "TOOL"           # reserved — Slice 6+ tool-loop turns
    POSTMORTEM = "POSTMORTEM"
    SYSTEM = "SYSTEM"       # reserved — system messages (not yet emitted)


# Default source → Speaker mapping. Mirrors ConversationBridge's known
# sources (SOURCE_TUI_USER / SOURCE_ASK_HUMAN_Q / SOURCE_ASK_HUMAN_A /
# SOURCE_POSTMORTEM / SOURCE_VOICE). The string keys deliberately
# reproduce the bridge's source values — we don't import them to keep
# this module's no-conversation-bridge-import invariant clean.
_DEFAULT_SOURCE_TO_SPEAKER: Mapping[str, Speaker] = {
    "tui_user":     Speaker.USER,
    "voice":        Speaker.USER,
    "ask_human_q":  Speaker.ASSISTANT,
    "ask_human_a":  Speaker.USER,
    "postmortem":   Speaker.POSTMORTEM,
}


def resolve_speaker(source: str, *, role: str = "") -> Speaker:
    """Resolve the rendering :class:`Speaker` for a bridge turn.

    Precedence:
      1. Operator override via ``JARVIS_THREAD_SPEAKER_MAPPING``.
      2. In-code default ``_DEFAULT_SOURCE_TO_SPEAKER``.
      3. Fallback by ``role``: ``"user"`` → USER; ``"assistant"`` →
         ASSISTANT; anything else → SYSTEM.
    """
    if not isinstance(source, str):
        source = ""
    overrides = speaker_mapping_override()
    if source in overrides:
        return overrides[source]
    if source in _DEFAULT_SOURCE_TO_SPEAKER:
        return _DEFAULT_SOURCE_TO_SPEAKER[source]
    role_norm = (role or "").strip().lower()
    if role_norm == "user":
        return Speaker.USER
    if role_norm == "assistant":
        return Speaker.ASSISTANT
    return Speaker.SYSTEM


# ---------------------------------------------------------------------------
# ThreadTurn — frozen typed primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadTurn:
    """One typed conversation turn. Closes Gap #7's "no rendering
    surface for the conversation thread".

    Frozen + hashable. The five fields together represent the
    addressable state of a single turn. ``source`` preserves the
    bridge's precise origin (``"tui_user"`` etc.) alongside the
    rendering :class:`Speaker` — backends can use whichever they
    prefer for visual differentiation.
    """

    speaker: Speaker
    content: str
    source: str = ""
    op_id: Optional[str] = None
    monotonic_ts: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError(
                f"ThreadTurn.content must be a string, "
                f"got {type(self.content).__name__}"
            )
        if not isinstance(self.source, str):
            raise ValueError(
                f"ThreadTurn.source must be a string, got {self.source!r}"
            )
        if self.op_id is not None and not isinstance(self.op_id, str):
            raise ValueError(
                f"ThreadTurn.op_id must be string or None, "
                f"got {self.op_id!r}"
            )

    def to_metadata(self) -> Dict[str, Any]:
        """Serialize for embedding in :class:`RenderEvent.metadata`.
        Preserves all five fields + a schema_version so backend
        consumers can pin a contract."""
        return {
            "schema_version": RENDER_THREAD_SCHEMA_VERSION,
            "kind": "thread_turn",
            "speaker": self.speaker.value,
            "content": self.content,
            "source": self.source,
            "op_id": self.op_id,
            "monotonic_ts": self.monotonic_ts,
        }

    @classmethod
    def from_metadata(
        cls, payload: Mapping[str, Any],
    ) -> Optional["ThreadTurn"]:
        """Defensive inverse of :meth:`to_metadata`. Returns ``None``
        on malformed payload — never raises."""
        try:
            if not isinstance(payload, Mapping):
                return None
            content = payload.get("content")
            if not isinstance(content, str):
                return None
            speaker_val = payload.get("speaker")
            if not isinstance(speaker_val, str):
                return None
            try:
                speaker = Speaker(speaker_val.strip().upper())
            except ValueError:
                return None
            source = payload.get("source", "")
            if not isinstance(source, str):
                source = ""
            op_id = payload.get("op_id")
            if op_id is not None and not isinstance(op_id, str):
                op_id = None
            ts = payload.get("monotonic_ts", time.monotonic())
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = time.monotonic()
            return cls(
                speaker=speaker, content=content, source=source,
                op_id=op_id, monotonic_ts=ts,
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# publish_thread_turn — producer-side helper
# ---------------------------------------------------------------------------


def publish_thread_turn(
    turn: ThreadTurn,
    *,
    source_module: str,
    extra_metadata: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Publish a THREAD_TURN event into the conductor.

    Returns ``True`` when the event was constructed and the conductor's
    ``publish`` was invoked. Returns ``False`` only on import-degradation
    edge cases (no conductor module available) or no conductor
    registered. The conductor's master flag still gates backend
    delivery (publish is no-op when off) — this helper does not
    second-guess the conductor's gate.
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
    metadata = dict(turn.to_metadata())
    if extra_metadata:
        try:
            metadata.update(dict(extra_metadata))
        except Exception:  # noqa: BLE001 — defensive
            pass
    # Speaker drives the role: USER stands out (EMPHASIS),
    # ASSISTANT/POSTMORTEM are normal CONTENT, SYSTEM/TOOL are MUTED.
    role_for_speaker = {
        Speaker.USER: ColorRole.EMPHASIS,
        Speaker.ASSISTANT: ColorRole.CONTENT,
        Speaker.POSTMORTEM: ColorRole.MUTED,
        Speaker.TOOL: ColorRole.MUTED,
        Speaker.SYSTEM: ColorRole.METADATA,
    }
    role = role_for_speaker.get(turn.speaker, ColorRole.CONTENT)
    try:
        event = RenderEvent(
            kind=EventKind.THREAD_TURN,
            region=RegionKind.THREAD,
            role=role,
            content=turn.content,
            source_module=source_module,
            op_id=turn.op_id,
            metadata=metadata,
        )
        conductor.publish(event)
        return True
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_thread] publish_thread_turn failed", exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# ThreadObserver — bridge → conductor sync pump (push-driven)
# ---------------------------------------------------------------------------


class ThreadObserver:
    """Sync observer that translates each :class:`ConversationTurn`
    admitted to the :class:`ConversationBridge` into a typed
    :class:`ThreadTurn` and publishes it into the
    :class:`RenderConductor`.

    Lifecycle:

      * :meth:`start` — checks master flag, lazily imports the bridge
        singleton, registers ``self._on_turn`` as a turn observer.
        Returns ``True`` on success, ``False`` on any documented
        degradation (master off, bridge unavailable, double-start).
        Idempotent — second start is a no-op.
      * :meth:`stop` — unregisters from the bridge. Idempotent.
      * :meth:`_on_turn` — the bridge's record_turn fast-path invokes
        this synchronously. Translates → publishes. NEVER raises
        (bridge contract).

    Master flag default false at Slice 5; the bridge keeps storing
    turns for its CONTEXT_EXPANSION consumer regardless. Slice 7 flips
    the master and the harness wires the observer at boot.
    """

    def __init__(self, *, source_module: str = "render_thread.ThreadObserver"):
        self._source_module = source_module
        self._registered: bool = False
        self._bridge_ref: Optional[Any] = None
        self._lock = threading.Lock()
        self._turn_count: int = 0

    @property
    def active(self) -> bool:
        return self._registered

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def start(self, *, bridge: Optional[Any] = None) -> bool:
        """Register on the bridge. Returns ``True`` when registration
        succeeded; ``False`` for any documented degradation. Idempotent.

        ``bridge`` (optional): inject a specific bridge instance for
        tests; production calls omit and the singleton is resolved via
        lazy import."""
        with self._lock:
            if self._registered:
                return True
            if not is_enabled():
                return False
            target = bridge
            if target is None:
                try:
                    from backend.core.ouroboros.governance import (
                        conversation_bridge as _cb,
                    )
                    target = _cb.get_default_bridge()
                except Exception:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[ThreadObserver] bridge import failed",
                        exc_info=True,
                    )
                    return False
            if target is None or not hasattr(
                target, "register_turn_observer",
            ):
                return False
            try:
                target.register_turn_observer(self._on_turn)
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[ThreadObserver] register_turn_observer failed",
                    exc_info=True,
                )
                return False
            self._bridge_ref = target
            self._registered = True
            return True

    def stop(self) -> None:
        """Unregister from the bridge. Idempotent."""
        with self._lock:
            if not self._registered:
                return
            target = self._bridge_ref
            self._registered = False
            self._bridge_ref = None
        if target is not None and hasattr(target, "unregister_turn_observer"):
            try:
                target.unregister_turn_observer(self._on_turn)
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[ThreadObserver] unregister failed", exc_info=True,
                )

    # -- internals -----------------------------------------------------

    def _on_turn(self, turn: Any) -> None:
        """Bridge's sync fan-out hook. Translates ``ConversationTurn``
        → :class:`ThreadTurn` → publish. NEVER raises (bridge contract)."""
        try:
            speaker = resolve_speaker(
                source=getattr(turn, "source", "") or "",
                role=getattr(turn, "role", "") or "",
            )
            content = getattr(turn, "text", "") or ""
            if not isinstance(content, str):
                return
            op_id_raw = getattr(turn, "op_id", "")
            op_id: Optional[str] = (
                op_id_raw if isinstance(op_id_raw, str) and op_id_raw
                else None
            )
            ts_raw = getattr(turn, "ts", None)
            try:
                ts = float(ts_raw) if ts_raw is not None else time.monotonic()
            except (TypeError, ValueError):
                ts = time.monotonic()
            thread_turn = ThreadTurn(
                speaker=speaker,
                content=content,
                source=getattr(turn, "source", "") or "",
                op_id=op_id,
                monotonic_ts=ts,
            )
            published = publish_thread_turn(
                thread_turn, source_module=self._source_module,
            )
            if published:
                self._turn_count += 1
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ThreadObserver] _on_turn translation failed",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Singleton triplet — mirrors RenderConductor / InputController pattern
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[ThreadObserver] = None
_DEFAULT_LOCK = threading.Lock()


def get_thread_observer() -> Optional[ThreadObserver]:
    with _DEFAULT_LOCK:
        return _DEFAULT_OBSERVER


def register_thread_observer(observer: Optional[ThreadObserver]) -> None:
    global _DEFAULT_OBSERVER
    with _DEFAULT_LOCK:
        _DEFAULT_OBSERVER = observer


def reset_thread_observer() -> None:
    register_thread_observer(None)


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
            name=_FLAG_THREAD_OBSERVER_ENABLED,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master gate for the ThreadObserver substrate (Wave 4 "
                "#1, Slice 5). When false, the observer doesn't "
                "register on ConversationBridge — bridge keeps storing "
                "turns for its CONTEXT_EXPANSION consumer (descriptive "
                "vs rendering split). Graduates with the conductor at "
                "Slice 7."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/render_thread.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_THREAD_SPEAKER_MAPPING,
            type=FlagType.JSON,
            default=None,
            description=(
                "Operator overlay on the default source→Speaker map. "
                "JSON object mapping ConversationBridge source strings "
                "(tui_user / ask_human_q / ask_human_a / postmortem / "
                "voice) to Speaker values (USER / ASSISTANT / TOOL / "
                "POSTMORTEM / SYSTEM). Unmapped sources fall back to "
                "the in-code default. Malformed entries silently "
                "skipped."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/render_thread.py"
            ),
            example='{"voice": "ASSISTANT"}',
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
)


_EXPECTED_SPEAKER = frozenset({
    "USER", "ASSISTANT", "TOOL", "POSTMORTEM", "SYSTEM",
})
_EXPECTED_THREAD_TURN_FIELDS = frozenset({
    "speaker", "content", "source", "op_id", "monotonic_ts",
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
    """render_thread MUST NOT import any authority module — including
    conversation_bridge (the bridge binding is via lazy import inside
    ThreadObserver.start; substrate stays descriptive-only)."""
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_speaker_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_enum_member_names(tree, "Speaker"))
    if found != set(_EXPECTED_SPEAKER):
        return (
            f"Speaker members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_SPEAKER)}",
        )
    return ()


def _validate_thread_turn_closed_taxonomy(
    tree: Any, source: str,
) -> tuple:
    """ThreadTurn dataclass MUST contain exactly the five documented
    fields. Adding/removing without coordinated to_metadata + AST pin
    update is structural drift caught here."""
    del source
    import ast
    found: set = set()
    seen_class = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ThreadTurn":
            seen_class = True
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name,
                ):
                    found.add(stmt.target.id)
    if not seen_class:
        return ("ThreadTurn class not found",)
    if found != _EXPECTED_THREAD_TURN_FIELDS:
        return (
            f"ThreadTurn fields {sorted(found)} != expected "
            f"{sorted(_EXPECTED_THREAD_TURN_FIELDS)}",
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


_TARGET_FILE = "backend/core/ouroboros/governance/render_thread.py"


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="render_thread_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "render_thread.py MUST NOT import rich.* — substrate "
                "speaks ThreadTurn primitives only; rendering belongs "
                "to backends consuming THREAD_TURN events."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="render_thread_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "render_thread.py MUST NOT import any authority module "
                "OR conversation_bridge at top level. The bridge "
                "binding is via lazy import inside ThreadObserver.start "
                "— keeps the substrate descriptive only."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="render_thread_speaker_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "Speaker enum members must exactly match the "
                "documented 5-value closed set (USER, ASSISTANT, TOOL, "
                "POSTMORTEM, SYSTEM). Adding a Speaker requires "
                "coordinated registry update."
            ),
            validate=_validate_speaker_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="render_thread_thread_turn_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "ThreadTurn field set MUST be exactly {speaker, "
                "content, source, op_id, monotonic_ts}. Adding/removing "
                "without coordinated to_metadata + closed-taxonomy pin "
                "update is structural drift — caught here at boot."
            ),
            validate=_validate_thread_turn_closed_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="render_thread_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must be "
                "module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "RENDER_THREAD_SCHEMA_VERSION",
    "Speaker",
    "ThreadObserver",
    "ThreadTurn",
    "get_thread_observer",
    "is_enabled",
    "publish_thread_turn",
    "register_flags",
    "register_shipped_invariants",
    "register_thread_observer",
    "reset_thread_observer",
    "resolve_speaker",
    "speaker_mapping_override",
]
