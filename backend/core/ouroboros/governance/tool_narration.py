"""Tool-call narration channel.

Bridges the synchronous ``ToolLoopCoordinator._on_tool_call`` callback
to the asynchronous :class:`CommProtocol` transport chain. Each invocation
builds a real :class:`CommMessage` stamped ``HEARTBEAT / phase=generate`` —
the shape :class:`SerpentTransport` already knows how to render.

Design
------
1.  **Sync-in, async-out.** ``emit`` is called from inside Venom's sync
    tool-dispatch hook. It wraps the async :meth:`CommProtocol._emit`
    in an :func:`asyncio.ensure_future` scheduled on the **running**
    loop (no deprecated :func:`asyncio.get_event_loop`).

2.  **Fault isolation.** Every failure path logs at DEBUG and returns.
    Narration *cannot* break a generation run, but silent ``except pass``
    is unacceptable — operators need to see *why* the CLI went quiet.

3.  **Config-driven kill switch.** ``JARVIS_TOOL_NARRATION_ENABLED=false``
    turns the channel into a no-op without touching call sites.
    ``JARVIS_TOOL_NARRATION_MAX_PREVIEW`` caps result-preview bytes so
    large tool outputs can't blow up the transport pipeline.

4.  **Lifecycle coverage.** Unlike the previous inline hack, this module
    emits for *every* status the tool loop produces:
    ``start / success / error / cancelled / denied / timeout``.
    SerpentFlow decides how each one renders — the channel is dumb.

5.  **Deterministic payload keys.** Callers never see payload shape
    changes because :data:`PAYLOAD_KEYS` documents the contract SerpentFlow
    reads from. Any new key goes there + serpent_flow.py in the same PR.

Manifesto compliance
--------------------
* §3 *Asynchronous tendrils* — sync callback never blocks the tool loop.
* §5 *Intelligence-driven routing* — same CommProtocol transport chain
  used by every other phase, no parallel observability path.
* §7 *Absolute observability* — every tool lifecycle event surfaces to
  the CLI, not just the happy path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants & types
# ---------------------------------------------------------------------------

#: Lifecycle statuses the tool loop may emit. ``""`` is an alias for
#: ``"start"`` — the pre-execution event. The channel normalises it so
#: transports see a non-empty status every time.
LIFECYCLE_STATUSES: FrozenSet[str] = frozenset({
    "start",     # pre-exec (tool about to run)
    "success",   # tool_result.error is None
    "error",     # tool_result.error set
    "timeout",   # ToolExecStatus.TIMEOUT
    "cancelled", # asyncio.CancelledError
    "denied",    # policy DENY
})

#: Keys SerpentFlow reads from the CommMessage payload. Duplicated here as
#: the authoritative contract between producer and consumer.
#:
#: ``preamble`` (new) carries the model's one-sentence WHY that SerpentFlow
#: renders above the spinner and the Karen voice channel speaks aloud. It
#: is only meaningful on ``status="start"`` events — post-exec emissions
#: leave it empty.
PAYLOAD_KEYS = (
    "phase",
    "tool_name",
    "tool_args_summary",
    "round_index",
    "result_preview",
    "duration_ms",
    "status",
    "tool_starting",
    "preamble",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def _env_int_min(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, val)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NarrationConfig:
    """Runtime configuration for the narration channel.

    Every tunable reads an env var with a safe default. The dataclass is
    frozen so a running channel can't have its config mutated out from
    under it — rebuild and reinstall if you need different settings.
    """

    enabled: bool = field(
        default_factory=lambda: _env_bool("JARVIS_TOOL_NARRATION_ENABLED", True)
    )
    max_preview_chars: int = field(
        default_factory=lambda: _env_int_min(
            "JARVIS_TOOL_NARRATION_MAX_PREVIEW", 500, minimum=0,
        )
    )
    max_args_chars: int = field(
        default_factory=lambda: _env_int_min(
            "JARVIS_TOOL_NARRATION_MAX_ARGS", 80, minimum=0,
        )
    )
    #: Hard cap on the preamble narration string reaching the TUI. Set to 0
    #: to disable truncation. Defaults to 160 — same as the parser cap, so
    #: the TUI and Karen voice see identical strings.
    max_preamble_chars: int = field(
        default_factory=lambda: _env_int_min(
            "JARVIS_TOOL_NARRATION_MAX_PREAMBLE", 160, minimum=0,
        )
    )
    #: When True, failures in the emit path log a warning. False keeps
    #: them at DEBUG (the default — noise-free).
    warn_on_failure: bool = field(
        default_factory=lambda: _env_bool("JARVIS_TOOL_NARRATION_WARN", False)
    )


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class ToolNarrationChannel:
    """Sync-to-async narration bridge for tool-call lifecycle events.

    Parameters
    ----------
    comm:
        A :class:`CommProtocol` instance (or anything with a ``_transports``
        list and an ``async _emit(msg)`` coroutine). The channel calls
        ``_emit`` so messages receive idempotency keys, global seqs, and
        correlation IDs — matching every other phase's observability.
    config:
        Optional :class:`NarrationConfig`. Defaults to env-var-driven values.
    seq_start:
        Initial monotonic counter for the channel's own local sequence
        numbers (used only when the CommProtocol is absent, e.g. tests).
    """

    def __init__(
        self,
        comm: Optional[Any],
        config: Optional[NarrationConfig] = None,
        *,
        seq_start: int = 0,
    ) -> None:
        self._comm = comm
        self._config = config or NarrationConfig()
        self._local_seq = seq_start
        # Count failures so the operator has *something* to grep even when
        # warn_on_failure is off. Exposed via ``failure_count`` property.
        self._failures: int = 0
        self._emits: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def config(self) -> NarrationConfig:
        return self._config

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def emit_count(self) -> int:
        return self._emits

    def emit(
        self,
        *,
        op_id: str,
        tool_name: str,
        round_index: int,
        args_summary: str = "",
        result_preview: str = "",
        duration_ms: float = 0.0,
        status: str = "",
        preamble: str = "",
    ) -> None:
        """Emit a narration event. Safe to call from any sync context.

        ``status=""`` is normalised to ``"start"`` (the pre-execution
        event). Unknown statuses are accepted verbatim — the channel
        never rejects input; SerpentFlow is the visual arbiter.

        ``preamble`` is the model's one-sentence WHY for the tool round.
        Only meaningful on ``start`` — the channel drops it on every
        other status so post-exec events don't re-render or re-speak it.
        """
        if not self._config.enabled:
            return
        if not tool_name:
            return

        # Normalise status + truncate free-text fields.
        norm_status = status.strip() or "start"
        if self._config.max_args_chars and args_summary:
            args_summary = args_summary[: self._config.max_args_chars]
        if self._config.max_preview_chars and result_preview:
            result_preview = result_preview[: self._config.max_preview_chars]

        # Preamble is only meaningful pre-exec. Drop it on success/error/
        # etc. so SerpentFlow doesn't re-render it after the round ends.
        if norm_status != "start":
            preamble = ""
        elif preamble:
            # Collapse whitespace (parser already does this, but defence
            # in depth — a direct caller may pass raw text).
            preamble = " ".join(preamble.split())
            cap = self._config.max_preamble_chars
            if cap and len(preamble) > cap:
                preamble = preamble[:cap].rstrip() + "…"

        payload = self._build_payload(
            tool_name=tool_name,
            args_summary=args_summary,
            round_index=round_index,
            result_preview=result_preview,
            duration_ms=duration_ms,
            status=norm_status,
            preamble=preamble,
        )

        msg = self._build_message(op_id=op_id, payload=payload)

        self._schedule_delivery(msg)
        self._emits += 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        *,
        tool_name: str,
        args_summary: str,
        round_index: int,
        result_preview: str,
        duration_ms: float,
        status: str,
        preamble: str = "",
    ) -> Dict[str, Any]:
        return {
            "phase": "generate",
            "tool_name": tool_name,
            "tool_args_summary": args_summary,
            "round_index": round_index,
            "result_preview": result_preview,
            "duration_ms": duration_ms,
            "status": status,
            # Back-compat key — SerpentTransport reads this to decide
            # between "start spinner" and "stop spinner + print artifact".
            "tool_starting": status == "start",
            # One-sentence WHY spoken by Ouroboros / rendered above spinner.
            # Always present in the payload (empty string on non-start
            # events) so consumers can use a single dict-get access path.
            "preamble": preamble,
        }

    def _build_message(
        self,
        *,
        op_id: str,
        payload: Dict[str, Any],
    ) -> Any:
        """Build a real CommMessage if available, else a duck-typed stand-in.

        The import is lazy so this module has zero hard dependencies on
        the rest of the governance tree — swap the comm backend and the
        channel still compiles.
        """
        try:
            from backend.core.ouroboros.governance.comm_protocol import (
                CommMessage,
                MessageType,
            )

            self._local_seq += 1
            return CommMessage(
                msg_type=MessageType.HEARTBEAT,
                op_id=op_id,
                seq=self._local_seq,
                causal_parent_seq=None,
                payload=payload,
                timestamp=time.time(),
            )
        except Exception:
            # Duck-typed fallback — SerpentTransport only needs .payload,
            # .op_id, and .msg_type.value to route the message.
            return _DuckMessage(
                op_id=op_id,
                payload=payload,
            )

    def _schedule_delivery(self, msg: Any) -> None:
        """Hand the message to the transport chain without blocking.

        The three cases we need to handle:
        1. We're inside a running loop (the common case — tool loop is
           async). Schedule a task with ``asyncio.ensure_future``.
        2. No running loop (test harness, CLI tool). Log at DEBUG and
           drop the message — CommProtocol isn't reachable anyway.
        3. Comm is None (headless / tests). Same as case 2.
        """
        if self._comm is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Not inside an event loop — drop the message. This is
            # expected in unit tests that drive the channel directly.
            return

        try:
            coro = self._deliver(msg)
            loop.create_task(coro)
        except Exception as exc:
            self._record_failure("schedule", exc, msg)

    async def _deliver(self, msg: Any) -> None:
        """Forward the message through CommProtocol._emit (if present).

        Falls back to direct transport.send() when ``_emit`` is absent
        (duck-typed comm objects in tests).
        """
        try:
            _emit = getattr(self._comm, "_emit", None)
            if _emit is not None:
                await _emit(msg)
                return
            transports = getattr(self._comm, "_transports", None) or []
            for t in transports:
                try:
                    send = getattr(t, "send", None)
                    if send is None:
                        continue
                    result = send(msg)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    self._record_failure("transport", exc, msg)
        except Exception as exc:
            self._record_failure("deliver", exc, msg)

    def _record_failure(self, stage: str, exc: BaseException, msg: Any) -> None:
        self._failures += 1
        log = logger.warning if self._config.warn_on_failure else logger.debug
        _op = getattr(msg, "op_id", "?") or "?"
        try:
            _tool = (msg.payload or {}).get("tool_name", "?") if hasattr(msg, "payload") else "?"
        except Exception:
            _tool = "?"
        log(
            "[ToolNarration] %s failure op=%s tool=%s: %s",
            stage, str(_op)[:12], _tool, exc,
        )


# ---------------------------------------------------------------------------
# Duck-typed fallback message
# ---------------------------------------------------------------------------


@dataclass
class _DuckMessage:
    """Minimal CommMessage stand-in when comm_protocol is unavailable.

    Matches the three attributes SerpentTransport.send() reads:
    ``.payload``, ``.op_id``, ``.msg_type.value``. Anything else is
    dropped silently by the transport.
    """

    op_id: str
    payload: Dict[str, Any]

    @property
    def msg_type(self) -> "_DuckMsgType":
        return _DuckMsgType()


class _DuckMsgType:
    value = "HEARTBEAT"


# ---------------------------------------------------------------------------
# Helpers for callers
# ---------------------------------------------------------------------------


def build_args_summary(
    arguments: Optional[Dict[str, Any]],
    *,
    max_chars: int = 80,
) -> str:
    """Build a short preview of a tool call's args.

    Uses the *first* value in the arguments dict. Works for every built-in
    Venom tool (path / query / cmd / pattern all live at position 0) and
    degrades gracefully on empty / None.
    """
    if not arguments:
        return ""
    try:
        first = next(iter(arguments.values()), "")
    except Exception:
        return ""
    if first is None:
        return ""
    text = str(first)
    return text[:max_chars] if max_chars else text
