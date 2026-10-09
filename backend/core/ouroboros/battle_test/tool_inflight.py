"""Which tool is running RIGHT NOW, per op: the live half of a tool call.

A tool call has two moments. While it runs, the operator wants to know what
it is touching. When it finishes, the transcript keeps a record of what it did.
Claude Code shows the first in a live region that rewrites itself and keeps
only the second in scrollback.

The cockpit used to write both to the append-only transcript. The start line
came from `op_tool_start` and the completed header from `op_tool_call`, so
every tool read twice in scrollback: `● Read(x.py)` and then `● Read(x.py)
0.2s`. The start line was there for a good reason, because the local spinner
never reaches an attached cockpit. But its right home is the channel that
already rewrites itself at 1 Hz: the heartbeat.

This module is that hand-off. `SerpentFlow` records a start and a finish, and
`attach_heartbeat` reads the newest label for the op it is describing. It
holds no policy and no rendering: a label in, a label out.

Bounded and leak-proof. A crashed op never reports a finish, so its entries
are evicted oldest-first past `JARVIS_TOOL_INFLIGHT_MAX_OPS`, and a label
older than `JARVIS_TOOL_INFLIGHT_STALE_S` is treated as gone. A hung tool must
not keep saying "Reading" forever. NEVER raises.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import List, Optional, Tuple


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def max_ops() -> int:
    """How many ops may hold in-flight entries at once."""
    return int(_env_float("JARVIS_TOOL_INFLIGHT_MAX_OPS", 64, 1, 4096))


def stale_after_s() -> float:
    """How long a start without a finish still counts as running."""
    return _env_float("JARVIS_TOOL_INFLIGHT_STALE_S", 900.0, 1.0, 86400.0)


class InflightTools:
    """op_id -> the labels of its running tools, in start order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ops: "OrderedDict[str, List[Tuple[str, float]]]" = OrderedDict()

    def started(self, op_id: str, label: str, *,
                now: Optional[float] = None) -> None:
        key, text = str(op_id or ""), str(label or "").strip()
        if not key or not text:
            return
        stamp = time.monotonic() if now is None else float(now)
        try:
            with self._lock:
                self._ops.setdefault(key, []).append((text, stamp))
                self._ops.move_to_end(key)
                while len(self._ops) > max_ops():
                    self._ops.popitem(last=False)
        except Exception:  # noqa: BLE001 — losing a live label is cosmetic
            pass

    def finished(self, op_id: str, label: str) -> None:
        """Retire ONE matching start. Parallel identical calls retire one
        at a time. A label with no match (e.g. the start was evicted) is
        ignored."""
        key, text = str(op_id or ""), str(label or "").strip()
        try:
            with self._lock:
                running = self._ops.get(key)
                if not running:
                    return
                for i, (held, _) in enumerate(running):
                    if held == text:
                        del running[i]
                        break
                if not running:
                    self._ops.pop(key, None)
        except Exception:  # noqa: BLE001
            pass

    def clear(self, op_id: str) -> None:
        """The op ended. Whatever it left running is no longer running."""
        try:
            with self._lock:
                self._ops.pop(str(op_id or ""), None)
        except Exception:  # noqa: BLE001
            pass

    def current(self, op_id: str, *, now: Optional[float] = None) -> str:
        """The newest live label for *op_id*, with `(+N)` for the others
        running beside it. "" when nothing is running."""
        try:
            horizon = (time.monotonic() if now is None else float(now)) \
                - stale_after_s()
            with self._lock:
                live = [t for t, at in self._ops.get(str(op_id or ""), ())
                        if at >= horizon]
            if not live:
                return ""
            extra = len(live) - 1
            return f"{live[-1]} (+{extra})" if extra else live[-1]
        except Exception:  # noqa: BLE001
            return ""


_DEFAULT = InflightTools()


def get_inflight_tools() -> InflightTools:
    return _DEFAULT


__all__ = ["InflightTools", "get_inflight_tools", "max_ops", "stale_after_s"]
