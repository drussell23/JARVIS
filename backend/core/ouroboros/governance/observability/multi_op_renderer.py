"""Phase 8 surface wiring Slice 3 — SerpentFlow ``--multi-op``
renderer.

Pure-stdlib, read-only projector over the decision-trace ledger that
produces a chronological multi-op timeline view for operator-side
inspection. Composes:

  * ``DecisionTraceLedger.reconstruct_op`` — per-op row reads.
  * ``multi_op_timeline.merge_streams`` — deterministic O(N log K)
    merge across labeled streams.
  * ``multi_op_timeline.render_text_timeline`` — plain-text view.

Plus a lightweight ANSI-color renderer that color-codes by op_id
so multiple ops are visually distinct in the same view.

Authority posture:

  * **Read-only** — never mutates the ledger or the substrate state.
  * **Stdlib-only top-level imports** — substrate is imported lazily
    inside the rendering helpers (pinned by AST scan).
  * **NEVER raises** — every error path returns a structured
    ``Optional[str]`` or empty list.
  * **Bounded outputs**: at most ``MAX_OPS_PER_RENDER=16`` ops
    merged into a single view; per-op rows capped by the ledger's
    own ``MAX_RECORDS_LOADED``; rendered text capped at
    ``MAX_RENDERED_LINES=400``.
  * **Master flag** — ``JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED``
    (default false until graduation). When off, every entry point
    returns an "(disabled)" stub or empty list.

## CLI surface (wired in ``scripts/ouroboros_battle_test.py``)

The ``--multi-op REF`` argument routes here:

  * ``--multi-op list``            → :func:`list_recent_op_ids`
  * ``--multi-op op-A,op-B,op-C``  → :func:`render_multi_op_timeline`
  * ``--multi-op @last:N``         → most-recent N ops
  * ``--multi-op session:<id>``    → ops referenced in a session
                                     summary (Phase 8 substrate
                                     does not auto-link sessions →
                                     ledgers; this reads
                                     ``operations[].op_id`` from
                                     ``summary.json`` if present).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Bounded caps — defends against runaway inputs.
MAX_OPS_PER_RENDER: int = 16
MAX_RENDERED_LINES: int = 400
MAX_OP_ID_LEN: int = 128
MAX_LIST_OP_IDS: int = 200


# Stable ANSI color cycle. Operator can disable via tty-aware check
# in the entry helpers (color stripped when stdout isn't a TTY).
_ANSI_RESET = "\033[0m"
_ANSI_PALETTE: Tuple[str, ...] = (
    "\033[36m",  # cyan
    "\033[32m",  # green
    "\033[35m",  # magenta
    "\033[33m",  # yellow
    "\033[34m",  # blue
    "\033[31m",  # red
    "\033[37m",  # white
    "\033[96m",  # bright cyan
    "\033[92m",  # bright green
    "\033[95m",  # bright magenta
    "\033[93m",  # bright yellow
    "\033[94m",  # bright blue
    "\033[91m",  # bright red
    "\033[90m",  # bright black (gray)
    "\033[97m",  # bright white
    "\033[36;1m",  # bold cyan
)


# ---------------------------------------------------------------------------
# Master flag + parsing helpers
# ---------------------------------------------------------------------------


def is_renderer_enabled() -> bool:
    """Master flag — ``JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _disabled_message() -> str:
    return (
        "(multi-op renderer disabled — set "
        "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED=true)"
    )


def _validate_op_id(s: str) -> Optional[str]:
    """Return cleaned op_id when valid, None when malformed.

    Accepts ``[A-Za-z0-9_-]{1,MAX_OP_ID_LEN}``. Rejects path-traversal
    + whitespace-injection + control characters."""
    if not isinstance(s, str):
        return None
    cleaned = s.strip()
    if not cleaned or len(cleaned) > MAX_OP_ID_LEN:
        return None
    for ch in cleaned:
        if not (ch.isalnum() or ch in ("-", "_")):
            return None
    return cleaned


