"""Sovereign Telemetry Parse — the pure, importable parse layer.

Extracted verbatim from ``scripts/telemetry_harvester.py`` (Slice 256) so
that BOTH the operator-facing harvester CLI *and* the Live-Fire Graduation
Soak substrate share ONE parse — zero duplication, zero drift.

This module owns only the **pure parse**:

  * :class:`Metrics` — the Metric A/B/C signal extracted from a session's
    ``debug.log`` + ``summary.json``.
  * :func:`parse_metrics` — pure function, NEVER raises, no side effects.

The harvester keeps its own concerns (``certify`` verdict, ``render_report``,
the async session watcher, the CLI) and re-imports :class:`Metrics` +
:func:`parse_metrics` from here.

## Authority posture (locked)

  * **Pure + stdlib-only** — ``re`` + ``dataclasses`` + ``typing`` at top
    level. No logger, no I/O, no subprocess, no network. The soak substrate
    *consults*; this module never observes state.
  * **NEVER raises** — :func:`parse_metrics` returns a default
    :class:`Metrics` on any malformed input.
  * Grounded regexes — every pattern is matched against real emitted
    ``debug.log`` strings (verified against the deployed LiveKernelValidator
    wiring).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── grounded log patterns (verified against real debug.log + deployed wiring)
_RE_PHASE = re.compile(r"phase=[A-Z_]+")
_RE_LIVEFIRE_FAIL = re.compile(
    r"\[LiveFire\] candidate FAILED live-fire boot:\s*(\S+)",
)
_RE_FAILCLASS_BUILD = re.compile(r"failure_class[=:][\"']?build\b")
_RE_RETRY = re.compile(r"GENERATE_RETRY|VALIDATE_RETRY")
_RE_RECOVERED = re.compile(r"state=applied|state=complete|phase=COMPLETE")
_RE_GATE_INERT = re.compile(r"GATE INERT")
_RE_TIMEOUT = re.compile(r"LiveFireTimeout|live-fire exceeded")
# Genuine memory-cap FIRE only. Slice 257 — the previous pattern matched bare
# ``process_memory_cap`` / ``OOM``, which also matched the ProcessMemoryWatchdog
# *arming* line ("armed: warn=…MB cap=…MB — graceful stop_reason=
# process_memory_cap before OS OOM-kill"). That line merely DESCRIBES the cap;
# arming is not firing. bt-2026-06-16-063106 completed cleanly at rss=555MB
# (cap 12288MB) via wall_clock_cap yet was mis-verdicted ANOMALY/OOM. The
# watchdog logs "Session X stopping: process_memory_cap" only when it actually
# trips, and stamps summary.stop_reason=process_memory_cap — both authoritative.
_RE_OOM = re.compile(
    r"stopping: process_memory_cap|MemoryError|emergency_brake|"
    r"memory_pressure_changed.*(critical|emergency)",
)

# Session outcomes the harness writes when the FSM finalized a summary.
_TERMINAL_OUTCOMES = {"complete", "incomplete_kill"}


@dataclass
class Metrics:
    """Parsed Metric A/B/C signal for one session. Duck-typed by the
    Graduation Arbiter (it reads ``.recovered`` / ``.oom`` / etc.)."""

    # A — deployment integrity
    booted: bool = False
    boot_check_passed: int = 0           # from optional deployer stdout
    boot_check_failed: bool = False
    # B — live-fire trajectory
    livefire_fired: List[str] = field(default_factory=list)  # exc types caught
    routed_build: bool = False
    retried: bool = False
    recovered: bool = False
    # C — hardware/state
    gate_inert: bool = False
    livefire_timeout: bool = False
    oom: bool = False
    # termination
    session_outcome: str = ""
    stop_reason: str = ""
    cost_total: Optional[float] = None
    duration_s: Optional[float] = None


def parse_metrics(
    log_text: str,
    summary: Optional[Dict],
    deployer_stdout: str = "",
) -> Metrics:
    """Pure parse — grounded in real emitted strings. No side effects.

    NEVER raises: malformed/absent inputs yield a default-valued
    :class:`Metrics`."""
    m = Metrics()
    log_text = log_text if isinstance(log_text, str) else ""
    deployer_stdout = (
        deployer_stdout if isinstance(deployer_stdout, str) else ""
    )

    m.booted = bool(_RE_PHASE.search(log_text))
    m.boot_check_passed = deployer_stdout.count("BOOT CHECK PASSED")
    m.boot_check_failed = "BOOT CHECK FAILED" in deployer_stdout

    m.livefire_fired = _RE_LIVEFIRE_FAIL.findall(log_text)
    m.routed_build = bool(_RE_FAILCLASS_BUILD.search(log_text))
    m.retried = bool(_RE_RETRY.search(log_text))
    m.recovered = bool(_RE_RECOVERED.search(log_text))

    m.gate_inert = bool(_RE_GATE_INERT.search(log_text))
    m.livefire_timeout = bool(_RE_TIMEOUT.search(log_text))
    m.oom = bool(_RE_OOM.search(log_text))

    if isinstance(summary, dict):
        m.session_outcome = str(summary.get("session_outcome", ""))
        m.stop_reason = str(summary.get("stop_reason", ""))
        m.cost_total = summary.get("cost_total")
        m.duration_s = summary.get("duration_s")
    # Authoritative memory-cap signal: when the ProcessMemoryWatchdog actually
    # fires it produces a graceful summary with stop_reason=process_memory_cap.
    # (The log-regex above catches the same fire + MemoryError / pressure.)
    m.oom = m.oom or m.stop_reason == "process_memory_cap"
    return m


__all__ = [
    "Metrics",
    "parse_metrics",
]
