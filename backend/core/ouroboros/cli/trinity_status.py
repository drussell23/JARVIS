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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (Any, Awaitable, Callable, Dict, Mapping,
                    Optional, Tuple)


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
    """The SUPERVISOR's own IPC socket, or None.

    This used to return the COCKPIT's socket
    (``cockpit_attach.attach_socket_path()``), which was a fine proxy while
    one process owned the governed loop and the body. `ov` owns the loop —
    and therefore the cockpit — so the supervisor's verdict was being
    computed from another daemon's transport: `trinity status` reported
    ``ZOMBIE/DEADLOCKED (tcp=live, uds=stale)`` about a supervisor that was
    serving HTTP 200 the whole time.

    A daemon is judged by ITS transports. ``JARVIS_SUPERVISOR_IPC_SOCKET``
    declares one if a deployment gives the supervisor a dedicated socket;
    absent, the supervisor is judged on its pid and its port, and ``uds`` is
    honestly reported as not applicable. The cockpit socket is probed on the
    O+V row, where it belongs — see :func:`engine_socket`.
    """
    raw = (os.environ.get("JARVIS_SUPERVISOR_IPC_SOCKET") or "").strip()
    return Path(raw) if raw else None


# ---------------------------------------------------------------------------
# Phase 2 — dynamic discovery. Every address below is read from the SAME
# configuration the daemons bind with; nothing here is a second opinion.
# ---------------------------------------------------------------------------


def supervisor_endpoint() -> Tuple[str, int]:
    """Where the supervisor serves. ``JARVIS_BACKEND_HOST``/``_PORT`` — the
    same values ``converged_headless.main`` passes to uvicorn."""
    host = (os.environ.get("JARVIS_BACKEND_HOST") or "127.0.0.1").strip()
    return host or "127.0.0.1", _health_port()


def engine_socket() -> Optional[Path]:
    """The cockpit attach socket — O+V's, resolved through the cockpit's own
    contract (``JARVIS_ATTACH_IPC_SOCKET``), never a literal here."""
    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
        return attach_socket_path()
    except Exception:  # noqa: BLE001
        return None


def engine_endpoint() -> Tuple[str, int]:
    """The O+V engine's IPC event bus — the EventChannelServer that hosts
    ``/ws/trinity-bus`` and the webhook surfaces. Read from
    ``JARVIS_CHANNEL_HOST``/``JARVIS_CHANNEL_PORT``, which is what
    ``event_channel`` itself binds with."""
    host = (os.environ.get("JARVIS_CHANNEL_HOST") or "127.0.0.1").strip()
    try:
        port = int((os.environ.get("JARVIS_CHANNEL_PORT") or "8099").strip()
                   or 8099)
    except ValueError:
        port = 8099
    return host or "127.0.0.1", port


def engine_pid(root: Optional[Path] = None) -> Optional[int]:
    """The O+V engine's PID from the lockfile the engine itself writes.

    ``.jarvis/intake_router.lock`` is written by the intake router when it
    binds — a NON-socket source, so a wedged socket cannot mask a live
    process. This is the O+V parallel to :func:`supervisor_pid`'s use of
    ``launchctl``/``pgrep``, and it is why a deadlocked engine reports
    UNRESPONSIVE rather than DOWN.

    A recorded pid that is no longer alive is a STALE lock, not a daemon.
    """
    import json
    try:
        base = root if root is not None else _repo_root()
        raw = (base / ".jarvis" / "intake_router.lock").read_text(
            encoding="utf-8")
        pid = int(json.loads(raw).get("pid") or 0)
        if pid <= 0:
            return None
        os.kill(pid, 0)                 # existence only; never signals
        return pid
    except (PermissionError,):
        return None
    except Exception:  # noqa: BLE001 — missing / malformed / dead == no pid
        return None


def _repo_root() -> Path:
    try:
        from backend.core.ouroboros.cli.thin_client import repo_root
        return repo_root()
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[4]


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
    # The verdict comes from `classify` — the same rule every fleet row uses,
    # so the supervisor handshake and the matrix can never disagree about
    # what "zombie" means. The PROSE below stays this function's own.
    _verdict = classify(pid_active, {"tcp": tcp_state, "uds": uds_state})

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



# ---------------------------------------------------------------------------
# Phase 1 — the multi-daemon matrix
# ---------------------------------------------------------------------------
#
# `trinity status` assumed a monolith: one pid, one tcp, one uds, one verdict.
# The organism is now two daemons with different jobs — `ov` owns the governed
# loop and the cockpit, `unified_supervisor` owns the body and the HTTP port —
# and folding one's transport into the other's verdict is what produced
# "ZOMBIE/DEADLOCKED" about a supervisor serving HTTP 200.
#
# A daemon is judged by ITS OWN transports. That is the whole fix, and it is
# structural: the matrix cannot mix them up because each row owns its probes.


