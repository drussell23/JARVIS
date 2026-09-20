"""``ov reachability`` — the negative-space panel.

Every cockpit surface in this tree renders what the organism is DOING. This
one renders what it is failing to do, because that is this system's
characteristic failure and nothing was watching for it.

The audit that produced it: the micro-fix ran 532 times and repaired
nothing, the VALIDATE_RETRY ladder never regenerated once in the system's
history, 43 ``_ENABLED`` switches default off and are set nowhere. All of
them registered. Most were invoked. None were ever effective, and every
existing surface showed a healthy organism throughout.

The alarm is ``invoked > 0 and effective == 0``. On its first 48 samples it
caught a live one: ``api_grounding_gate`` invoked 48 times, effective 0 --
because it was reading ``ctx.plan``, which does not exist, and adjudicating
an empty string.

Decoupling
----------

The ledger lives in the daemon; this panel is a different process and reads
the append-only JSONL the daemon writes. The isolation is therefore
structural, not a convention someone has to keep: a rendering fault here
cannot reach the FSM because there is no shared object to fault through.
The file read happens in a worker thread, so a slow disk stalls the frame
and not the loop -- the same discipline the control plane itself needed.

Read-only by construction. This module imports nothing that can mutate
governance state and holds no handle to the orchestrator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ReachabilityPanel")

_DEFAULT_POLL_S = 2.0
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def ledger_path() -> Optional[Path]:
    """The same path the daemon's ledger writes, read from the same env."""
    raw = (os.environ.get("JARVIS_REACHABILITY_LEDGER_PATH", "") or "").strip()
    return Path(raw) if raw else None


def poll_interval_s() -> float:
    try:
        v = float(
            (os.environ.get("JARVIS_REACHABILITY_PANEL_POLL_S", "") or "").strip()
            or _DEFAULT_POLL_S
        )
        return v if v >= 0.25 else _DEFAULT_POLL_S
    except (TypeError, ValueError):
        return _DEFAULT_POLL_S


def max_read_bytes() -> int:
    """Cap on how much of the ledger tail is parsed per frame.

    An append-only file grows without bound, and a panel that re-read a
    100MB ledger every two seconds would be a worse I/O offender than
    anything it reports.
    """
    try:
        v = int(
            (os.environ.get("JARVIS_REACHABILITY_PANEL_MAX_BYTES", "") or "").strip()
            or _DEFAULT_MAX_BYTES
        )
        return v if v > 0 else _DEFAULT_MAX_BYTES
    except (TypeError, ValueError):
        return _DEFAULT_MAX_BYTES


@dataclass
class CapabilityRow:
    """One capability's standing, as the ledger records it."""

    capability: str
    registered: int = 0
    invoked: int = 0
    effective: int = 0
    last_detail: str = ""

    @property
    def inert(self) -> bool:
        """Reached, and never changed anything. The alarm."""
        return self.invoked > 0 and self.effective == 0

    @property
    def dormant(self) -> bool:
        """Loaded, and never reached."""
        return self.registered > 0 and self.invoked == 0

    @property
    def yield_pct(self) -> float:
        """Effective per invocation. The number a roadmap should be graded
        on, rather than whether the code exists."""
        return 100.0 * self.effective / self.invoked if self.invoked else 0.0

    @property
    def verdict(self) -> str:
        if self.inert:
            return "INERT"
        if self.dormant:
            return "DORMANT"
        return "LIVE"


@dataclass
class ReachabilityModel:
    """What the panel knows. Rebuilt per frame; holds no governance state."""

    rows: List[CapabilityRow] = field(default_factory=list)
    total_records: int = 0
    truncated: bool = False
    error: str = ""

    @property
    def inert(self) -> List[CapabilityRow]:
        """Inert capabilities, worst first -- most-invoked wastes most."""
        return sorted(
            (r for r in self.rows if r.inert), key=lambda r: -r.invoked,
        )

    @property
    def dormant(self) -> List[CapabilityRow]:
        return sorted((r for r in self.rows if r.dormant), key=lambda r: r.capability)

    @property
    def live(self) -> List[CapabilityRow]:
        return sorted(
            (r for r in self.rows if not r.inert and not r.dormant),
            key=lambda r: -r.effective,
        )


def aggregate(lines: List[str]) -> ReachabilityModel:
    """Fold ledger records into per-capability standings. NEVER raises.

    A malformed line is skipped rather than fatal: the ledger is appended
    from a live process and a torn final line is an ordinary condition, not
    a reason to blank the panel.
    """
    rows: Dict[str, CapabilityRow] = {}
    seen = 0
    for line in lines:
        try:
            record = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        cap = str(record.get("capability", "") or "")
        tier = str(record.get("tier", "") or "")
        if not cap or not tier:
            continue
        seen += 1
        row = rows.setdefault(cap, CapabilityRow(capability=cap))
        if tier == "registered":
            row.registered += 1
        elif tier == "invoked":
            row.invoked += 1
        elif tier == "effective":
            row.effective += 1
        detail = str(record.get("detail", "") or "")
        if detail:
            row.last_detail = detail[:80]
    return ReachabilityModel(rows=list(rows.values()), total_records=seen)


