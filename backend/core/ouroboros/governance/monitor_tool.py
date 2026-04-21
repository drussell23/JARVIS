"""Venom monitor tool — policy-gated, read-only observer over a subprocess.

Slice 2 of Ticket #4. Wraps the :mod:`background_monitor` primitive
with a Venom-surface handler that:

  * Is **deny-by-default** at the policy layer
    (``JARVIS_TOOL_MONITOR_ENABLED`` defaults ``false``)
  * Restricts spawned binaries to an operator-curated allowlist
    (``JARVIS_TOOL_MONITOR_ALLOWED_BINARIES``)
  * Caps per-invocation wall-clock time at an env ceiling
    (``JARVIS_TOOL_MONITOR_TIMEOUT_S``)
  * Caps retained events at a ring-buffer ceiling
    (``JARVIS_TOOL_MONITOR_MAX_EVENTS``)
  * Supports optional early-exit on a regex pattern match against
    stdout/stderr lines — the use case that motivated the slice
    (streaming-pytest: stop on "FAILED" without waiting for the whole
    suite)

Authority posture:

  * **Read-only capability set** — manifest declares ``{"subprocess"}``
    NOT ``{"subprocess", "write"}``. Under ``is_read_only`` ops, the
    scope gate still blocks mutation tools; ``monitor`` remains
    allowed because it neither edits files nor runs a shell.
  * **Argv-only spawn** — delegates to :class:`BackgroundMonitor` which
    uses ``asyncio.create_subprocess_exec`` (execve-family, no shell).
    The tool adds a binary-allowlist gate on top so a permitted argv
    isn't enough; the ``cmd[0]`` basename must match the policy
    allowlist.
  * **No new mutation surface** — this tool observes an already-
    authorized binary; it does NOT grant the model a generic
    "run anything" escape hatch.

The handler is deliberately thin — policy gating + allowlist
enforcement + timeout capping + structured JSON result — and the
heavy lifting (stream decoding, ring buffer, exit reaping) lives in
the primitive. Testable in isolation by passing a
``ToolCall``/``PolicyContext`` pair; no Venom-executor fixture
required.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from backend.core.ouroboros.governance.background_monitor import (
    BackgroundMonitor,
    KIND_EXITED,
    KIND_STDERR,
    KIND_STDOUT,
    MonitorEvent,
)

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyContext,
        ToolCall,
        ToolResult,
    )


logger = logging.getLogger(__name__)


# --- Env knobs (all deny-by-default or conservative defaults) ---------------

_DEFAULT_ALLOWED_BINARIES_CSV = (
    "pytest,python,python3,node,npm,go,cargo,make,ruff,mypy,pyright"
)
_DEFAULT_TIMEOUT_S = 60.0
_DEFAULT_MAX_EVENTS = 500
_DEFAULT_TERMINATE_GRACE_S = 1.0


def monitor_enabled() -> bool:
    """Master switch. **Default false** — tool is deny-by-default.

    Flip to ``"true"`` to make the policy engine start allowing
    ``monitor`` tool calls. Even when flipped, per-call binary
    allowlist + timeout cap still apply.
    """
    return os.environ.get(
        "JARVIS_TOOL_MONITOR_ENABLED", "false",
    ).strip().lower() == "true"


def monitor_allowed_binaries() -> frozenset:
    """Return the active allowlist of argv[0] basenames.

    Default list covers common test runners + build tools
    (pytest/python/node/npm/go/cargo/make/ruff/mypy/pyright). Override
    via ``JARVIS_TOOL_MONITOR_ALLOWED_BINARIES`` (comma-separated).
    Empty entries are ignored; whitespace is stripped. An empty
    override → empty allowlist → every binary is denied (useful for
    tests that want to prove the deny-path fires without disabling
    the master switch).
    """
    raw = os.environ.get(
        "JARVIS_TOOL_MONITOR_ALLOWED_BINARIES",
        _DEFAULT_ALLOWED_BINARIES_CSV,
    )
    items = {tok.strip() for tok in raw.split(",") if tok.strip()}
    return frozenset(items)


def monitor_timeout_ceiling() -> float:
    raw = os.environ.get(
        "JARVIS_TOOL_MONITOR_TIMEOUT_S", str(_DEFAULT_TIMEOUT_S),
    )
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def monitor_max_events() -> int:
    raw = os.environ.get(
        "JARVIS_TOOL_MONITOR_MAX_EVENTS", str(_DEFAULT_MAX_EVENTS),
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_EVENTS


def classify_cmd(cmd: Any) -> Optional[str]:
    """Structural validator for the ``cmd`` argument.

    Returns an error-reason string on rejection, or ``None`` when the
    argument shape is valid. Shared between the policy layer (decides
    whether to ALLOW/DENY at dispatch) and the handler (defense in
    depth — re-checks even after the policy already approved).
    """
    if not isinstance(cmd, list) or not cmd:
        return "cmd must be a non-empty list of strings"
    for item in cmd:
        if not isinstance(item, str) or not item:
            return "cmd elements must be non-empty strings"
    return None


def extract_binary_basename(cmd: List[str]) -> str:
    """Return the basename of ``cmd[0]`` — the entry that the
    allowlist gates on. Strips directories so
    ``/usr/local/bin/pytest`` and ``pytest`` gate identically."""
    if not cmd:
        return ""
    return os.path.basename(cmd[0])


# --- Handler ---------------------------------------------------------------


async def run_monitor_tool(
    call: "ToolCall",
    policy_ctx: "PolicyContext",
    timeout: float,
    cap: int,
) -> "ToolResult":
    """Execute a monitor tool call. Returns a ``ToolResult``.

    The handler assumes policy has already validated the call (env
    enabled, cmd well-formed, binary in allowlist). It still
    defense-in-depth-validates the args so that direct-call tests
    (bypassing the policy) can't crash it. Handler failures always
    return a ToolResult — never raise.

    Output shape (JSON-serialized in ``ToolResult.output``):

        {
          "exit_code": int | null,
          "duration_s": float,
          "event_count": int,
          "events": [                 # capped at MAX_EVENTS
            {"kind": "stdout",
             "data": "...",
             "ts_mono": 12345.6,
             "sequence": 1},
            ...
          ],
          "early_exit": bool,         # true iff pattern matched
          "early_exit_match": str,    # matched line (empty if early_exit=false)
          "timed_out": bool,          # true iff timeout wall-clock elapsed
          "truncated": bool           # true iff ring buffer evicted events
        }
    """
    from backend.core.ouroboros.governance.tool_executor import (
        ToolExecStatus,
        ToolResult,
    )

    args = call.arguments or {}
    cmd = args.get("cmd")
    pattern_raw = args.get("pattern", "")
    requested_timeout = args.get("timeout_s", _DEFAULT_TIMEOUT_S)

    # Defense-in-depth arg validation (policy should have caught these).
    err = classify_cmd(cmd)
    if err is not None:
        return ToolResult(
            tool_call=call, output="", error=err,
            status=ToolExecStatus.EXEC_ERROR,
        )

    # Compile the optional early-exit regex. Malformed regex → clean
    # error, not a crash.
    compiled_pattern: Optional[re.Pattern] = None
    if isinstance(pattern_raw, str) and pattern_raw:
        try:
            compiled_pattern = re.compile(pattern_raw)
        except re.error as exc:
            return ToolResult(
                tool_call=call, output="",
                error=f"invalid pattern: {exc}",
                status=ToolExecStatus.EXEC_ERROR,
            )

    # Resolve the effective timeout: min(model-requested, env cap, Venom
    # remaining-deadline). Never silently exceed any of the three.
    try:
        requested = float(requested_timeout)
    except (TypeError, ValueError):
        requested = _DEFAULT_TIMEOUT_S
    effective_timeout = max(
        1.0,
        min(requested, monitor_timeout_ceiling(), max(1.0, timeout)),
    )

    max_events = monitor_max_events()
    kept_events: List[Dict[str, Any]] = []
    early_exit = False
    early_exit_match = ""
    timed_out = False
    t0 = time.monotonic()

    # The monitor. Deliberately pass event_bus=None — Slice 2 does not
    # couple to the bus. Slice 3+ can wire through policy_ctx if/when
    # the bus ref is available on PolicyContext.
    try:
        async with BackgroundMonitor(
            cmd=list(cmd),  # type: ignore[arg-type]  (validated above)
            op_id=policy_ctx.op_id,
            ring_capacity=max_events,
            terminate_grace_s=_DEFAULT_TERMINATE_GRACE_S,
            event_bus=None,
        ) as mon:
            try:
                async def _drive() -> None:
                    nonlocal early_exit, early_exit_match
                    async for ev in mon.events():
                        # Record a compact serializable form up to cap.
                        if len(kept_events) < max_events:
                            kept_events.append({
                                "kind": ev.kind,
                                "data": ev.data,
                                "ts_mono": ev.ts_mono,
                                "sequence": ev.sequence,
                                "exit_code": ev.exit_code,
                            })
                        # Early-exit only on stream events (not EXITED).
                        if (
                            compiled_pattern is not None
                            and ev.kind in (KIND_STDOUT, KIND_STDERR)
                            and compiled_pattern.search(ev.data)
                        ):
                            early_exit = True
                            early_exit_match = ev.data
                            return

                await asyncio.wait_for(_drive(), timeout=effective_timeout)
            except asyncio.TimeoutError:
                timed_out = True
            # Ring snapshot reflects the final state (may exceed the
            # kept_events array if events arrived after our cap).
            snap_len = len(mon.ring_snapshot())
            truncated = snap_len >= max_events
            exit_code = mon.exit_code
    except FileNotFoundError:
        # Binary disappeared between policy approval and spawn (race,
        # mid-invocation cleanup, etc.). Surface cleanly.
        return ToolResult(
            tool_call=call, output="",
            error=(
                f"monitor: binary not found: {extract_binary_basename(cmd)!r}"
            ),
            status=ToolExecStatus.EXEC_ERROR,
        )
    except PermissionError as exc:
        return ToolResult(
            tool_call=call, output="",
            error=f"monitor: permission denied: {exc}",
            status=ToolExecStatus.EXEC_ERROR,
        )
    except Exception as exc:  # noqa: BLE001 — tool boundary, must never raise
        logger.debug(
            "[MonitorTool] unexpected exception op=%s cmd=%s",
            policy_ctx.op_id, cmd, exc_info=True,
        )
        return ToolResult(
            tool_call=call, output="",
            error=f"monitor: {type(exc).__name__}: {str(exc)[:200]}",
            status=ToolExecStatus.EXEC_ERROR,
        )

    duration_s = time.monotonic() - t0
    payload: Dict[str, Any] = {
        "exit_code": exit_code,
        "duration_s": round(duration_s, 3),
        "event_count": len(kept_events),
        "events": kept_events,
        "early_exit": early_exit,
        "early_exit_match": early_exit_match,
        "timed_out": timed_out,
        "truncated": truncated,
    }
    output = json.dumps(payload, ensure_ascii=False)
    if len(output) > cap:
        # Drop the events array but keep the header so the model can
        # still reason about exit_code / duration / early_exit.
        truncated_payload = dict(payload)
        truncated_payload["events"] = []
        truncated_payload["truncated"] = True
        output = json.dumps(truncated_payload, ensure_ascii=False)
    return ToolResult(
        tool_call=call, output=output, error=None,
        status=ToolExecStatus.SUCCESS,
    )