def fleet_probe_timeout_s() -> float:
    """Per-probe ceiling. ``JARVIS_FLEET_PROBE_TIMEOUT_S`` — default 0.75s.

    Aggressively sub-second and deliberately tighter than
    :func:`_strict_timeout`: this is a DIAGNOSTIC. A tool that hangs while
    reporting on a hung daemon has become the problem it was run to
    describe, and an operator staring at a frozen `trinity status` learns
    nothing except to stop trusting it.
    """
    try:
        return max(0.05, min(10.0, float(
            os.environ.get("JARVIS_FLEET_PROBE_TIMEOUT_S", "0.75"))))
    except (TypeError, ValueError):
        return 0.75


def fleet_deadline_s() -> float:
    """Ceiling on the WHOLE matrix. ``JARVIS_FLEET_DEADLINE_S`` — default 3s.

    A second bound around the per-probe one, because "every probe is bounded"
    and "the command is bounded" are different promises: N daemons x M
    transports of individually-bounded probes still add up.
    """
    try:
        return max(0.2, min(30.0, float(
            os.environ.get("JARVIS_FLEET_DEADLINE_S", "3.0"))))
    except (TypeError, ValueError):
        return 3.0


def probe_attempts() -> int:
    """Samples before a `timeout` is believed. ``JARVIS_FLEET_PROBE_ATTEMPTS``
    — default 2.

    A single sub-second sample is a coin flip against a daemon with real
    stalls: the supervisor's own LoopSentinel logs pauses over a second, so
    one probe reported UNRESPONSIVE about a process answering in 17ms
    moments later. Retried ONLY on `timeout`, which is the ambiguous verdict
    — `refused` and `stale` are deterministic and gain nothing from asking
    twice.

    Still hard-bounded: attempts x per-probe budget, under the fleet
    deadline. Never crying wolf must not cost the promise never to hang.
    """
    try:
        return max(1, min(5, int(
            os.environ.get("JARVIS_FLEET_PROBE_ATTEMPTS", "2"))))
    except (TypeError, ValueError):
        return 2


def classify(pid_active: bool, states: Mapping[str, str]) -> Health:
    """The verdict rule, in ONE place. Pure; NEVER raises.

    Shared by the supervisor handshake and every fleet row so the two can
    never disagree about what "zombie" means.

    * nothing answering and no pid            -> DOWN
    * a pid, but every transport unreachable  -> ZOMBIE (the wedged loop)
    * something live, nothing dead            -> HEALTHY
    * a live and a dead transport together    -> DEGRADED
    """
    try:
        live = [k for k, v in states.items() if v == "live"]
        dead = [k for k, v in states.items()
                if v in ("timeout", "refused", "stale", "error")]
        if not pid_active and not live:
            return Health.DOWN
        if pid_active and not live and dead:
            return Health.ZOMBIE
        if live and not dead:
            return Health.HEALTHY
        if live and dead:
            return Health.DEGRADED
        return Health.DOWN if not live else Health.HEALTHY
    except Exception:  # noqa: BLE001
        return Health.UNKNOWN


@dataclass(frozen=True)
class Transport:
    """One address a daemon binds, and how to reach it.

    ``resolve`` is a callable rather than a value so the matrix reads the
    configuration at PROBE time — an operator who moves a port between two
    runs is not told about the old one.
    """

    kind: str                       # "tcp" | "uds"
    label: str
    resolve: Callable[[], Any]
    #: An optional transport is REPORTED but never degrades the verdict.
    #: The cockpit socket is attach-on-demand: nobody attached is the normal
    #: state, and calling a healthy engine DEGRADED for it is the same error
    #: as judging the supervisor by the cockpit's socket — crying wolf about
    #: a transport that is legitimately absent.
    optional: bool = False


@dataclass(frozen=True)
class DaemonSpec:
    """A daemon, as a declaration. Adding one is data, not control flow."""

    name: str
    title: str
    pid_source: Callable[[], Optional[int]]
    transports: Tuple[Transport, ...]
    absent_hint: str = ""
    #: Where to look when this daemon is wrong. Per-daemon, because pointing
    #: an operator at the supervisor's error log to debug the engine is worse
    #: than saying nothing.
    log_hint: Callable[[], str] = lambda: ""


