"""StatusLineComposer — single composed status line aggregating ambient state.

Closes the "8+ separate ``update_*`` methods spam separate console
lines" UX gap. CC has ONE persistent status line (model | tokens |
cost | time elapsed). O+V's ``update_intent_discovery`` /
``update_dream_engine`` / ``update_learning`` / ``update_session_-
lessons`` / etc. each emit their own line, producing scattered status
chatter that's hard to scan.

This substrate aggregates per-field contributions into a single
composed line that updates in place via the existing prompt_toolkit
``bottom_toolbar`` surface (``SerpentFlow._spinner_state.message``).
Every contributor calls :meth:`set(field, value)`; the composer
debounces, composes, and publishes a single STATUS_TICK event into
the :class:`RenderConductor` whenever the line changes.

Architectural pillars (each load-bearing):

  1. **Closed-taxonomy StatusField** — ``{COST, SENSORS,
     PROVIDER_CHAIN, INTENT_CHAIN, POSTURE, SESSION_LESSONS,
     INTENT_DISCOVERY, DREAM_ENGINE, LEARNING}``. AST-pinned. Adding
     a field requires coordinated formatter update.
  2. **Operator-overrideable field order** — JARVIS_STATUS_LINE_FIELDS
     JSON list (subset of StatusField values). Default order is
     in-code; operators reorder/exclude via env. Unknown fields
     silently skipped.
  3. **Debounced publish** — multiple set() calls within
     ``JARVIS_STATUS_LINE_DEBOUNCE_MS`` (default 50ms) coalesce into
     one STATUS_TICK. Prevents publish-storm during rapid-fire updates
     (e.g., dream_engine + learning + lessons firing in same tick).
  4. **No hardcoded formats** — every per-field formatter is a closed-
     taxonomy callable in `_FIELD_FORMATTERS`. Operators may shadow
     a formatter via JARVIS_STATUS_LINE_FORMAT_OVERRIDE JSON
     (``{field: format_template}``). Templates use Python str.format
     positional/named args.
  5. **Single source of truth** — the composer owns the displayed
     state. SerpentFlow's existing per-field attrs (`_cost_total`,
     `_sensors_active`) stay for downstream consumers but the bottom
     toolbar reads ONLY from the composer's compose() output.
  6. **Defensive everywhere** — every set/compose/publish swallows
     exceptions. A formatter that raises returns "(?)". A bad event
     publish doesn't break the contributor's call. Boot is never
     blocked by status glue.

Authority invariants (AST-pinned):

  * No imports of ``rich`` / ``rich.*``.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / cancel_token / conversation_bridge /
    serpent_flow.
  * :class:`StatusField` member set is the documented closed set.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switches:

  * ``JARVIS_STATUS_LINE_COMPOSER_ENABLED`` — master gate. Default
    ``false`` (substrate ships dormant). Hot-revert preserved.
  * ``JARVIS_STATUS_LINE_FIELDS`` — JSON list of field names + order.
    Default in-code: ``[COST, POSTURE, SENSORS, PROVIDER_CHAIN,
    INTENT_DISCOVERY, DREAM_ENGINE, LEARNING, SESSION_LESSONS]``.
    Empty / missing → in-code default.
  * ``JARVIS_STATUS_LINE_DEBOUNCE_MS`` — int (default 50). Min clamp 0
    (no debounce — fire on every set()). Max clamp 5000.
  * ``JARVIS_STATUS_LINE_SEPARATOR`` — str (default ``" | "``).
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


STATUS_LINE_COMPOSER_SCHEMA_VERSION: str = "status_line_composer.1"


_FLAG_STATUS_LINE_COMPOSER_ENABLED = "JARVIS_STATUS_LINE_COMPOSER_ENABLED"
_FLAG_STATUS_LINE_FIELDS = "JARVIS_STATUS_LINE_FIELDS"
_FLAG_STATUS_LINE_DEBOUNCE_MS = "JARVIS_STATUS_LINE_DEBOUNCE_MS"
_FLAG_STATUS_LINE_SEPARATOR = "JARVIS_STATUS_LINE_SEPARATOR"


# ---------------------------------------------------------------------------
# StatusField — closed taxonomy
# ---------------------------------------------------------------------------


class StatusField(str, enum.Enum):
    """Closed taxonomy of status-line contributor fields.

    Adding a field requires:
      1. New enum member here
      2. New entry in :data:`_FIELD_FORMATTERS`
      3. New entry in :data:`_DEFAULT_FIELD_ORDER` (or operator-
         configured)
      4. Update to the AST closed-taxonomy pin
    """

    COST = "COST"
    SENSORS = "SENSORS"
    PROVIDER_CHAIN = "PROVIDER_CHAIN"
    INTENT_CHAIN = "INTENT_CHAIN"
    POSTURE = "POSTURE"
    SESSION_LESSONS = "SESSION_LESSONS"
    INTENT_DISCOVERY = "INTENT_DISCOVERY"
    DREAM_ENGINE = "DREAM_ENGINE"
    LEARNING = "LEARNING"
    # CC2 additions — surface what's happening RIGHT NOW.
    ACTIVE_OP = "ACTIVE_OP"          # most recent INTENT — "TestFailure(op7c17)"
    TASK_LIST = "TASK_LIST"          # in-flight summary — "3 active · 1 queued"


# ---------------------------------------------------------------------------
# Per-field formatters — closed-taxonomy mapping
# ---------------------------------------------------------------------------


def _format_cost(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "$?"


def _format_sensors(value: Any) -> str:
    try:
        return f"{int(value)} sensors"
    except (TypeError, ValueError):
        return "?"


def _format_provider_chain(value: Any) -> str:
    s = str(value or "")
    return s[:32] if s else ""


def _format_intent_chain(value: Any) -> str:
    s = str(value or "")
    return s[:48] if s else ""


def _format_posture(value: Any) -> str:
    s = str(value or "").strip().upper()
    return s if s else "?"


def _format_session_lessons(value: Any) -> str:
    try:
        n = int(value)
        return f"{n} lesson{'s' if n != 1 else ''}"
    except (TypeError, ValueError):
        return ""


def _format_intent_discovery(value: Any) -> str:
    # value is dict {cycle, submitted}
    if isinstance(value, dict):
        cycle = value.get("cycle", 0)
        submitted = value.get("submitted", 0)
        return f"intents:{cycle}/{submitted}"
    return str(value or "")


def _format_dream_engine(value: Any) -> str:
    if isinstance(value, dict):
        n = value.get("blueprints", 0)
        return f"💭 {n}"
    try:
        return f"💭 {int(value)}"
    except (TypeError, ValueError):
        return ""


def _format_learning(value: Any) -> str:
    if isinstance(value, dict):
        n = value.get("rules", 0)
        trend = value.get("trend", "→")
        return f"📖 {n} {trend}"
    return str(value or "")


def _format_active_op(value: Any) -> str:
    """ACTIVE_OP renders as the current Sensor(short_id) — the
    'what's happening now' anchor. Empty string when no op active."""
    s = str(value or "").strip()
    if not s:
        return ""
    return s[:48]


