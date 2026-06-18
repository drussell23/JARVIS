"""Sovereign Lifecycle Daemon Supervisor for the reactor-core "Soul" (:8090).

Programmatic, in-process, resource-aware process supervisor — no external launchers (launchd/systemd),
no static plists. Manages the reactor-core control-plane (`run_reactor.py`) lifecycle natively so the
Trinity stays online for the life of the JARVIS control plane and tears down cleanly with it.

Phase 1 — Asynchronous subprocess daemonization
    `asyncio.create_subprocess_exec` launches `run_reactor.py --port 8090` in its own session
    (detached), pipes its stdout/stderr through an async drain into a size-rotated structured log
    (`logs/reactor_daemon.log`), keeping the primary terminal plane pristine.

Phase 2 — Signal-driven POSIX boundary & state control
    Registers SIGTERM/SIGHUP/SIGINT handlers on the running loop; on any of them it runs a coordinated
    graceful termination cascade — atomic teardown to the child (SIGTERM to the process group → the
    reactor closes its :8090 listener) → verify the port is released → SIGKILL fallback — so no orphaned
    background listener sockets survive a JARVIS shutdown/crash.

Phase 3 — Adaptive M1 resource profiling & niceness modulation
    A supervise loop wires to the existing `MemoryPressureGate`: under HIGH/CRITICAL host memory
    pressure (e.g. heavy 29k-file Oracle graph traversals) it `os.setpriority`-deprioritizes the
    background reactor so the main control plane keeps headroom, and restores full priority when
    pressure clears. Optional injectable "busy" signal (e.g. OperationalVelocityScore) composes in.

Fail-soft throughout; gated `JARVIS_REACTOR_DAEMON_ENABLED` for auto-start integration (the class
itself is invoked explicitly and is always importable).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["ReactorDaemonSupervisor", "daemon_enabled", "nice_for_level"]


def daemon_enabled() -> bool:
    """``JARVIS_REACTOR_DAEMON_ENABLED`` (default OFF) — gate for auto-start integration."""
    return os.environ.get("JARVIS_REACTOR_DAEMON_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Pressure level (str value) → nice increment. Higher nice = lower priority. Under memory pressure the
# background reactor yields CPU to the main control plane; at OK it runs full-throttle (nice 0).
_DEFAULT_NICE_MAP: Dict[str, int] = {"ok": 0, "warn": 5, "high": 10, "critical": 15}


def nice_for_level(level: Any, nice_map: Optional[Dict[str, int]] = None) -> int:
    """Map a PressureLevel (or its str value) to a nice value. Pure."""
    m = nice_map or _DEFAULT_NICE_MAP
    key = getattr(level, "value", level)
    return int(m.get(str(key).lower(), 0))


class ReactorDaemonSupervisor:
    """Native async lifecycle supervisor for the reactor-core Soul.

    Parameters
    ----------
    repo_path / port:
        reactor-core repo + the control-plane port (default 8090).
    log_dir / log_name / log_max_bytes / log_backups:
        size-rotated structured log target for the child's stdout/stderr.
    mem_gate:
        a ``MemoryPressureGate``-shaped object (``.pressure()`` → PressureLevel). ``None`` → lazily
        resolves the default gate. Injectable for tests.
    setpriority:
        ``os.setpriority``-shaped callable (injectable for tests).
    busy_signal:
        optional ``() -> bool`` (e.g. main-plane busy / negative velocity) that forces deprioritization
        even when memory is OK. Composes with memory pressure (strictest wins).
    """

    def __init__(
        self,
        *,
        repo_path: Path,
        port: int = 8090,
        log_dir: str = "logs",
        log_name: str = "reactor_daemon.log",
        log_max_bytes: int = 8 * 1024 * 1024,
        log_backups: int = 5,
        python_exe: Optional[str] = None,
        mem_gate: Any = None,
        nice_map: Optional[Dict[str, int]] = None,
        setpriority: Optional[Callable[[int, int, int], None]] = None,
        busy_signal: Optional[Callable[[], bool]] = None,
        supervise_interval_s: float = 5.0,
        health_timeout_s: float = 90.0,
    ) -> None:
        self._repo = Path(repo_path)
        self._port = int(port)
        self._log_path = Path(log_dir) / log_name
        self._log_max_bytes = log_max_bytes
        self._log_backups = log_backups
        self._python = python_exe or "python3"
        self._mem_gate = mem_gate
        self._nice_map = nice_map or dict(_DEFAULT_NICE_MAP)
        self._setpriority = setpriority or os.setpriority
        self._busy_signal = busy_signal
        self._interval = supervise_interval_s
        self._health_timeout = health_timeout_s

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._supervise_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._current_nice = 0
        self._file_logger: Optional[logging.Logger] = None

    # ------------------------------------------------------------------ logging (Phase 1)
    def _make_file_logger(self) -> logging.Logger:
        lg = logging.getLogger(f"reactor_daemon.{self._port}")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(
                str(self._log_path), maxBytes=self._log_max_bytes,
                backupCount=self._log_backups, encoding="utf-8",
            )
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(h)
        return lg

    async def _drain_logs(self) -> None:
        """Read the child's merged stdout line-by-line → rotated structured file (unbuffered)."""
        assert self._proc is not None and self._proc.stdout is not None
        flog = self._file_logger
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                if flog is not None:
                    flog.info(line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — log drain is best-effort
            logger.debug("[ReactorDaemon] log drain ended: %s", exc)

    # ------------------------------------------------------------------ health/port
    def _port_free(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", self._port)) != 0

    async def _await_health(self) -> bool:
        import urllib.request
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._health_timeout
        while loop.time() < deadline:
            try:
                def _probe() -> bool:
                    with urllib.request.urlopen(
                        f"http://localhost:{self._port}/health", timeout=2.0,
                    ) as r:
                        return r.status == 200
                if await asyncio.to_thread(_probe):
                    return True
            except Exception:  # noqa: BLE001
                pass
            if self._proc is not None and self._proc.returncode is not None:
                return False  # child died during boot
            await asyncio.sleep(2)
        return False

    # ------------------------------------------------------------------ Phase 1: start
    async def start(self) -> bool:
        """Launch run_reactor.py natively (detached, log-rotated). Returns True once /health is up."""
        if not self._port_free():
            logger.info("[ReactorDaemon] :%d already serving — adopting existing Soul (no spawn)", self._port)
            return await self._await_health()
        launcher = self._repo / "run_reactor.py"
        if not launcher.is_file():
            logger.error("[ReactorDaemon] launcher missing: %s", launcher)
            return False
        self._file_logger = self._make_file_logger()
        env = dict(os.environ)
        env["REACTOR_PORT"] = str(self._port)
        env["PYTHONUNBUFFERED"] = "1"
        self._proc = await asyncio.create_subprocess_exec(
            self._python, "run_reactor.py", "--port", str(self._port),
            cwd=str(self._repo), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,   # own process group → clean group teardown
        )
        self._drain_task = asyncio.ensure_future(self._drain_logs())
        logger.info("[ReactorDaemon] launched run_reactor.py pid=%d → %s", self._proc.pid, self._log_path)
        ok = await self._await_health()
        if not ok:
            logger.error("[ReactorDaemon] Soul did not become healthy within %.0fs", self._health_timeout)
        return ok

    # ------------------------------------------------------------------ Phase 2: signals + stop
    def install_signal_handlers(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        loop = loop or asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(self._on_signal(s)))
            except (NotImplementedError, RuntimeError, ValueError) as exc:
                logger.debug("[ReactorDaemon] signal %s not installable: %s", sig, exc)

    async def _on_signal(self, sig: int) -> None:
        logger.info("[ReactorDaemon] received signal %s → graceful teardown cascade", sig)
        await self.stop()

    async def stop(self, grace_s: float = 10.0) -> None:
        """Coordinated graceful termination: SIGTERM to the child's group (reactor closes its :8090
        listener) → verify port released → SIGKILL fallback. Idempotent + fail-soft."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        for t in (self._supervise_task, self._drain_task):
            if t is not None:
                t.cancel()
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)   # atomic group teardown
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_s)
        except asyncio.TimeoutError:
            logger.warning("[ReactorDaemon] grace expired → SIGKILL")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # Verify no orphaned listener remains.
        if self._port_free():
            logger.info("[ReactorDaemon] :%d released cleanly", self._port)
        else:
            logger.warning("[ReactorDaemon] :%d still bound after teardown (possible orphan)", self._port)

    # ------------------------------------------------------------------ Phase 3: adaptive nice
    def _gate(self) -> Any:
        if self._mem_gate is not None:
            return self._mem_gate
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import get_default_gate
            self._mem_gate = get_default_gate()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ReactorDaemon] memory gate unavailable: %s", exc)
        return self._mem_gate

    def _target_nice(self) -> int:
        """Strictest-wins of memory pressure + optional busy signal."""
        nice = 0
        gate = self._gate()
        if gate is not None:
            try:
                nice = nice_for_level(gate.pressure(), self._nice_map)
            except Exception:  # noqa: BLE001
                nice = 0
        if self._busy_signal is not None:
            try:
                if self._busy_signal():
                    nice = max(nice, self._nice_map.get("high", 10))
            except Exception:  # noqa: BLE001
                pass
        return nice

    def apply_adaptive_nice(self) -> Optional[int]:
        """Re-nice the child to match current pressure. Returns the nice applied (or None). Fail-soft."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return None
        target = self._target_nice()
        if target == self._current_nice:
            return self._current_nice
        try:
            self._setpriority(os.PRIO_PROCESS, proc.pid, target)
            logger.info("[ReactorDaemon] re-niced pid=%d %d → %d (memory-adaptive)",
                        proc.pid, self._current_nice, target)
            self._current_nice = target
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ReactorDaemon] setpriority failed: %s", exc)
        return self._current_nice

    async def _supervise_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                self.apply_adaptive_nice()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ReactorDaemon] supervise loop ended: %s", exc)

    # ------------------------------------------------------------------ orchestration
    async def run_forever(self) -> int:
        """Start + install signals + supervise until a teardown signal. Returns child exit code."""
        if not await self.start():
            return 1
        self.install_signal_handlers()
        self._supervise_task = asyncio.ensure_future(self._supervise_loop())
        try:
            if self._proc is not None:
                await self._proc.wait()
        finally:
            await self.stop()
        rc = self._proc.returncode if self._proc is not None else 0
        return int(rc) if rc is not None else 0
