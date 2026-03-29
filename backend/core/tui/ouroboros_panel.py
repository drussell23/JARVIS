"""Ouroboros TUI panel -- live autonomous activity dashboard.

Data layer and Rich-markup formatter for the Ouroboros tab.  Consumes
``ouroboros.*`` TelemetryEnvelopes and maintains bounded collections of
REM epochs, findings, sagas, and synthesis state.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from backend.core.telemetry_contract import TelemetryEnvelope


# ---------------------------------------------------------------------------
# Sub-entries
# ---------------------------------------------------------------------------

@dataclass
class RemEpochEntry:
    """Summary of a single completed REM epoch."""

    epoch_id: int
    state: str  # IDLE_WATCH, EXPLORING, ANALYZING, PATCHING, COOLDOWN, complete
    findings_count: int = 0
    envelopes_submitted: int = 0
    started_at: float = 0.0
    duration_s: float = 0.0


@dataclass
class SagaEntry:
    """Lightweight mirror of a saga's lifecycle."""

    saga_id: str
    title: str
    phase: str  # pending, running, complete, aborted
    total_steps: int = 0
    completed_steps: int = 0
    started_at: float = 0.0


@dataclass
class FindingEntry:
    """One exploration finding surfaced by a REM epoch."""

    description: str
    category: str
    source: str  # oracle, fleet, roadmap
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Panel data class
# ---------------------------------------------------------------------------

@dataclass
class OuroborosData:
    """Data layer for the Ouroboros TUI panel.

    Follows the same pattern as :class:`PipelineData` et al.: a pure data
    container with an ``update(envelope)`` method that is called by the
    :class:`TelemetryBusConsumer` router.
    """

    # Current REM state
    rem_state: str = "IDLE_WATCH"
    current_epoch_id: int = 0
    vital_status: str = "unknown"
    spinal_status: str = "unknown"
    narrator_enabled: bool = False

    # Cumulative counters
    total_epochs: int = 0
    total_findings: int = 0
    total_envelopes: int = 0
    total_prs: int = 0

    # Live activity (bounded)
    recent_findings: Deque[FindingEntry] = field(
        default_factory=lambda: deque(maxlen=10)
    )
    active_sagas: Dict[str, SagaEntry] = field(default_factory=dict)
    recent_sagas: Deque[SagaEntry] = field(
        default_factory=lambda: deque(maxlen=5)
    )
    last_epoch: Optional[RemEpochEntry] = None

    # Synthesis
    synthesis_hypothesis_count: int = 0
    synthesis_last_run: float = 0.0

    # Roadmap
    roadmap_snapshot_version: int = 0
    roadmap_fragment_count: int = 0

    # ------------------------------------------------------------------
    # Envelope ingestion
    # ------------------------------------------------------------------

    def update(self, envelope: TelemetryEnvelope) -> None:
        """Route a telemetry envelope to the appropriate handler."""
        schema = envelope.event_schema
        payload = envelope.payload

        if not schema.startswith("ouroboros."):
            return

        # Strip domain prefix and version: "ouroboros.rem.epoch_start@1.0.0" -> "rem.epoch_start"
        event_type = schema.replace("ouroboros.", "").split("@")[0]

        if event_type == "rem.epoch_start":
            self.rem_state = "EXPLORING"
            self.current_epoch_id = payload.get("epoch_id", 0)

        elif event_type == "rem.epoch_complete":
            self.rem_state = "COOLDOWN"
            self.total_epochs += 1
            fc = payload.get("findings_count", 0)
            ec = payload.get("envelopes_submitted", 0)
            self.total_findings += fc
            self.total_envelopes += ec
            self.last_epoch = RemEpochEntry(
                epoch_id=payload.get("epoch_id", 0),
                state="complete",
                findings_count=fc,
                envelopes_submitted=ec,
                duration_s=payload.get("duration_s", 0.0),
            )

        elif event_type == "rem.idle_watch":
            self.rem_state = "IDLE_WATCH"

        elif event_type == "finding":
            self.recent_findings.append(FindingEntry(
                description=payload.get("description", "")[:80],
                category=payload.get("category", "unknown"),
                source=payload.get("source", "unknown"),
                timestamp=time.time(),
            ))

        elif event_type == "synthesis.complete":
            self.synthesis_hypothesis_count = payload.get("hypothesis_count", 0)
            self.synthesis_last_run = time.time()

        elif event_type == "saga.started":
            sid = payload.get("saga_id", "")
            self.active_sagas[sid] = SagaEntry(
                saga_id=sid,
                title=payload.get("title", payload.get("plan_id", "unknown")),
                phase="running",
                total_steps=payload.get("step_count", 0),
                started_at=time.time(),
            )

        elif event_type == "saga.complete":
            sid = payload.get("saga_id", "")
            if sid in self.active_sagas:
                saga = self.active_sagas.pop(sid)
                saga.phase = "complete"
                self.recent_sagas.append(saga)
            self.total_prs += 1

        elif event_type == "saga.aborted":
            sid = payload.get("saga_id", "")
            if sid in self.active_sagas:
                saga = self.active_sagas.pop(sid)
                saga.phase = "aborted"
                self.recent_sagas.append(saga)

        elif event_type == "vital":
            self.vital_status = payload.get("status", "unknown")

        elif event_type == "spinal":
            self.spinal_status = payload.get("status", "unknown")

        elif event_type == "roadmap.snapshot":
            self.roadmap_snapshot_version = payload.get("version", 0)
            self.roadmap_fragment_count = payload.get("fragment_count", 0)

        elif event_type == "patch_applied":
            self.total_prs += 1


