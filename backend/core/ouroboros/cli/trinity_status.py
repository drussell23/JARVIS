"""``trinity status`` — the Active IPC Health Handshake.

Operator authorization 2026-07-19 (Phase 7). A ``launchd`` daemon can be
a ZOMBIE: the PID is alive and ``launchctl`` reports it running, but the
Python event loop is deadlocked or never bound its multiplexers. PID
existence alone (``psutil.pid_exists`` / ``launchctl list``) CANNOT see
that — so this probes the network stack directly to prove operational
readiness.

Mandate 1 — Root-Cause: liveness is proven by a REAL connect to the TCP
port + the UDS attach socket, not by a PID check.

Mandate 2 — edge cases:
  * **Active IPC Handshake** — verify the PID, then non-blocking async
    ping BOTH the TCP port (8010) and the UDS attach socket.
  * **Zombie Detection** — a probe that times out (>2.0s) or is refused
    WHILE the PID is still registered ⇒ ``ZOMBIE/DEADLOCKED``, with a
    recommendation to tail the daemon error log.
  * **Graceful Client Fallback** — every probe opens, confirms, and
    immediately closes the transport (``probe_socket``/``probe_tcp`` are
    non-invasive) — no dangling connection is left on the multiplexer.

Mandate 3 — DRY: reuses ``thin_client.probe_socket`` (now with a strict
timeout) + its new ``probe_tcp`` companion, and the installer's
``SUPERVISOR_LABEL``.

Every public entry point NEVER raises.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


class Health(str, Enum):
    HEALTHY = "HEALTHY"
    ZOMBIE = "ZOMBIE/DEADLOCKED"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


@dataclass
class HealthReport:
    state: Health
    pid: Optional[int] = None
    tcp_state: str = ""
    uds_state: str = ""
    detail: str = ""
    recommendation: str = ""

    @property
    def ok(self) -> bool:
        return self.state is Health.HEALTHY


def _strict_timeout() -> float:
    try:
        return max(0.2, float(os.environ.get("JARVIS_HEALTH_PROBE_TIMEOUT_S",
                                              "2.0")))
    except (TypeError, ValueError):
        return 2.0


def _health_port() -> int:
    try:
        p = int(os.environ.get("JARVIS_BACKEND_PORT", "0") or "0")
        return p if p > 0 else 8010
    except ValueError:
        return 8010


def _attach_socket() -> Optional[Path]:
    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
        return attach_socket_path()
    except Exception:  # noqa: BLE001
        return None


def _err_log() -> Path:
    try:
        from backend.core.ouroboros.cli.thin_client import repo_root
        base = repo_root()
    except Exception:  # noqa: BLE001
        base = Path(__file__).resolve().parents[4]
    return base / ".jarvis" / "logs" / "supervisor.err.log"


def supervisor_pid(runner: Callable[..., Any] = subprocess.run) -> Optional[int]:
    """The resident supervisor's PID from a NON-socket source (so a dead
    socket can't mask a live-but-deadlocked process): ``launchctl`` for
    the agent label first, then ``pgrep``. None when nothing is
    registered. NEVER raises."""
    try:
        from backend.core.ouroboros.cli.trinity_installer import (
            SUPERVISOR_LABEL,
        )
    except Exception:  # noqa: BLE001
        SUPERVISOR_LABEL = "com.jarvis.supervisor"
    try:
        r = runner(["launchctl", "list", SUPERVISOR_LABEL],
                   capture_output=True, text=True, timeout=5)
        out = getattr(r, "stdout", "") or ""
        m = re.search(r'"PID"\s*=\s*(\d+)', out)
        if m:
            return int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    # pgrep fallback — a supervisor started outside launchd still counts.
    try:
        import shutil
        if shutil.which("pgrep"):
            r = runner(["pgrep", "-f", "unified_supervisor.py"],
                       capture_output=True, text=True, timeout=5)
            pids = [int(x) for x in (getattr(r, "stdout", "") or "").split()
                    if x.strip().isdigit()]
            if pids:
                return pids[0]
    except Exception:  # noqa: BLE001
        pass
    return None


async def _safe(coro: Awaitable[str]) -> str:
    """Run a probe, mapping a RAISED timeout/refusal to a state string so
    a strict/mocked probe that raises still synthesizes correctly."""
    try:
        return await coro
    except (asyncio.TimeoutError, TimeoutError):
        return "timeout"
    except (ConnectionRefusedError, ConnectionResetError):
        return "refused"
    except OSError:
        return "refused"
    except Exception:  # noqa: BLE001
        return "error"


async def active_health_handshake(
    *,
    port: Optional[int] = None,
    socket_path: Optional[Path] = None,
    timeout: Optional[float] = None,
    pid_fn: Optional[Callable[[], Optional[int]]] = None,
    tcp_probe: Optional[Callable[..., Awaitable[str]]] = None,
    socket_probe: Optional[Callable[..., Awaitable[str]]] = None,
) -> HealthReport:
    """Probe the daemon's real readiness and synthesize a health verdict.
    NEVER raises."""
    from backend.core.ouroboros.cli.thin_client import probe_socket, probe_http
    prt = port if port is not None else _health_port()
    sock = socket_path if socket_path is not None else _attach_socket()
    to = timeout if timeout is not None else _strict_timeout()
    pidf = pid_fn or supervisor_pid
    # APP-level HTTP probe (not bare connect) so a kernel-accepted but
    # event-loop-wedged daemon is caught as a zombie, not passed as live.
    tcpf = tcp_probe or probe_http
    udsf = socket_probe or probe_socket

    pid = None
    try:
        pid = pidf()
    except Exception:  # noqa: BLE001
        pid = None

    # Probe both transports concurrently, strictly bounded + non-invasive.
    tcp_state = await _safe(tcpf("127.0.0.1", prt, to))
    uds_state = "absent"
    if sock is not None:
        uds_state = await _safe(udsf(sock, to))

    pid_active = pid is not None
    tcp_live = tcp_state == "live"
    uds_live = uds_state == "live"
    tcp_dead = tcp_state in ("timeout", "refused", "error")
    uds_dead = uds_state in ("timeout", "refused", "stale", "error")

    rec = ""
    if not pid_active and not tcp_live and not uds_live:
        state = Health.DOWN
        detail = "no supervisor PID registered and no transport answering"
        rec = "start it with `trinity install` (or `trinity up`)"
    elif pid_active and (tcp_dead or uds_dead):
        # PID alive but a transport is deadlocked/refused → the classic
        # zombie: launchctl says "running", the event loop is wedged.
        state = Health.ZOMBIE
        detail = (f"pid {pid} ALIVE but transports unreachable "
                  f"(tcp={tcp_state}, uds={uds_state}) — the event loop is "
                  "deadlocked or never bound its multiplexers")
        rec = f"tail the daemon error log: tail -f {_err_log()}"
    elif tcp_live and (uds_live or uds_state == "absent"):
        state = Health.HEALTHY
        detail = (f"pid {pid} live; tcp:{prt}=live"
                  + (f", uds=live" if uds_live else ", uds=absent (cockpit "
                     "not attached — fine)"))
    else:
        state = Health.DEGRADED
        detail = (f"partial readiness (pid={pid}, tcp={tcp_state}, "
                  f"uds={uds_state})")
        rec = f"inspect {_err_log()}"

    return HealthReport(state=state, pid=pid, tcp_state=tcp_state,
                        uds_state=uds_state, detail=detail, recommendation=rec)


_GLYPH = {
    Health.HEALTHY: "⏺", Health.ZOMBIE: "✗", Health.DOWN: "○",
    Health.DEGRADED: "▲", Health.UNKNOWN: "·",
}


def status_main(console=None) -> int:
    """Entry for ``trinity status`` (Active IPC Health Handshake). Returns
    0 only when HEALTHY. NEVER raises."""
    try:
        if console is None:
            from backend.core.ouroboros.ui.theme import build_console
            console = build_console()
        report = asyncio.run(active_health_handshake())
        g = _GLYPH.get(report.state, "·")
        console.print(f"{g} supervisor: {report.state.value}", markup=False)
        console.print(f"  ⎿ {report.detail}", markup=False)
        if report.recommendation:
            console.print(f"  ⎿ {report.recommendation}", markup=False)
        return 0 if report.ok else 1
    except Exception as exc:  # noqa: BLE001
        try:
            console and console.print(f"✗ status failed: {exc}", markup=False)
        except Exception:  # noqa: BLE001
            pass
        return 1


__all__ = [
    "Health", "HealthReport", "supervisor_pid", "active_health_handshake",
    "status_main",
]