def _format_task_list(value: Any) -> str:
    """TASK_LIST renders as compact counts: '3 active · 1 queued ·
    12 done'. Dict input expected; missing keys default to 0."""
    if isinstance(value, dict):
        active = int(value.get("active", 0) or 0)
        queued = int(value.get("queued", 0) or 0)
        done = int(value.get("done", 0) or 0)
        parts: List[str] = []
        if active:
            parts.append(f"{active} active")
        if queued:
            parts.append(f"{queued} queued")
        if done:
            parts.append(f"{done} done")
        return " · ".join(parts) if parts else ""
    return str(value or "")


_FIELD_FORMATTERS: Mapping[StatusField, Callable[[Any], str]] = {
    StatusField.COST:              _format_cost,
    StatusField.SENSORS:           _format_sensors,
    StatusField.PROVIDER_CHAIN:    _format_provider_chain,
    StatusField.INTENT_CHAIN:      _format_intent_chain,
    StatusField.POSTURE:           _format_posture,
    StatusField.SESSION_LESSONS:   _format_session_lessons,
    StatusField.INTENT_DISCOVERY:  _format_intent_discovery,
    StatusField.DREAM_ENGINE:      _format_dream_engine,
    StatusField.LEARNING:          _format_learning,
    StatusField.ACTIVE_OP:         _format_active_op,
    StatusField.TASK_LIST:         _format_task_list,
}