def parse_multi_op_argument(arg: str) -> Tuple[str, Any]:
    """Parse the operator-supplied REF into ``(kind, payload)``.

    Returns:
      * ``("list", None)`` for ``"list"``
      * ``("ops", [op_id, ...])`` for comma-separated op_ids
      * ``("last_n", N)`` for ``"@last[:N]"`` (default N=5)
      * ``("session", session_id)`` for ``"session:<id>"``
      * ``("invalid", reason)`` on malformed input

    NEVER raises.
    """
    if not isinstance(arg, str) or not arg.strip():
        return ("invalid", "empty_argument")
    s = arg.strip()
    if s.lower() == "list":
        return ("list", None)
    if s.startswith("@last"):
        # Forms: "@last", "@last:N"
        if s == "@last":
            return ("last_n", 5)
        rest = s[len("@last"):]
        if rest.startswith(":"):
            try:
                n = int(rest[1:])
            except ValueError:
                return ("invalid", "bad_last_n")
            if n < 1:
                return ("invalid", "non_positive_last_n")
            if n > MAX_OPS_PER_RENDER:
                n = MAX_OPS_PER_RENDER
            return ("last_n", n)
        return ("invalid", "bad_last_format")
    if s.startswith("session:"):
        sid = s[len("session:"):].strip()
        # Same charset as op_id (session IDs follow the same pattern).
        cleaned = _validate_op_id(sid)
        if cleaned is None:
            return ("invalid", "bad_session_id")
        return ("session", cleaned)
    # Comma-separated op_ids.
    parts = [p for p in s.split(",") if p.strip()]
    if not parts:
        return ("invalid", "no_op_ids_after_split")
    if len(parts) > MAX_OPS_PER_RENDER:
        parts = parts[:MAX_OPS_PER_RENDER]
    op_ids: List[str] = []
    for p in parts:
        cleaned = _validate_op_id(p)
        if cleaned is None:
            return ("invalid", f"bad_op_id:{p[:32]}")
        op_ids.append(cleaned)
    if not op_ids:
        return ("invalid", "no_valid_op_ids")
    return ("ops", op_ids)


# ---------------------------------------------------------------------------
# Ledger reads (lazy substrate import)
# ---------------------------------------------------------------------------


def list_recent_op_ids(
    *,
    limit: int = 20,
    ledger_path: Optional[Path] = None,
) -> List[str]:
    """Return up to ``limit`` distinct op_ids from the decision-trace
    ledger, most-recent-first.

    Master-off → empty list. NEVER raises.
    """
    if not is_renderer_enabled():
        return []
    if limit < 1:
        return []
    if limit > MAX_LIST_OP_IDS:
        limit = MAX_LIST_OP_IDS
    path = ledger_path or _resolve_ledger_path()
    try:
        if not path.exists():
            return []
    except OSError:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    seen: Dict[str, None] = {}  # ordered set
    # Walk reverse for most-recent-first.
    for line in reversed(text.splitlines()):
        if len(seen) >= limit:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        op = obj.get("op_id")
        if not isinstance(op, str):
            continue
        cleaned = _validate_op_id(op)
        if cleaned is None:
            continue
        seen.setdefault(cleaned)
    return list(seen.keys())


def _resolve_ledger_path() -> Path:
    """Lazy substrate-import wrapper around
    ``decision_trace_ledger.ledger_path()``."""
    try:
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            ledger_path,
        )
        return ledger_path()
    except Exception:  # noqa: BLE001 — defensive
        return Path(".jarvis") / "decision_trace.jsonl"