# ---------------------------------------------------------------------------
# Rich formatter
# ---------------------------------------------------------------------------

def format_ouroboros_display(data: OuroborosData) -> str:
    """Format the Ouroboros panel content with Rich markup.

    Returns a single string with embedded Rich markup tags suitable for
    :meth:`RichLog.write`.
    """
    lines: List[str] = []

    # -- Header with REM state --
    state_colors = {
        "IDLE_WATCH": "dim white",
        "EXPLORING": "bold cyan",
        "ANALYZING": "bold yellow",
        "PATCHING": "bold green",
        "COOLDOWN": "dim blue",
    }
    color = state_colors.get(data.rem_state, "white")
    lines.append(f"[{color}]{'=' * 48}[/]")
    lines.append(
        f"[bold white]  OUROBOROS[/] [dim]|[/] [{color}] {data.rem_state} [/]"
        f"[dim]|[/] [dim]epoch #{data.current_epoch_id}[/]"
    )
    lines.append(f"[{color}]{'=' * 48}[/]")
    lines.append("")

    # -- System status --
    vital_c = (
        "green" if data.vital_status == "pass"
        else "yellow" if data.vital_status == "warn"
        else "red"
    )
    spinal_c = "green" if data.spinal_status == "connected" else "yellow"
    narrator_str = "[green]on[/]" if data.narrator_enabled else "[dim]off[/]"
    lines.append(
        f"  [dim]Vital:[/] [{vital_c}]{data.vital_status}[/]"
        f"  [dim]Spinal:[/] [{spinal_c}]{data.spinal_status}[/]"
        f"  [dim]Narrator:[/] {narrator_str}"
    )
    lines.append("")

    # -- Cumulative stats --
    lines.append(
        f"  [dim]Epochs:[/] [bold]{data.total_epochs}[/]"
        f"  [dim]Findings:[/] [bold]{data.total_findings}[/]"
        f"  [dim]Patches:[/] [bold]{data.total_envelopes}[/]"
        f"  [dim]PRs:[/] [bold]{data.total_prs}[/]"
    )
    lines.append("")

    # -- Roadmap & Synthesis --
    if data.roadmap_snapshot_version > 0:
        lines.append(
            f"  [dim]Roadmap:[/] v{data.roadmap_snapshot_version}"
            f" ({data.roadmap_fragment_count} fragments)"
        )
    if data.synthesis_hypothesis_count > 0:
        age = ""
        if data.synthesis_last_run > 0:
            mins = int((time.time() - data.synthesis_last_run) / 60)
            if mins < 60:
                age = f" [dim]({mins}m ago)[/]"
            else:
                age = f" [dim]({mins // 60}h ago)[/]"
        lines.append(
            f"  [dim]Synthesis:[/] [bold cyan]{data.synthesis_hypothesis_count}[/] gaps{age}"
        )
    lines.append("")

    # -- Active sagas --
    if data.active_sagas:
        lines.append("  [bold yellow]Active Sagas[/]")
        for saga in data.active_sagas.values():
            pct = int(saga.completed_steps / max(saga.total_steps, 1) * 100)
            bar_filled = pct // 5
            bar_empty = 20 - bar_filled
            bar = f"[green]{'#' * bar_filled}[/][dim]{'.' * bar_empty}[/]"
            lines.append(
                f"    {bar} {pct}% [bold]{saga.title}[/]"
                f" [dim]({saga.completed_steps}/{saga.total_steps})[/]"
            )
        lines.append("")

    # -- Recent sagas --
    if data.recent_sagas:
        lines.append("  [bold]Recent Sagas[/]")
        for saga in reversed(data.recent_sagas):
            icon = "[green]OK[/]" if saga.phase == "complete" else "[red]X[/]"
            lines.append(f"    {icon} {saga.title} [dim]({saga.phase})[/]")
        lines.append("")

    # -- Recent findings --
    if data.recent_findings:
        lines.append("  [bold]Recent Findings[/]")
        cat_colors = {
            "dead_code": "red",
            "missing_capability": "yellow",
            "circular_dep": "magenta",
            "incomplete_wiring": "cyan",
            "manifesto_violation": "bold red",
        }
        for finding in list(data.recent_findings)[-5:]:
            cat_color = cat_colors.get(finding.category, "white")
            lines.append(f"    [{cat_color}]*[/] {finding.description}")
        lines.append("")

    # -- Last epoch summary --
    if data.last_epoch:
        e = data.last_epoch
        lines.append(
            f"  [dim]Last epoch: #{e.epoch_id} -- "
            f"{e.findings_count} findings, {e.envelopes_submitted} patches, "
            f"{e.duration_s:.1f}s[/]"
        )

    return "\n".join(lines)
