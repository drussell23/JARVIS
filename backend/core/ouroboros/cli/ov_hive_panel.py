"""``ov hive`` — the live agent-activity feed (Phase 12, Hive Step 1).

A read-only, scrolling projection of the REAL O+V pipeline: the Hive Aggregator
(daemon side) fans in the fragmented fabrics — TrinityEventBus + the IDE SSE
observability broker — into Universal Envelopes and relays them over the same
Cockpit Attach UDS the ``ov system`` cockpit uses. This panel attaches to that
socket, folds the ``hive`` frames into a chronological feed, and renders it.

DRY: reuses the Slice-G ``TelemetryConnectionManager`` (graceful reconnect) and
the ``ov system`` render idiom. Throttled batched rendering (100ms) so a burst of
deliberation can never starve the terminal thread. NEVER raises to the terminal.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.cli.ov_system_panel import (
    ConnState, TelemetryConnectionManager,
)

logger = logging.getLogger("Ouroboros.OVHivePanel")

#: subsystem → accent colour for the feed.
_SUB_COLOR = {
    "governance": "cyan", "swarm": "magenta", "sensor": "yellow",
    "routing": "blue", "training": "green", "mesh": "bright_magenta",
    "consciousness": "bright_blue", "perception": "bright_cyan",
    "actuation": "bright_yellow", "mcp": "bright_green", "persona": "white",
}


@dataclass
class HiveFeedModel:
    """The rolling, chronological agent-activity feed folded from hive frames."""
    lines: List[Tuple[float, str, str, str, str]] = field(default_factory=list)  # ts, actor, subsystem, summary, severity
    seen_fabrics: set = field(default_factory=set)
    _MAX = 500

    def ingest(self, frame: Dict[str, Any]) -> None:
        """Fold ONE cockpit frame into the feed (only `hive` frames). NEVER raises."""
        try:
            if not frame.get("hive"):
                return
            ts = float(frame.get("ts", time.time()))
            actor = str(frame.get("actor_id") or frame.get("source_brain") or "?")
            subsystem = str(frame.get("subsystem") or "bus")
            summary = str(frame.get("action_summary") or frame.get("narration_text") or "")
            sev = str(frame.get("severity") or "info")
            self.seen_fabrics.add(str(frame.get("source_fabric") or "?"))
            self.lines.append((ts, actor, subsystem, summary, sev))
            if len(self.lines) > self._MAX:
                del self.lines[: len(self.lines) - self._MAX]
        except Exception:  # noqa: BLE001
            pass


def render_hive_feed(model: HiveFeedModel, state: "ConnState") -> Any:
    """Rich renderable — a scrolling feed of who did/said what. NEVER raises."""
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.console import Group

    online = state is ConnState.ATTACHED
    header = Table.grid(expand=True)
    header.add_column(justify="left")
    header.add_column(justify="right")
    dot = "🟢" if online else ("🟠" if state is ConnState.RECONNECTING else "🔴")
    fabrics = " ".join(sorted(model.seen_fabrics)) or "—"
    header.add_row(Text(f"{dot} ov · hive — live agent feed", style="bold"),
                   Text(f"fabrics: {fabrics}", style="dim"))

    feed = Table.grid(expand=True, padding=(0, 1))
    feed.add_column(style="dim", no_wrap=True)   # time
    feed.add_column(no_wrap=True)                 # actor
    feed.add_column()                             # summary
    tail = model.lines[-22:]
    for ts, actor, subsystem, summary, sev in tail:
        clock = time.strftime("%H:%M:%S", time.localtime(ts))
        color = _SUB_COLOR.get(subsystem, "white")
        sev_mark = {"error": "✗ ", "warn": "! ", "success": "✓ "}.get(sev, "")
        feed.add_row(Text(clock, style="dim"),
                     Text(f"{actor}", style=color),
                     Text(f"{sev_mark}{summary}",
                          style="red" if sev == "error" else "default"))
    if not model.lines:
        feed.add_row(Text(""), Text(""),
                     Text("(awaiting agent activity — run an O+V op to see the pipeline talk)",
                          style="dim italic"))

    parts: List[Any] = [header, Text("")]
    if not online:
        parts += [Text("  DAEMON OFFLINE — ATTEMPTING RECONNECT  ",
                       style="bold white on red"), Text("")]
    parts.append(feed)
    return Panel(Group(*parts), title="JARVIS · Agent Hive",
                 border_style="green" if online else "red")


async def run_hive_panel(
    *, manager: Optional[TelemetryConnectionManager] = None,
    refresh_hz: float = 10.0, console: Any = None,
    stop_after_s: Optional[float] = None,
) -> int:
    """Attach to the cockpit UDS and render the live hive feed. Throttled 100ms
    batched render (mandate). NEVER raises; returns an exit code."""
    from rich.console import Console
    from rich.live import Live

    model = HiveFeedModel()
    mgr = manager or TelemetryConnectionManager(on_frame=model.ingest)
    if manager is not None:
        prev = mgr._on_frame
        mgr._on_frame = lambda f: (model.ingest(f), prev(f))  # type: ignore
    con = console or Console()

    run_task = asyncio.get_event_loop().create_task(mgr.run(), name="ov-hive-conn")
    started = time.monotonic()
    try:
        with Live(render_hive_feed(model, mgr.state), console=con,
                  refresh_per_second=max(1.0, refresh_hz), screen=False) as live:
            while True:
                await asyncio.sleep(0.1)   # 100ms batched render
                live.update(render_hive_feed(model, mgr.state))
                if stop_after_s is not None and (time.monotonic() - started) >= stop_after_s:
                    break
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    except Exception:  # noqa: BLE001
        logger.debug("[OVHive] render degraded", exc_info=True)
    finally:
        mgr.stop()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    return 0


__all__ = ["HiveFeedModel", "render_hive_feed", "run_hive_panel"]