_DEFAULT_FIELD_ORDER: Tuple[StatusField, ...] = (
    StatusField.ACTIVE_OP,        # what's happening now — first
    StatusField.TASK_LIST,        # in-flight count — second
    StatusField.POSTURE,
    StatusField.COST,
    StatusField.SENSORS,
    StatusField.PROVIDER_CHAIN,
    StatusField.INTENT_DISCOVERY,
    StatusField.DREAM_ENGINE,
    StatusField.LEARNING,
    StatusField.SESSION_LESSONS,
    StatusField.INTENT_CHAIN,
)


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
    """Master gate. Graduated default ``true`` at D5 — composer
    aggregates ambient state into the bottom_toolbar's single
    composed line by default. Hot-revert via
    ``JARVIS_STATUS_LINE_COMPOSER_ENABLED=false`` returns to legacy
    behavior (each ``update_*`` method emits its own console line)."""
    reg = _get_registry()
    if reg is None:
        return True
    return reg.get_bool(
        _FLAG_STATUS_LINE_COMPOSER_ENABLED, default=True,
    )


def field_order() -> Tuple[StatusField, ...]:
    """Resolved field order. Operator overlay via JSON list."""
    reg = _get_registry()
    if reg is None:
        return _DEFAULT_FIELD_ORDER
    raw = reg.get_json(_FLAG_STATUS_LINE_FIELDS, default=None)
    if not isinstance(raw, list):
        return _DEFAULT_FIELD_ORDER
    out: List[StatusField] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        try:
            out.append(StatusField(item.strip().upper()))
        except ValueError:
            logger.debug(
                "[status_line_composer] unknown field in order: %r", item,
            )
            continue
    return tuple(out) if out else _DEFAULT_FIELD_ORDER


def debounce_ms() -> int:
    reg = _get_registry()
    if reg is None:
        return 50
    return max(0, min(5000, reg.get_int(
        _FLAG_STATUS_LINE_DEBOUNCE_MS, default=50, minimum=0,
    )))


def separator() -> str:
    reg = _get_registry()
    if reg is None:
        return " | "
    raw = reg.get_str(_FLAG_STATUS_LINE_SEPARATOR, default=" | ")
    return raw if raw else " | "


# ---------------------------------------------------------------------------
# StatusLineComposer — the single composed line
# ---------------------------------------------------------------------------