def _load_op_rows(op_id: str) -> List[Any]:
    """Lazy-import + read all rows for one op_id. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.observability.decision_trace_ledger import (  # noqa: E501
            get_default_ledger,
        )
        ledger = get_default_ledger()
        return list(ledger.reconstruct_op(op_id))
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiOpRenderer] _load_op_rows exception op_id=%r",
            op_id, exc_info=True,
        )
        return []


def _build_streams(op_ids: Sequence[str]) -> Dict[str, List[Any]]:
    """Build the ``{op_id: [TimelineEvent, ...]}`` dict for
    :func:`merge_streams`. Each op becomes one stream."""
    try:
        from backend.core.ouroboros.governance.observability.multi_op_timeline import (  # noqa: E501
            TimelineEvent,
        )
    except Exception:  # noqa: BLE001
        return {}
    streams: Dict[str, List[Any]] = {}
    for op_id in op_ids:
        rows = _load_op_rows(op_id)
        if not rows:
            continue
        evs: List[Any] = []
        for i, r in enumerate(rows):
            evs.append(TimelineEvent(
                ts_epoch=getattr(r, "ts_epoch", 0.0),
                stream_id=op_id,
                event_type="decision",
                payload={
                    "phase": getattr(r, "phase", ""),
                    "decision": getattr(r, "decision", ""),
                    "rationale": getattr(r, "rationale", "")[:200],
                },
                seq=i,
            ))
        streams[op_id] = evs
    return streams


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_multi_op_timeline(
    op_ids: Sequence[str],
    *,
    color: bool = False,
    max_lines: int = MAX_RENDERED_LINES,
) -> str:
    """Render ``op_ids`` as one chronological timeline.

    Args:
      op_ids: list of op_ids (each cleaned + capped to
        MAX_OPS_PER_RENDER).
      color: when True, prefix each line with an ANSI color code
        cycled per op_id. Caller decides — this function never
        consults isatty(); the CLI hook does that.
      max_lines: hard cap on output lines (defaults to
        MAX_RENDERED_LINES).

    Returns the rendered text. Master-off / no rows → "(disabled)"
    or "(no events for any op)".
    """
    if not is_renderer_enabled():
        return _disabled_message()
    if not op_ids:
        return "(no op_ids supplied)"
    cleaned: List[str] = []
    for op in op_ids:
        c = _validate_op_id(op)
        if c is not None:
            cleaned.append(c)
        if len(cleaned) >= MAX_OPS_PER_RENDER:
            break
    if not cleaned:
        return "(no valid op_ids)"
    streams = _build_streams(cleaned)
    if not streams:
        return f"(no events for any op in: {', '.join(cleaned)})"
    try:
        from backend.core.ouroboros.governance.observability.multi_op_timeline import (  # noqa: E501
            merge_streams, render_text_timeline,
        )
    except Exception:  # noqa: BLE001
        return "(timeline substrate unavailable)"
    merged = merge_streams(streams)
    if not merged:
        return "(merged timeline empty)"
    if not color:
        return render_text_timeline(merged, max_lines=max_lines)
    # Color path: build palette mapping by op_id (alpha-stable).
    palette: Dict[str, str] = {}
    for i, op in enumerate(sorted(streams.keys())):
        palette[op] = _ANSI_PALETTE[i % len(_ANSI_PALETTE)]
    out_lines: List[str] = []
    plain = render_text_timeline(merged, max_lines=max_lines)
    for line in plain.splitlines():
        # Detect the [stream_id] segment — render_text_timeline emits
        # "<ts> [stream_id] event_type :: payload".
        prefix = ""
        for op_id, code in palette.items():
            if f"[{op_id}" in line or f"[{op_id:<12}" in line:
                prefix = code
                break
        if prefix:
            out_lines.append(f"{prefix}{line}{_ANSI_RESET}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def render_last_n_op_timeline(
    n: int,
    *,
    color: bool = False,
    max_lines: int = MAX_RENDERED_LINES,
) -> str:
    """Convenience: take the most-recent N op_ids and render them
    as a multi-op timeline.

    NEVER raises."""
    if not is_renderer_enabled():
        return _disabled_message()
    if n < 1:
        return "(non-positive N)"
    if n > MAX_OPS_PER_RENDER:
        n = MAX_OPS_PER_RENDER
    op_ids = list_recent_op_ids(limit=n)
    if not op_ids:
        return "(no recent ops in the ledger)"
    return render_multi_op_timeline(
        op_ids, color=color, max_lines=max_lines,
    )


def render_session_timeline(
    session_id: str,
    *,
    color: bool = False,
    max_lines: int = MAX_RENDERED_LINES,
    sessions_root: Optional[Path] = None,
) -> str:
    """Render every op_id present in a battle-test session's
    ``summary.json``. Looks up
    ``.ouroboros/sessions/<session_id>/summary.json``.

    Each op listed under ``operations[]`` is rendered.

    NEVER raises."""
    if not is_renderer_enabled():
        return _disabled_message()
    cleaned = _validate_op_id(session_id)
    if cleaned is None:
        return "(invalid session_id)"
    root = sessions_root or (Path(".ouroboros") / "sessions")
    summary_path = root / cleaned / "summary.json"
    try:
        if not summary_path.exists():
            return f"(session not found: {cleaned})"
    except OSError:
        return "(session lookup failed)"
    try:
        text = summary_path.read_text(encoding="utf-8")
    except OSError:
        return f"(could not read summary.json for: {cleaned})"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return f"(corrupt summary.json for: {cleaned})"
    op_ids: List[str] = []
    if isinstance(data, dict):
        ops_field = data.get("operations") or []
        if isinstance(ops_field, list):
            for entry in ops_field:
                if isinstance(entry, dict):
                    op = entry.get("op_id")
                    op_clean = _validate_op_id(op) if op else None
                    if op_clean is not None and op_clean not in op_ids:
                        op_ids.append(op_clean)
                if len(op_ids) >= MAX_OPS_PER_RENDER:
                    break
    if not op_ids:
        return f"(no op_ids found in session: {cleaned})"
    return render_multi_op_timeline(
        op_ids, color=color, max_lines=max_lines,
    )


# ---------------------------------------------------------------------------
# CLI dispatch helper (called from scripts/ouroboros_battle_test.py)
# ---------------------------------------------------------------------------


def dispatch_cli_argument(
    arg: str,
    *,
    color: bool = False,
    max_lines: int = MAX_RENDERED_LINES,
    sessions_root: Optional[Path] = None,
) -> str:
    """Single-call dispatcher used by the ``--multi-op`` CLI hook.

    Parses ``arg`` via :func:`parse_multi_op_argument` then routes
    to the matching renderer. Returns the text to print. NEVER
    raises."""
    if not is_renderer_enabled():
        return _disabled_message()
    kind, payload = parse_multi_op_argument(arg)
    if kind == "invalid":
        return f"(invalid --multi-op argument: {payload})"
    if kind == "list":
        ids = list_recent_op_ids(limit=20)
        if not ids:
            return "(no recent ops in the ledger)"
        return "Recent ops in decision-trace ledger:\n" + "\n".join(
            f"  {op}" for op in ids
        )
    if kind == "ops":
        return render_multi_op_timeline(
            list(payload), color=color, max_lines=max_lines,
        )
    if kind == "last_n":
        return render_last_n_op_timeline(
            int(payload), color=color, max_lines=max_lines,
        )
    if kind == "session":
        return render_session_timeline(
            str(payload), color=color, max_lines=max_lines,
            sessions_root=sessions_root,
        )
    return "(unknown dispatch kind)"


__all__ = [
    "MAX_LIST_OP_IDS",
    "MAX_OPS_PER_RENDER",
    "MAX_OP_ID_LEN",
    "MAX_RENDERED_LINES",
    "dispatch_cli_argument",
    "is_renderer_enabled",
    "list_recent_op_ids",
    "parse_multi_op_argument",
    "render_last_n_op_timeline",
    "render_multi_op_timeline",
    "render_session_timeline",
]
