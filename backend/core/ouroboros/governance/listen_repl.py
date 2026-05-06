"""§37 Slice 2 — `/listen` REPL surface composing
:mod:`ide_observability_stream.StreamEventBroker`.

Closes Tier 1 #5 from the §37 UX roadmap: surfaces the live SSE
event stream that until now was only visible to IDE/SSE consumers
(VS Code / Cursor / Sublime / JetBrains extensions). The 57 event
types (plus `stream_lag` chatter-suppression marker) are the
canonical operator-facing observability spine — every subsystem
publishes to this single broker via ``get_default_broker()``.

Per the operator binding "fully leverage the existing files and
architecture within the codebase so we avoid duplication and
build cleanly on what already exists":

  * The broker is the canonical surface — composing it directly
    means ``/listen`` sees EVERY event the IDE extensions see, in
    the same chronological order, with the same shape.
  * No parallel queue. No second tap. Just the existing bounded
    history ring (default 512 entries) read via the new public
    :meth:`StreamEventBroker.recent_history` helper.

Architectural locks (operator binding 2026-05-05):

  * **Single pipeline** — read state via the canonical
    `get_default_broker()` singleton ONLY. Forbidden to
    construct a new ``StreamEventBroker`` here. AST-pinned.
  * **Authority asymmetry / read-only** — REPL NEVER calls
    ``publish()`` / ``publish_op_started()`` / etc. on the
    broker. The dashboard observes; producers (orchestrator,
    sensors, SSE bridges) write.
  * **Auto-discovered** — file ends `_repl.py` per §32.11
    Slice 4 naming-cage; verb name `listen` derived from
    basename; ``dispatch_listen_command(line)`` matches.
  * **Master-flag honest UX** — empty history renders an
    honest "no events yet" rather than fabricating activity.
  * **NEVER raises** — pure-function dispatch.

Subcommands:

  * ``/listen`` (bare)             — most recent N events
                                     (default 20)
  * ``/listen recent [N]``         — last N events (max 200)
  * ``/listen types``              — list distinct event_types
                                     in history
  * ``/listen ops``                — list distinct op_ids
                                     in history
  * ``/listen filter type=X [N]``  — events matching event_type
  * ``/listen filter op=Y [N]``    — events for one op_id
  * ``/listen show <event_id>``    — full payload for one event
  * ``/listen stats``              — broker stats (subscribers /
                                     history size / published /
                                     dropped)
  * ``/listen help``               — bypass-master help

Live-tail mode is deliberately deferred — would require an async
tail loop coexisting with the REPL stdin loop. Snapshot mode
covers 90% of the operator-debug use case; live tail is a
follow-up if needed.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import List, Optional


# ---------------------------------------------------------------------------
# ANSI palette — identity-consistent (chrome=dim / outcomes=green / errors=red)
# ---------------------------------------------------------------------------


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


# ---------------------------------------------------------------------------
# Frozen result envelope (mirrors decisions_repl + health_repl shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListenReplDispatchResult:
    """Result of a ``/listen`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/listen`` invocation."""

    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/listen — observability event stream{_RESET}\n"
    f"  {_DIM}Read-only operator view of the canonical SSE broker "
    f"history (57 event types).{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/listen{_RESET}                    "
    f"{_DIM}most recent 20 events{_RESET}\n"
    f"    {_CYAN}/listen recent [N]{_RESET}         "
    f"{_DIM}last N events (default 20, max 200){_RESET}\n"
    f"    {_CYAN}/listen types{_RESET}              "
    f"{_DIM}distinct event_types in history{_RESET}\n"
    f"    {_CYAN}/listen ops{_RESET}                "
    f"{_DIM}distinct op_ids in history{_RESET}\n"
    f"    {_CYAN}/listen filter type=X [N]{_RESET}  "
    f"{_DIM}events matching event_type{_RESET}\n"
    f"    {_CYAN}/listen filter op=Y [N]{_RESET}    "
    f"{_DIM}events for one op_id{_RESET}\n"
    f"    {_CYAN}/listen show <event_id>{_RESET}    "
    f"{_DIM}full payload for one event{_RESET}\n"
    f"    {_CYAN}/listen stats{_RESET}              "
    f"{_DIM}broker stats{_RESET}\n"
    f"    {_CYAN}/listen help{_RESET}               "
    f"{_DIM}this message{_RESET}\n"
)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/listen"
        or s == "listen"
        or s.startswith("/listen ")
        or s.startswith("listen ")
    )


def _color_for_event_type(event_type: str) -> str:
    """Identity-consistent event-type coloring. NOTE: we do NOT
    use bright_green here — that's reserved for outcome rendering
    (op success / `✨` evolved), per §37.9 invariant #3 + Slice 4
    lint pin."""
    if "completed" in event_type or "success" in event_type:
        return _GREEN
    if (
        "error" in event_type
        or "rolled_back" in event_type
        or "failed" in event_type
    ):
        return _RED
    if "lag" in event_type or "warn" in event_type:
        return _YELLOW
    return _CYAN


def _format_event_line(ev) -> str:
    """One-line event summary."""
    type_color = _color_for_event_type(ev.event_type)
    op_marker = (
        f"{_DIM}op={ev.op_id[:12]}{_RESET}"
        if ev.op_id else f"{_DIM}op=-{_RESET}"
    )
    return (
        f"  {_DIM}{ev.timestamp[11:23]}{_RESET}  "
        f"{type_color}{ev.event_type}{_RESET}  "
        f"{op_marker}  "
        f"{_DIM}id={ev.event_id[:8]}{_RESET}"
    )


def _format_event_detail(ev) -> str:
    """Multi-line full event detail for /listen show."""
    type_color = _color_for_event_type(ev.event_type)
    out = [
        f"\n  {_BOLD}{type_color}{ev.event_type}{_RESET}",
        f"  {_DIM}event_id:   {ev.event_id}{_RESET}",
        f"  {_DIM}timestamp:  {ev.timestamp}{_RESET}",
        f"  {_DIM}op_id:      {ev.op_id or '(none)'}{_RESET}",
        f"  {_DIM}schema:     {ev.schema_version}{_RESET}",
        "",
        f"  {_BOLD}Payload:{_RESET}",
    ]
    try:
        payload_json = json.dumps(
            dict(ev.payload), indent=2, ensure_ascii=False, default=str,
        )
        for line in payload_json.split("\n"):
            out.append(f"    {_DIM}{line}{_RESET}")
    except Exception:  # noqa: BLE001 — defensive
        out.append(f"    {_RED}(payload unrenderable){_RESET}")
    return "\n".join(out) + "\n"


def _parse_limit(args: List[str]) -> int:
    """Parse optional integer limit arg with sane clamping."""
    if not args:
        return _DEFAULT_LIMIT
    try:
        n = int(args[0])
    except (ValueError, TypeError):
        return _DEFAULT_LIMIT
    if n < 1:
        return 1
    if n > _MAX_LIMIT:
        return _MAX_LIMIT
    return n


def _parse_filter_kv(args: List[str]) -> Optional[tuple]:
    """Parse `key=value` filter arg. Returns (key, value) or
    None on parse error."""
    if not args:
        return None
    raw = args[0]
    if "=" not in raw:
        return None
    key, _, value = raw.partition("=")
    key = key.strip().lower()
    value = value.strip()
    if not key or not value:
        return None
    if key not in ("type", "op"):
        return None
    return (key, value)


# ---------------------------------------------------------------------------
# Renderers — read via canonical singleton ONLY (single-pipeline guardrail)
# ---------------------------------------------------------------------------


def _render_recent(limit: int) -> str:
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    broker = get_default_broker()
    events = broker.recent_history(limit=limit)
    if not events:
        return (
            f"\n  {_BOLD}{_CYAN}Event Stream{_RESET}\n"
            f"  {_DIM}No events in history yet — broker is "
            f"running but no producers have published. Subsystems "
            f"will populate this surface as ops fire.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Event Stream{_RESET}  "
        f"{_DIM}(showing {len(events)} most recent of "
        f"{broker.history_size} in history){_RESET}",
        "",
    ]
    for ev in events:
        out.append(_format_event_line(ev))
    out.append("")
    out.append(
        f"  {_DIM}Use /listen show <event_id> for full payload "
        f"detail. Use /listen filter type=X for narrower "
        f"streams.{_RESET}",
    )
    return "\n".join(out) + "\n"


def _render_types() -> str:
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    broker = get_default_broker()
    types = broker.distinct_event_types()
    if not types:
        return (
            f"\n  {_DIM}No events in history yet.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Distinct event types{_RESET}  "
        f"{_DIM}({len(types)}){_RESET}",
        "",
    ]
    for t in types:
        out.append(
            f"  {_color_for_event_type(t)}{t}{_RESET}",
        )
    return "\n".join(out) + "\n"


def _render_ops() -> str:
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    op_ids = get_default_broker().distinct_op_ids(limit=50)
    if not op_ids:
        return (
            f"\n  {_DIM}No op_ids in history yet.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Distinct op_ids{_RESET}  "
        f"{_DIM}({len(op_ids)} most-recent-first){_RESET}",
        "",
    ]
    for op_id in op_ids:
        out.append(f"  {_DIM}{op_id}{_RESET}")
    return "\n".join(out) + "\n"


def _render_filter(filter_kv: tuple, limit: int) -> str:
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    key, value = filter_kv
    broker = get_default_broker()
    if key == "type":
        events = broker.recent_history(
            limit=limit, event_type=value,
        )
    else:  # key == "op"
        events = broker.recent_history(limit=limit, op_id=value)
    if not events:
        return (
            f"\n  {_DIM}No events match {key}={value!r} in "
            f"current history.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Filtered Events{_RESET}  "
        f"{_DIM}({key}={value}, {len(events)} matches){_RESET}",
        "",
    ]
    for ev in events:
        out.append(_format_event_line(ev))
    return "\n".join(out) + "\n"


def _render_show(event_id_prefix: str) -> str:
    """Find an event by event_id prefix (8-char prefix matches
    the rendered short form). NEVER raises."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    broker = get_default_broker()
    # Read the full history snapshot. recent_history() with
    # broker._history_maxlen returns everything.
    events = broker.recent_history(limit=_MAX_LIMIT)
    matches = [
        ev for ev in events
        if ev.event_id.startswith(event_id_prefix)
    ]
    if not matches:
        return (
            f"\n  {_RED}No event matches event_id prefix "
            f"{event_id_prefix!r}.{_RESET}\n"
            f"  {_DIM}Use /listen recent to see available "
            f"event_ids.{_RESET}\n"
        )
    if len(matches) > 1:
        out = [
            f"\n  {_YELLOW}Multiple events match prefix "
            f"{event_id_prefix!r}. Showing the most recent.{_RESET}",
        ]
        out.append(_format_event_detail(matches[-1]))
        return "\n".join(out)
    return _format_event_detail(matches[0])


def _render_stats() -> str:
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    broker = get_default_broker()
    out = [
        f"\n  {_BOLD}{_CYAN}Broker Stats{_RESET}",
        "",
        f"  {_DIM}history_size:    {_RESET}{broker.history_size}",
        f"  {_DIM}subscribers:     {_RESET}{broker.subscriber_count}",
        f"  {_DIM}published_count: {_RESET}{broker.published_count}",
        f"  {_DIM}dropped_count:   {_RESET}"
        f"{_RED if broker.dropped_count > 0 else _DIM}"
        f"{broker.dropped_count}{_RESET}",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher (auto-mounted via repl_dispatch_registry)
# ---------------------------------------------------------------------------


def dispatch_listen_command(
    line: str,
) -> ListenReplDispatchResult:
    """Parse a ``/listen`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return ListenReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ListenReplDispatchResult(
            ok=False,
            text=f"  /listen parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return ListenReplDispatchResult(ok=True, text=_HELP)

    try:
        if head == "":
            return ListenReplDispatchResult(
                ok=True, text=_render_recent(_DEFAULT_LIMIT),
            )
        if head == "recent":
            limit = _parse_limit(args[1:])
            return ListenReplDispatchResult(
                ok=True, text=_render_recent(limit),
            )
        if head == "types":
            return ListenReplDispatchResult(
                ok=True, text=_render_types(),
            )
        if head == "ops":
            return ListenReplDispatchResult(
                ok=True, text=_render_ops(),
            )
        if head == "stats":
            return ListenReplDispatchResult(
                ok=True, text=_render_stats(),
            )
        if head == "filter":
            filter_kv = _parse_filter_kv(args[1:])
            if filter_kv is None:
                return ListenReplDispatchResult(
                    ok=False,
                    text=(
                        "  /listen filter <key>=<value> [N] — "
                        "key must be 'type' or 'op', value "
                        "non-empty. e.g.: /listen filter "
                        "type=op_completed 10"
                    ),
                )
            limit = _parse_limit(args[2:])
            return ListenReplDispatchResult(
                ok=True,
                text=_render_filter(filter_kv, limit),
            )
        if head == "show":
            if len(args) < 2:
                return ListenReplDispatchResult(
                    ok=False,
                    text=(
                        "  /listen show <event_id> — event_id "
                        "(or prefix) required"
                    ),
                )
            return ListenReplDispatchResult(
                ok=True, text=_render_show(args[1]),
            )
        return ListenReplDispatchResult(
            ok=False,
            text=(
                f"  /listen: unknown subcommand "
                f"{head!r} — try /listen help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ListenReplDispatchResult(
            ok=False,
            text=(
                f"  /listen: error reading broker — {exc}. "
                f"Try again after subsystems boot."
            ),
        )


# ---------------------------------------------------------------------------
# /help auto-discovery hook
# ---------------------------------------------------------------------------


def register_verbs(registry) -> int:
    """Auto-discovered by `help_dispatcher`. Registers the
    `/listen` verb in the operator-facing /help index."""
    try:
        registry.register(
            verb="listen",
            description=(
                "Observability event stream tail — read-only "
                "snapshot of the canonical SSE broker history "
                "(57 event types). Filter by event_type or "
                "op_id; show full payload by event_id."
            ),
            posture_relevance="RELEVANT",
            since="§37 Slice 2 (PRD §36.5, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins (auto-discovered via shipped_code_invariants)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``listen_repl_composes_canonical_broker`` — module
         reads via `get_default_broker()` ONLY; never
         constructs `StreamEventBroker()` directly (would
         create stale parallel surface).
      2. ``listen_repl_authority_read_only`` — module NEVER
         calls `publish()` / `publish_op_*()` on the broker.
         Read-only operator surface.
      3. ``listen_repl_authority_asymmetry`` — substrate purity
         (no orchestrator / iron_gate / providers imports).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/listen_repl.py"
    )

    def _validate_composes_canonical_broker(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "StreamEventBroker"
                ):
                    violations.append(
                        "listen_repl.py MUST NOT construct "
                        "StreamEventBroker() directly — "
                        "compose get_default_broker() (single-"
                        "pipeline guardrail)"
                    )
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "StreamEventBroker"
                ):
                    violations.append(
                        "listen_repl.py MUST NOT construct "
                        "StreamEventBroker() via attribute "
                        "access"
                    )
        return tuple(violations)

    def _validate_authority_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT call mutating broker methods."""
        violations: list = []
        forbidden_methods = (
            "publish",
            "publish_op_started",
            "publish_op_completed",
            "publish_op_failed",
            "publish_phase_changed",
            "publish_review_branch_created",
            "publish_review_branch_accepted",
            "publish_review_branch_rejected",
            "publish_review_branch_expired",
            "publish_governor_throttle_applied",
            "publish_memory_pressure_changed",
            "publish_posture_changed",
            "publish_curiosity_event",
            "publish_m10_proposal_event",
            "publish_semantic_budget_event",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in forbidden_methods:
                    continue
                # Heuristic: receiver is "broker" or ends in
                # "_broker" → broker handle.
                receiver = func.value
                if (
                    isinstance(receiver, ast.Name)
                    and (
                        receiver.id == "broker"
                        or receiver.id.endswith("_broker")
                    )
                ):
                    violations.append(
                        f"listen_repl.py MUST NOT call "
                        f"broker.{func.attr}(...) — read-only "
                        f"operator surface"
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
                            f"listen_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "listen_repl_composes_canonical_broker"
            ),
            target_file=target,
            description=(
                "§37 Slice 2 — single-pipeline guardrail: "
                "module composes get_default_broker() "
                "singleton; never constructs "
                "StreamEventBroker directly."
            ),
            validate=_validate_composes_canonical_broker,
        ),
        ShippedCodeInvariant(
            invariant_name="listen_repl_authority_read_only",
            target_file=target,
            description=(
                "§37 Slice 2 — read-only operator surface: "
                "module MUST NOT call any broker.publish* "
                "method. Producers write; dashboard observes."
            ),
            validate=_validate_authority_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name="listen_repl_authority_asymmetry",
            target_file=target,
            description=(
                "§37 Slice 2 — substrate purity: no "
                "orchestrator / iron_gate / policy / providers "
                "/ candidate_generator imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "ListenReplDispatchResult",
    "dispatch_listen_command",
    "register_shipped_invariants",
    "register_verbs",
]