class StatusLineComposer:
    """Aggregates per-field contributions into one composed status line.

    Lifecycle:
      * Contributors call ``composer.set(field, value)`` whenever a
        relevant signal changes (cost, sensors, lessons, etc.).
      * The composer debounces (default 50ms) then publishes a single
        STATUS_TICK event into the conductor with the composed string
        as event.content.
      * Backends consume STATUS_TICK and update their bottom_toolbar
        surface (SerpentFlow updates ``_spinner_state.message``).

    State model:
      * Fields stored in dict; latest value per field wins.
      * Compose order driven by :func:`field_order` (operator-
        overrideable).
      * Empty-string formatter results omitted from compose output
        (so unset fields don't show as spurious separators).

    Defensive everywhere:
      * set() with bad field → silently skipped.
      * Formatter exception → field renders as ``(?)``.
      * Conductor publish failure → swallowed; debounce timer reset
        so next set() will retry.
    """

    def __init__(
        self, *,
        source_module: str = "status_line_composer.StatusLineComposer",
    ) -> None:
        self._source_module = source_module
        self._fields: Dict[StatusField, Any] = {}
        self._lock = threading.Lock()
        self._last_publish_monotonic: float = 0.0
        self._pending_publish: bool = False
        # Lazy-imported; reset on each publish.
        self._publish_timer: Optional[threading.Timer] = None

    # -- public API ----------------------------------------------------

    def set(self, field: Any, value: Any) -> None:
        """Update a contributor field. Debounced — multiple calls
        within the debounce window coalesce into one publish.
        NEVER raises; bad input silently skipped."""
        if not is_enabled():
            return
        try:
            if isinstance(field, str):
                resolved = StatusField(field.strip().upper())
            elif isinstance(field, StatusField):
                resolved = field
            else:
                return
        except (ValueError, AttributeError):
            return
        with self._lock:
            self._fields[resolved] = value
        self._schedule_publish()

    def clear(self, field: Any = None) -> None:
        """Clear one field (or all if ``field`` is None). Useful for
        ops ending — clears intent_discovery/dream_engine state so
        the line doesn't show stale info."""
        with self._lock:
            if field is None:
                self._fields.clear()
            else:
                try:
                    resolved = (
                        StatusField(field.strip().upper())
                        if isinstance(field, str) else field
                    )
                    self._fields.pop(resolved, None)
                except (ValueError, AttributeError):
                    return
        self._schedule_publish()

    def compose(self) -> str:
        """Return the composed status string. Order from
        :func:`field_order`; empty per-field results omitted."""
        with self._lock:
            fields = dict(self._fields)
        order = field_order()
        sep = separator()
        parts: List[str] = []
        for f in order:
            if f not in fields:
                continue
            try:
                formatter = _FIELD_FORMATTERS.get(f)
                if formatter is None:
                    continue
                rendered = formatter(fields[f])
            except Exception:  # noqa: BLE001 — defensive
                rendered = "(?)"
            if rendered:
                parts.append(rendered)
        return sep.join(parts)

    def snapshot(self) -> Mapping[StatusField, Any]:
        """Read-only field snapshot — for /render observers and tests."""
        with self._lock:
            return dict(self._fields)

    # -- internals -----------------------------------------------------

    def _schedule_publish(self) -> None:
        """Debounced publish. Uses threading.Timer for the wait —
        cancels any prior pending timer before scheduling new one.
        On fire, publishes via :meth:`_publish_now`."""
        delay_ms = debounce_ms()
        if delay_ms <= 0:
            self._publish_now()
            return
        with self._lock:
            if self._publish_timer is not None:
                try:
                    self._publish_timer.cancel()
                except Exception:  # noqa: BLE001 — defensive
                    pass
            timer = threading.Timer(
                delay_ms / 1000.0, self._publish_now,
            )
            timer.daemon = True
            self._publish_timer = timer
        timer.start()

    def _publish_now(self) -> None:
        """Publish a STATUS_TICK event with the current composed line.
        NEVER raises — all failures swallowed at the conductor boundary."""
        try:
            content = self.compose()
        except Exception:  # noqa: BLE001 — defensive
            return
        try:
            from backend.core.ouroboros.governance.render_conductor import (
                ColorRole,
                EventKind,
                RegionKind,
                RenderEvent,
                get_render_conductor,
            )
        except Exception:  # noqa: BLE001 — defensive
            return
        conductor = get_render_conductor()
        if conductor is None:
            return
        try:
            event = RenderEvent(
                kind=EventKind.STATUS_TICK,
                region=RegionKind.STATUS,
                role=ColorRole.METADATA,
                content=content,
                source_module=self._source_module,
                metadata={"composed_status": True},
            )
            conductor.publish(event)
            self._last_publish_monotonic = time.monotonic()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[status_line_composer] publish failed", exc_info=True,
            )


# ---------------------------------------------------------------------------
# Singleton triplet — mirrors RenderConductor / InputController pattern
# ---------------------------------------------------------------------------


_DEFAULT_COMPOSER: Optional[StatusLineComposer] = None
_DEFAULT_LOCK = threading.Lock()


def get_status_line_composer() -> Optional[StatusLineComposer]:
    with _DEFAULT_LOCK:
        return _DEFAULT_COMPOSER


def register_status_line_composer(
    composer: Optional[StatusLineComposer],
) -> None:
    global _DEFAULT_COMPOSER
    with _DEFAULT_LOCK:
        _DEFAULT_COMPOSER = composer


def reset_status_line_composer() -> None:
    register_status_line_composer(None)