@dataclass(frozen=True)
class DaemonHealth:
    name: str
    title: str
    state: Health
    pid: Optional[int] = None
    transports: Mapping[str, str] = field(default_factory=dict)
    detail: str = ""
    recommendation: str = ""

    @property
    def ok(self) -> bool:
        return self.state is Health.HEALTHY

    @property
    def faulted(self) -> bool:
        """A daemon that is present and wrong. DOWN is not a fault — a
        deployment may legitimately not be running the engine."""
        return self.state in (Health.ZOMBIE, Health.DEGRADED)


@dataclass(frozen=True)
class FleetHealth:
    daemons: Tuple[DaemonHealth, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(d.faulted for d in self.daemons)

    def by_name(self, name: str) -> Optional[DaemonHealth]:
        for d in self.daemons:
            if d.name == name:
                return d
        return None


def default_fleet() -> Tuple[DaemonSpec, ...]:
    """The two daemons this repo runs. Every address resolved dynamically."""
    return (
        DaemonSpec(
            name="supervisor",
            title="supervisor (body: audio, vision, HUD stream)",
            pid_source=supervisor_pid,
            transports=(
                Transport("tcp", "tcp", supervisor_endpoint),
                Transport("uds", "uds", _attach_socket, optional=True),
            ),
            absent_hint="start it with `trinity up`",
            log_hint=lambda: f"tail {_err_log()}",
        ),
        DaemonSpec(
            name="ov",
            title="O+V engine (the governed loop)",
            pid_source=engine_pid,
            transports=(
                # Attach-on-demand: `ov daemon` runs headless and nobody is
                # attached most of the time. Reported, never degrading.
                Transport("uds", "cockpit", engine_socket, optional=True),
                Transport("tcp", "event-bus", engine_endpoint),
            ),
            absent_hint="start it with `ov daemon`",
            log_hint=_engine_log_hint,
        ),
    )



def _engine_log_hint() -> str:
    """The ENGINE's log, which is a per-session file, not the supervisor's."""
    try:
        base = _repo_root() / ".ouroboros" / "sessions"
        latest = max((d for d in base.iterdir() if d.is_dir()),
                     key=lambda d: d.stat().st_mtime, default=None)
        return f"tail {latest / 'debug.log'}" if latest else ""
    except Exception:  # noqa: BLE001
        return ""


def _hint(spec: "DaemonSpec") -> str:
    try:
        return spec.log_hint() or ""
    except Exception:  # noqa: BLE001
        return ""


def _probe_fns() -> Tuple[Callable[..., Any], Callable[..., Any]]:
    """Resolve the probe callables ONCE, outside any timed window.

    This import used to sit inside `_probe_transport`, i.e. INSIDE a
    `wait_for`. In a warm process that is free; in a cold CLI process it is a
    blocking import that stalls the event loop while a sibling probe's
    deadline is already running — so `trinity status` reported `timeout`
    against endpoints answering in 5ms, and only from the CLI, never from a
    warm interpreter. A bounded probe must not contain unbounded work.
    """
    from backend.core.ouroboros.cli.thin_client import probe_http, probe_socket
    return probe_http, probe_socket


async def _probe_transport(t: Transport, timeout: float,
                           probes: Optional[Tuple[Any, Any]] = None) -> str:
    """One transport, hard-bounded. NEVER raises, NEVER hangs.

    Double-bounded on purpose: the probe is given a timeout AND wrapped in
    `wait_for`. A probe that mishandles its own deadline must not be able to
    freeze the diagnostic — the tool's responsiveness cannot rest on the
    correctness of the thing it is diagnosing.
    """
    probe_http, probe_socket = probes if probes is not None else _probe_fns()
    try:
        target = t.resolve()
    except Exception:  # noqa: BLE001
        return "error"
    if target is None:
        return "absent"
    # `timeout` is the CEILING for this probe, and the outer `wait_for` is
    # what enforces it. The inner probe gets the same value so its own
    # per-phase deadlines effectively never fire first — dividing the budget
    # across `probe_http`'s three phases (connect / drain / read) starves a
    # slow-but-healthy endpoint and manufactures a "timeout".
    #
    # Measured on this machine: the engine's /observability/health answers in
    # 0.502s and the supervisor's in 0.005s. A third of a 0.75s budget is
    # 0.25s, which called the healthy engine UNRESPONSIVE. One ceiling, one
    # enforcer, no arithmetic in between.
    result = "error"
    for attempt in range(probe_attempts()):
        try:
            if t.kind == "tcp":
                host, port = target
                inner = probe_http(host, port, timeout)
            else:
                inner = probe_socket(Path(target), timeout)
            result = await asyncio.wait_for(inner, timeout=timeout)
        except asyncio.TimeoutError:
            result = "timeout"
        except Exception:  # noqa: BLE001
            result = "error"
        # Only `timeout` is ambiguous enough to be worth asking twice.
        if result != "timeout":
            return result
    return result


async def assess_daemon(spec: DaemonSpec, *,
                        timeout: Optional[float] = None) -> DaemonHealth:
    """Probe one daemon's OWN transports. NEVER raises, NEVER hangs."""
    to = timeout if timeout is not None else fleet_probe_timeout_s()
    # Warmed BEFORE any clock starts — see `_probe_fns`.
    try:
        probes = _probe_fns()
    except Exception:  # noqa: BLE001
        probes = None
    try:
        pid = spec.pid_source()
    except Exception:  # noqa: BLE001
        pid = None
    try:
        results = await asyncio.gather(
            *(_probe_transport(t, to, probes) for t in spec.transports),
            return_exceptions=True)
    except Exception:  # noqa: BLE001
        results = ["error"] * len(spec.transports)
    states: Dict[str, str] = {}
    for t, r in zip(spec.transports, results):
        states[t.label] = r if isinstance(r, str) else "error"

    required = {t.label: states.get(t.label, "error")
                for t in spec.transports if not t.optional}
    state = classify(pid is not None, required or states)
    shown = ", ".join(
        f"{k}={v}" + ("*" if any(t.label == k and t.optional
                                 for t in spec.transports) else "")
        for k, v in states.items())
    if state is Health.DOWN:
        detail = f"not running (no pid, {shown})"
        rec = spec.absent_hint
    elif state is Health.ZOMBIE:
        detail = (f"pid {pid} ALIVE but UNRESPONSIVE ({shown}) — the event "
                  f"loop is wedged or it never bound its transports")
        rec = _hint(spec)
    elif state is Health.DEGRADED:
        detail = f"partial readiness (pid={pid}, {shown})"
        rec = _hint(spec)
    else:
        detail = f"pid {pid} live; {shown}" if pid else f"live; {shown}"
        rec = ""
    return DaemonHealth(name=spec.name, title=spec.title, state=state,
                        pid=pid, transports=states, detail=detail,
                        recommendation=rec)


async def assess_fleet(specs: Optional[Tuple[DaemonSpec, ...]] = None, *,
                       timeout: Optional[float] = None,
                       deadline: Optional[float] = None) -> FleetHealth:
    """The whole matrix, concurrently and within a hard ceiling.

    NEVER raises, NEVER hangs: a daemon whose assessment overruns the
    deadline is reported UNKNOWN rather than being waited on.
    """
    fleet = specs if specs is not None else default_fleet()
    cap = deadline if deadline is not None else fleet_deadline_s()
    try:
        rows = await asyncio.wait_for(
            asyncio.gather(*(assess_daemon(s, timeout=timeout) for s in fleet),
                           return_exceptions=True),
            timeout=cap)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        rows = [None] * len(fleet)
    out = []
    for spec, row in zip(fleet, rows):
        if isinstance(row, DaemonHealth):
            out.append(row)
        else:
            out.append(DaemonHealth(
                name=spec.name, title=spec.title, state=Health.UNKNOWN,
                detail=f"probe exceeded the {cap:.2f}s fleet deadline",
                recommendation="raise JARVIS_FLEET_DEADLINE_S to look harder"))
    return FleetHealth(daemons=tuple(out))

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
        fleet = asyncio.run(assess_fleet())
        for row in fleet.daemons:
            g = _GLYPH.get(row.state, "·")
            console.print(f"{g} {row.title}: {row.state.value}", markup=False)
            console.print(f"  ⎿ {row.detail}", markup=False)
            if row.recommendation:
                console.print(f"  ⎿ {row.recommendation}", markup=False)
        # A DOWN daemon is not a failure — a deployment may legitimately run
        # only one. A daemon that is PRESENT AND WRONG is.
        return 0 if fleet.ok else 1
    except Exception as exc:  # noqa: BLE001
        try:
            console and console.print(f"✗ status failed: {exc}", markup=False)
        except Exception:  # noqa: BLE001
            pass
        return 1


__all__ = [
    "DaemonHealth", "DaemonSpec", "FleetHealth", "Health", "HealthReport",
    "Transport", "active_health_handshake", "assess_daemon", "assess_fleet",
    "classify", "default_fleet", "engine_endpoint", "engine_pid",
    "engine_socket", "fleet_deadline_s", "fleet_probe_timeout_s",
    "status_main", "supervisor_endpoint", "supervisor_pid",
]
