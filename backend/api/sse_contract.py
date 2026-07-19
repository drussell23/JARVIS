"""JARVISKit SSE serialization contract (Phase 10).

The native Swift ``SSEClient.parseBlock`` is a STRICT, non-spec-compliant
SSE parser, and the local ``EventStream`` emits a different dialect. This
module is the single enforcement point that translates the local frame
envelope into the exact bytes ``JARVISKit`` decodes.

Two byte-exact facts pinned from ``SSEClient.swift`` (do not "fix" to the
spec — the Swift side is the authority):

  * The type line is parsed with ``line.dropFirst(6)`` after the
    ``event:`` prefix WITHOUT trimming — so it MUST be ``event:daemon``
    with **no space** (``event: daemon`` yields ``" daemon"`` and the
    ``switch`` silently drops the frame).
  * The frame requires an ``event:`` line at all (``guard let type`` →
    ``nil`` without it). Bare ``id:``/``data:`` frames — what the local
    ``EventStream`` emits — are dropped.

And the local ``EventStream`` wraps payloads as
``{"seq","ch","ts","d": <payload>}`` under ``data:``; ``JARVISKit``
decodes the ``data:`` JSON DIRECTLY into a flat ``Codable`` struct — so
the inner ``d`` must be UNWRAPPED and re-emitted flat.

DRY (mandate 3): this is a pure string/dict transform applied at the
device-stream boundary — it reuses the existing ``EventStream`` generator
output; it does NOT stand up a second SSE server.

Every function NEVER raises.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# EventStream frames look like ``id: <seq>\ndata: {json}\n\n``.
_ID_RE = re.compile(r"^id:\s*(\d+)", re.MULTILINE)
_DATA_RE = re.compile(r"^data:\s?(.*)$", re.MULTILINE)

# O+V / governance payload ``type`` → the Swift event vocabulary
# (token/daemon/status/complete/action/heartbeat). O+V telemetry is the
# "daemon" channel. Unknown types pass through verbatim (Swift drops what
# it doesn't know — safe).
_TYPE_MAP = {
    "ov_activity": "daemon",
    "ov_activity_batch": "daemon",
    "daemon": "daemon",
    "governance": "daemon",
    "token": "token",
    "status": "status",
    "complete": "complete",
    "action": "action",
}


def render_jarviskit_frame(
    seq: Optional[int], event_type: str, payload: Dict[str, Any],
) -> str:
    """The canonical JARVISKit SSE frame. NO space after ``event:``/
    ``data:`` — the Swift parser strips a fixed prefix length, not
    whitespace. Terminated with ``\\n\\n``. NEVER raises."""
    try:
        body = json.dumps(payload, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        body = "{}"
    id_line = f"id:{seq}\n" if seq is not None else ""
    return f"{id_line}event:{event_type}\ndata:{body}\n\n"


def daemon_payload(
    *, command_id: str, narration_text: str,
    narration_priority: str = "normal", source_brain: str = "ouroboros",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a payload that satisfies the Swift ``DaemonEvent`` Codable
    (all four keys REQUIRED). Extra keys are ignored by ``Codable`` — safe
    to attach detail for other consumers. NEVER raises."""
    p: Dict[str, Any] = {
        "command_id": str(command_id or "ouroboros"),
        "narration_text": str(narration_text or ""),
        "narration_priority": str(narration_priority or "normal"),
        "source_brain": str(source_brain or "ouroboros"),
    }
    if extra:
        for k, v in extra.items():
            p.setdefault(k, v)
    return p


def eventstream_frame_to_jarviskit(raw_frame: str) -> Optional[str]:
    """Translate ONE local ``EventStream`` frame into a ``JARVISKit`` SSE
    frame. Unwraps the ``d`` payload, reads its ``type``, maps to the Swift
    event vocabulary, and re-emits flat with the strict ``event:`` line.

    Returns None for a keepalive / unparseable / non-typed frame (the
    device stream passes those through untouched). NEVER raises."""
    try:
        if not raw_frame or raw_frame.startswith(":"):
            return None                         # keepalive — leave as-is
        m = _DATA_RE.search(raw_frame)
        if not m:
            return None
        envelope = json.loads(m.group(1))
        # EventStream wraps the real payload under "d"; unwrap it.
        inner = envelope.get("d") if isinstance(envelope, dict) else None
        if not isinstance(inner, dict):
            inner = envelope if isinstance(envelope, dict) else {}
        raw_type = str(inner.get("type", "") or "")
        if not raw_type:
            return None
        event_type = _TYPE_MAP.get(raw_type, raw_type)
        seq_m = _ID_RE.search(raw_frame)
        seq = int(seq_m.group(1)) if seq_m else None
        # For a daemon frame, guarantee the DaemonEvent-required keys.
        if event_type == "daemon":
            flat = daemon_payload(
                command_id=inner.get("command_id") or inner.get("op_id")
                or "ouroboros",
                narration_text=inner.get("narration_text")
                or inner.get("event") or raw_type,
                narration_priority=inner.get("narration_priority") or "normal",
                source_brain=inner.get("source_brain") or "ouroboros",
                extra={k: v for k, v in inner.items()
                       if k not in ("command_id", "narration_text",
                                    "narration_priority", "source_brain",
                                    "type")},
            )
        else:
            flat = inner
        return render_jarviskit_frame(seq, event_type, flat)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "render_jarviskit_frame", "daemon_payload",
    "eventstream_frame_to_jarviskit",
]