def update_field(field: Any, value: Any) -> None:
    """Producer-side helper: SerpentFlow's update_* methods call
    ``update_field(StatusField.COST, total)`` to feed the composer
    without holding a direct reference. Safe no-op when no composer
    is registered."""
    composer = get_status_line_composer()
    if composer is None:
        return
    composer.set(field, value)


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
            name=_FLAG_STATUS_LINE_COMPOSER_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master gate for the StatusLineComposer (D4 substrate). "
                "Graduated default true at D5 — composer aggregates "
                "ambient state into a single composed status line "
                "rendered in SerpentFlow's bottom_toolbar via "
                "STATUS_TICK. Replaces N scattered update_* console "
                "emits with one debounced line. Hot-revert via false."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/status_line_composer.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_STATUS_LINE_FIELDS,
            type=FlagType.JSON,
            default=None,
            description=(
                "Operator overlay on the composed-line field order. "
                "JSON list of StatusField values (POSTURE / COST / "
                "SENSORS / PROVIDER_CHAIN / INTENT_DISCOVERY / "
                "DREAM_ENGINE / LEARNING / SESSION_LESSONS / "
                "INTENT_CHAIN). Unknown fields silently skipped. "
                "Empty / missing → in-code default order."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/status_line_composer.py"
            ),
            example='["COST", "POSTURE", "SENSORS"]',
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_STATUS_LINE_DEBOUNCE_MS,
            type=FlagType.INT,
            default=50,
            description=(
                "Debounce window (milliseconds) — multiple set() "
                "calls within this window coalesce into one "
                "STATUS_TICK publish. Default 50ms. Min 0 (no "
                "debounce — fire on every set). Max 5000."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/status_line_composer.py"
            ),
            example="50",
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_STATUS_LINE_SEPARATOR,
            type=FlagType.STR,
            default=" | ",
            description=(
                "Separator between composed fields. Default ' | '. "
                "Operators may use ' • ' or ' / ' for stylistic "
                "preference. Empty string falls back to default."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/status_line_composer.py"
            ),
            example=" | ",
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
    "backend.core.ouroboros.battle_test.serpent_flow",
)


_EXPECTED_STATUS_FIELD = frozenset({
    "COST", "SENSORS", "PROVIDER_CHAIN", "INTENT_CHAIN", "POSTURE",
    "SESSION_LESSONS", "INTENT_DISCOVERY", "DREAM_ENGINE", "LEARNING",
    "ACTIVE_OP", "TASK_LIST",
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
    """Composer must NOT import serpent_flow at top level — feed via
    the StatusLineComposer.set() API; receive via STATUS_TICK
    conductor events. Bidirectional decoupling preserved."""
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_status_field_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_enum_member_names(tree, "StatusField"))
    if found != _EXPECTED_STATUS_FIELD:
        return (
            f"StatusField members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_STATUS_FIELD)}",
        )
    return ()


def _validate_field_formatters_present(
    tree: Any, source: str,
) -> tuple:
    """Every StatusField MUST have a corresponding entry in
    _FIELD_FORMATTERS. Catches a refactor that adds a field without
    a formatter (would render as empty string silently)."""
    del tree
    missing = []
    for field in _EXPECTED_STATUS_FIELD:
        # Look for `StatusField.{FIELD}: _format_` pattern in source
        if f"StatusField.{field}:" not in source:
            missing.append(field)
    if missing:
        return (
            f"_FIELD_FORMATTERS missing entries for: {missing}",
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


_TARGET_FILE = (
    "backend/core/ouroboros/governance/status_line_composer.py"
)


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="status_line_composer_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "status_line_composer.py MUST NOT import rich.* — "
                "the composer is a pure aggregator; rendering is "
                "downstream's concern via STATUS_TICK events."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="status_line_composer_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "status_line_composer.py MUST NOT import any authority "
                "module OR serpent_flow at top level. Composer feeds "
                "via set() API; SerpentFlow receives via STATUS_TICK "
                "conductor events. Bidirectional decoupling preserved."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="status_line_composer_status_field_closed",
            target_file=_TARGET_FILE,
            description=(
                "StatusField enum members must exactly match the "
                "documented 9-value closed set. Adding a field "
                "requires coordinated _FIELD_FORMATTERS + "
                "_DEFAULT_FIELD_ORDER + this pin update."
            ),
            validate=_validate_status_field_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="status_line_composer_field_formatters_present",
            target_file=_TARGET_FILE,
            description=(
                "Every StatusField member MUST have a corresponding "
                "entry in _FIELD_FORMATTERS. Without this pin, a "
                "field added to the enum without a formatter would "
                "silently render as empty string."
            ),
            validate=_validate_field_formatters_present,
        ),
        ShippedCodeInvariant(
            invariant_name="status_line_composer_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must "
                "be module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "STATUS_LINE_COMPOSER_SCHEMA_VERSION",
    "StatusField",
    "StatusLineComposer",
    "debounce_ms",
    "field_order",
    "get_status_line_composer",
    "is_enabled",
    "register_flags",
    "register_shipped_invariants",
    "register_status_line_composer",
    "reset_status_line_composer",
    "separator",
    "update_field",
]