def _read_tail(path: Path, cap_bytes: int) -> Tuple[List[str], bool]:
    """The last *cap_bytes* of the ledger, as whole lines.

    Runs in a worker thread. The first line of a tail read is usually a
    fragment, so it is dropped -- a partial record would otherwise be
    counted as a malformed one forever.
    """
    size = path.stat().st_size
    truncated = size > cap_bytes
    with path.open("rb") as fh:
        if truncated:
            fh.seek(size - cap_bytes)
        blob = fh.read()
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]
    return lines, truncated


async def load_model(path: Optional[Path] = None) -> ReachabilityModel:
    """Read and fold the ledger without touching the event loop's budget.

    The file read is offloaded; the fold is pure. NEVER raises -- a panel
    that dies on a missing file teaches operators to distrust the surface
    that would have told them something was wrong.
    """
    target = path or ledger_path()
    if target is None:
        return ReachabilityModel(
            error="JARVIS_REACHABILITY_LEDGER_PATH is not set — the daemon "
                  "is not recording execution provenance",
        )
    try:
        if not target.is_file():
            return ReachabilityModel(
                error=f"no ledger at {target} — nothing has been recorded yet",
            )
        lines, truncated = await asyncio.to_thread(
            _read_tail, target, max_read_bytes(),
        )
    except Exception as exc:  # noqa: BLE001
        return ReachabilityModel(error=f"{type(exc).__name__}: {exc}")
    model = aggregate(lines)
    model.truncated = truncated
    return model


def render_rows(model: ReachabilityModel) -> List[str]:
    """Plain-text rendering, used when Rich is unavailable and by tests.

    Ordering is the message: inert first, because a capability that runs and
    changes nothing is costing compute to do nothing, which is worse than
    one that never runs at all.
    """
    out: List[str] = []
    if model.error:
        return [f"  {model.error}"]
    if not model.rows:
        return ["  ledger is empty — no capability has reported yet"]

    def _fmt(row: CapabilityRow) -> str:
        return (
            f"  {row.verdict:<8} {row.capability:<28} "
            f"reg={row.registered:<5} inv={row.invoked:<5} "
            f"eff={row.effective:<5} yield={row.yield_pct:5.1f}%"
        )

    if model.inert:
        out.append("INERT — invoked, never changed an outcome:")
        out.extend(_fmt(r) for r in model.inert)
    if model.dormant:
        out.append("DORMANT — loaded, never reached:")
        out.extend(_fmt(r) for r in model.dormant)
    if model.live:
        out.append("LIVE:")
        out.extend(_fmt(r) for r in model.live)
    if model.truncated:
        out.append(f"  (tail only — ledger exceeds {max_read_bytes()} bytes)")
    return out


def render_panel(model: ReachabilityModel) -> object:
    """A Rich table when Rich is present, else the plain rendering.

    Colour carries the verdict: red is a capability burning compute to no
    effect, yellow one that has never run, green one that is working.
    """
    try:
        from rich.table import Table
        from rich.text import Text
    except Exception:  # noqa: BLE001
        return "\n".join(render_rows(model))

    table = Table(
        title="Reachability — what the organism is NOT doing",
        expand=True,
    )
    table.add_column("verdict", no_wrap=True)
    table.add_column("capability", no_wrap=True)
    table.add_column("reg", justify="right")
    table.add_column("inv", justify="right")
    table.add_column("eff", justify="right")
    table.add_column("yield", justify="right")
    table.add_column("last", overflow="ellipsis")

    if model.error:
        table.add_row(Text("NO DATA", style="yellow"), model.error, "", "", "", "", "")
        return table

    style_for = {"INERT": "bold red", "DORMANT": "yellow", "LIVE": "green"}
    for row in list(model.inert) + list(model.dormant) + list(model.live):
        table.add_row(
            Text(row.verdict, style=style_for.get(row.verdict, "")),
            row.capability,
            str(row.registered), str(row.invoked), str(row.effective),
            f"{row.yield_pct:.0f}%",
            row.last_detail,
        )
    return table


async def watch(
    path: Optional[Path] = None,
    *,
    iterations: Optional[int] = None,
) -> None:
    """Poll the ledger and render. ``iterations`` bounds it for tests.

    A render fault is logged and the loop continues: the panel exists to
    report degradation, so it must survive degrading itself.
    """
    try:
        from rich.console import Console
        from rich.live import Live
    except Exception:  # noqa: BLE001
        model = await load_model(path)
        print("\n".join(render_rows(model)))
        return

    console = Console()
    ticks = 0
    with Live(console=console, refresh_per_second=4, screen=False) as live:
        while iterations is None or ticks < iterations:
            try:
                live.update(render_panel(await load_model(path)))
            except Exception:  # noqa: BLE001
                logger.debug("[ReachabilityPanel] frame degraded", exc_info=True)
            ticks += 1
            if iterations is not None and ticks >= iterations:
                break
            await asyncio.sleep(poll_interval_s())


__all__ = [
    "CapabilityRow",
    "ReachabilityModel",
    "aggregate",
    "ledger_path",
    "load_model",
    "max_read_bytes",
    "poll_interval_s",
    "render_panel",
    "render_rows",
    "watch",
]
