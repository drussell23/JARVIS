"""Thin-Client Split — bare ``ov`` becomes a sub-second presentation shell.

Operator authorization 2026-07-18. The root cause of slow ``ov`` was
architectural: the entry process imported the ENTIRE organism (the
battle-test bootstrap chain) before painting anything. This module is
the bifurcation: the presentation plane and the domain plane are now
separate EXECUTION BOUNDARIES, not lazily-interleaved imports.

Strict import isolation (mandate 2, AST-pinned by the test spine):
this module — and ``cli/ov.py`` — may import ONLY the standard
library, the TUI layer (``backend.core.ouroboros.ui.*``), and the IPC
client (``battle_test.cockpit_attach``). Zero ML, zero embeddings,
zero governance/domain modules. The organism lives in a DETACHED
process spawned via ``subprocess.Popen``.

Flow for bare ``ov``::

    crest (instant) → zero-trust socket probe
      live   → attach (sub-second warm path)
      stale  → unlink ghost socket → cold boot
      absent → cold boot
    cold boot: Popen detached daemon → waking breadcrumb →
               attach the moment the bridge binds → same split-plane
               TUI; boot lines arrive through the SAME PresentationRouter
               chokepoint the warm path uses (DRY — one conformance).

Zero-Trust probe (mandate 2): the socket FILE is never trusted — a
violently-killed daemon leaves a ghost inode that connects refuse. The
probe is a bounded real connection attempt; refusal classifies the
socket as STALE and the client unlinks it before cold-booting. No
traceback ever reaches the operator.

Resident Organism (``ov daemon --install``): generates a per-user
macOS launchd Agent plist (POSIX paths, no hardcoded machine state —
interpreter, repo root, and log paths resolved at install time),
piping raw stderr/stdout to dedicated logs while the IPC socket serves
thin clients. ``--uninstall`` reverses it.
"""
from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

_TRUTHY = ("1", "true", "yes", "on")

#: launchd label — stable identity for the resident agent.
AGENT_LABEL = "com.jarvis.ov.daemon"


def thin_client_enabled() -> bool:
    """Master gate — default ON (the split IS the product now);
    ``JARVIS_OV_THIN_CLIENT=false`` or ``ov --legacy-boot`` reverts to
    the in-process organism boot. NEVER raises."""
    return os.environ.get(
        "JARVIS_OV_THIN_CLIENT", "1",
    ).strip().lower() in _TRUTHY


def repo_root() -> Path:
    """The checkout root (…/JARVIS-AI-Agent*), resolved structurally
    from this file — never from cwd."""
    return Path(__file__).resolve().parents[4]


def _probe_timeout_s() -> float:
    try:
        raw = float(os.environ.get("JARVIS_OV_PROBE_TIMEOUT_S", "0.4"))
    except (TypeError, ValueError):
        raw = 0.4
    return max(0.05, min(3.0, raw))


def _boot_wait_s() -> float:
    try:
        raw = float(os.environ.get("JARVIS_OV_BOOT_WAIT_S", "120"))
    except (TypeError, ValueError):
        raw = 120.0
    return max(5.0, min(900.0, raw))


# ---------------------------------------------------------------------------
# Zero-Trust socket probe
# ---------------------------------------------------------------------------


async def probe_socket(path: Path, timeout: Optional[float] = None) -> str:
    """Classify the attach socket with a REAL bounded connect:

    * ``"live"``   — something accepted (a daemon is home)
    * ``"stale"``  — inode exists but connection refused/timed out
      (ghost of a violently-killed daemon)
    * ``"absent"`` — no inode at all

    ``timeout`` overrides the env default for a STRICT live-validation
    bound (Phase 7 health handshake). Non-invasive: opens, confirms, and
    immediately closes the transport — no lingering connection on the
    daemon's multiplexer. NEVER raises; NEVER hangs."""
    try:
        if not path.exists():
            return "absent"
        try:
            _r, w = await asyncio.wait_for(
                asyncio.open_unix_connection(path=str(path)),
                timeout=timeout if timeout is not None else _probe_timeout_s(),
            )
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
            return "live"
        except (
            ConnectionRefusedError, ConnectionResetError,
            asyncio.TimeoutError, OSError,
        ):
            return "stale"
    except Exception:  # noqa: BLE001
        return "stale" if path.exists() else "absent"


async def probe_tcp(host: str, port: int, timeout: float = 2.0) -> str:
    """TCP companion to ``probe_socket`` — a REAL bounded, non-invasive
    connect to prove a listener's event loop is live:

    * ``"live"``    — the connection was accepted
    * ``"refused"`` — ConnectionRefusedError (nothing/dead bound the port)
    * ``"timeout"`` — no response within ``timeout`` (deadlocked loop)

    Opens, confirms, immediately closes — leaves no dangling connection.
    NEVER raises."""
    try:
        try:
            _r, w = await asyncio.wait_for(
                asyncio.open_connection(host=host, port=port),
                timeout=timeout,
            )
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
            return "live"
        except asyncio.TimeoutError:
            return "timeout"
        except (ConnectionRefusedError, ConnectionResetError):
            return "refused"
        except OSError:
            return "refused"
    except Exception:  # noqa: BLE001
        return "refused"


async def probe_http(
    host: str, port: int, timeout: float = 2.0,
    path: str = "/observability/health",
) -> str:
    """APPLICATION-level liveness probe. A bare TCP connect proves only
    that the KERNEL accepted the socket — a deadlocked event loop still
    completes the handshake at the kernel layer. So this sends a real
    HTTP request and awaits a response within ``timeout``:

    * ``"live"``    — an HTTP response came back (the loop is processing)
    * ``"timeout"`` — connected, but the app never answered (ZOMBIE: the
      event loop is wedged / never bound its handlers)
    * ``"refused"`` — nothing/dead bound the port

    Non-invasive: sends ``Connection: close`` and closes the transport —
    no lingering connection on the server. NEVER raises."""
    writer = None
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host=host, port=port), timeout=timeout)
        except asyncio.TimeoutError:
            return "timeout"
        except (ConnectionRefusedError, ConnectionResetError, OSError):
            return "refused"
        try:
            req = (f"GET {path} HTTP/1.0\r\nHost: {host}\r\n"
                   "Connection: close\r\n\r\n")
            writer.write(req.encode())
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            data = await asyncio.wait_for(reader.read(64), timeout=timeout)
            # Any bytes back = the app's event loop answered → live. Empty
            # read = connected but the loop produced nothing → wedged.
            return "live" if data else "timeout"
        except asyncio.TimeoutError:
            return "timeout"          # accepted but no app response → ZOMBIE
        except (ConnectionResetError, OSError):
            return "refused"
    except Exception:  # noqa: BLE001
        return "refused"
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


def clean_stale_socket(path: Path) -> bool:
    """Unlink a ghost socket. True when the path is gone afterwards.
    NEVER raises."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        return not path.exists()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Detached cold boot
# ---------------------------------------------------------------------------


def daemon_argv() -> list:
    """The detached organism's argv — the SAME bootstrap the operator
    would run by hand (one source of truth for flags)."""
    return [
        sys.executable,
        str(repo_root() / "scripts" / "ouroboros_battle_test.py"),
        "--headless",
    ]


def daemon_log_path() -> Path:
    return repo_root() / ".jarvis" / "logs" / "ov-daemon.log"


def _log_max_bytes() -> int:
    try:
        return max(1024, int(os.environ.get(
            "JARVIS_OV_DAEMON_LOG_MAX_BYTES", str(50 * 1024 * 1024),
        )))
    except (TypeError, ValueError):
        return 50 * 1024 * 1024


def _log_backups() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_OV_DAEMON_LOG_BACKUPS", "3",
        )))
    except (TypeError, ValueError):
        return 3


def rollover_daemon_log(path: Optional[Path] = None) -> bool:
    """Circadian Log Janitor for the RAW daemon sinks (the streams
    launchd / Popen capture BELOW the logging layer). Uses the
    standard ``RotatingFileHandler`` machinery (``shouldRollover`` /
    ``doRollover`` — never manual byte manipulation) at each
    spawn/boot boundary: oversized logs shift to ``.1``/``.2``/``.3``
    and the oldest falls off. Caveat, stated honestly: a stream fd
    HELD OPEN by launchd keeps writing to the rotated inode until the
    next daemon start — the bound is therefore max_bytes × (backups+2)
    per sink, never unbounded. NEVER raises."""
    import logging as _logging
    import logging.handlers as _lh
    target = path or daemon_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = _lh.RotatingFileHandler(
            str(target),
            maxBytes=_log_max_bytes(),
            backupCount=_log_backups(),
            encoding="utf-8",
            delay=True,                    # never touches a healthy file
        )
        probe = _logging.LogRecord(
            "ov.janitor", _logging.INFO, __file__, 0, "", (), None,
        )
        try:
            if handler.shouldRollover(probe):
                handler.doRollover()
                return True
            return False
        finally:
            handler.close()
    except Exception:  # noqa: BLE001
        return False


def spawn_daemon(
    *, spawner: Callable[..., Any] = subprocess.Popen,
) -> Optional[int]:
    """Launch the organism DETACHED (new session — it survives this
    terminal closing) with stdout+stderr piped to the daemon log.
    Returns the child pid, or None on spawn failure. NEVER raises."""
    try:
        log = daemon_log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        rollover_daemon_log(log)           # janitor at every ignition
        env = dict(os.environ)
        # The resident organism serves cockpits — give it the cockpit
        # session economics unless the operator overrode them.
        env.setdefault(
            "OUROBOROS_BATTLE_COST_CAP",
            os.environ.get("JARVIS_COCKPIT_COST_CAP", "2.50"),
        )
        env.setdefault("OUROBOROS_BATTLE_IDLE_TIMEOUT", os.environ.get(
            "JARVIS_OV_DAEMON_IDLE_TIMEOUT_S", "86400",
        ))
        with open(log, "ab") as sink:
            proc = spawner(
                daemon_argv(),
                cwd=str(repo_root()),
                stdout=sink,
                stderr=sink,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        return getattr(proc, "pid", None)
    except Exception:  # noqa: BLE001
        return None


async def await_socket(
    path: Path,
    *,
    on_tick: Optional[Callable[[float], None]] = None,
    deadline_s: Optional[float] = None,
) -> bool:
    """Wait (bounded) for the daemon's bridge to bind, re-probing with
    the zero-trust connect. ``on_tick(elapsed)`` drives the waking
    breadcrumb. NEVER raises."""
    deadline = deadline_s if deadline_s is not None else _boot_wait_s()
    start = time.monotonic()
    try:
        while (time.monotonic() - start) < deadline:
            if await probe_socket(path) == "live":
                return True
            if on_tick is not None:
                try:
                    on_tick(time.monotonic() - start)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(0.5)
        return False
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False


async def ensure_daemon(
    *,
    on_status: Optional[Callable[[str], None]] = None,
    spawner: Callable[..., Any] = subprocess.Popen,
) -> bool:
    """The full zero-trust route: probe → (clean ghost) → (cold boot)
    → wait-for-live. True when a live daemon is reachable. NEVER
    raises; NEVER hangs past the boot deadline; NEVER shows a
    traceback."""
    def _say(msg: str) -> None:
        if on_status is not None:
            try:
                on_status(msg)
            except Exception:  # noqa: BLE001
                pass

    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
        path = attach_socket_path()
        state = await probe_socket(path)
        if state == "live":
            return True
        if state == "stale":
            _say("⎿ ghost socket from a dead organism — cleaning")
            clean_stale_socket(path)
        _say("⏺ no organism awake — igniting one in the background")
        if spawn_daemon(spawner=spawner) is None:
            _say("⚠ ignition failed — see " + str(daemon_log_path()))
            return False

        last_beat = [-10.0]

        def _tick(elapsed: float) -> None:
            if elapsed - last_beat[0] >= 5.0:
                last_beat[0] = elapsed
                _say(f"⎿ organism waking · {int(elapsed)}s")

        if await await_socket(path, on_tick=_tick):
            _say("⏺ organism live — attaching")
            return True
        _say(
            "⚠ boot did not surface within the wait window — "
            "tail " + str(daemon_log_path()),
        )
        return False
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Resident Organism — launchd User Agent
# ---------------------------------------------------------------------------


def agent_plist_path(agents_dir: Optional[Path] = None) -> Path:
    base = agents_dir or (Path.home() / "Library" / "LaunchAgents")
    return base / f"{AGENT_LABEL}.plist"


def build_agent_plist() -> dict:
    """The launchd definition — every path resolved at INSTALL time
    (interpreter, repo, logs); nothing machine-hardcoded."""
    log_dir = repo_root() / ".jarvis" / "logs"
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(a) for a in daemon_argv()],
        "WorkingDirectory": str(repo_root()),
        "RunAtLoad": True,
        # KeepAlive restarts a crashed organism; single-flight makes a
        # racing duplicate exit 75 harmlessly.
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "ov-daemon.out.log"),
        "StandardErrorPath": str(log_dir / "ov-daemon.err.log"),
        "EnvironmentVariables": {
            "OUROBOROS_BATTLE_COST_CAP": os.environ.get(
                "JARVIS_COCKPIT_COST_CAP", "2.50",
            ),
            "OUROBOROS_BATTLE_IDLE_TIMEOUT": os.environ.get(
                "JARVIS_OV_DAEMON_IDLE_TIMEOUT_S", "86400",
            ),
        },
    }


def install_agent(
    *,
    agents_dir: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Write the plist + bootstrap it into the user's launchd domain.
    Returns a one-line operator message. NEVER raises."""
    try:
        path = agent_plist_path(agents_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        log_dir = repo_root() / ".jarvis" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Janitor the launchd sinks at (re)install so an accumulated
        # history never rides into the resident era unbounded.
        rollover_daemon_log(log_dir / "ov-daemon.out.log")
        rollover_daemon_log(log_dir / "ov-daemon.err.log")
        with open(path, "wb") as fh:
            plistlib.dump(build_agent_plist(), fh)
        try:
            uid = os.getuid()
            runner(
                ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                capture_output=True, timeout=10,
            )
        except Exception:  # noqa: BLE001
            return (
                f"⏺ resident agent written to {path} — load it with: "
                f"launchctl bootstrap gui/$UID {path}"
            )
        return f"⏺ resident organism installed ({AGENT_LABEL}) — {path}"
    except Exception as exc:  # noqa: BLE001
        return f"⚠ install failed: {exc}"


def uninstall_agent(
    *,
    agents_dir: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Boot the agent out of launchd + remove the plist. NEVER raises."""
    try:
        path = agent_plist_path(agents_dir)
        try:
            uid = os.getuid()
            runner(
                ["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"],
                capture_output=True, timeout=10,
            )
        except Exception:  # noqa: BLE001
            pass
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return (
            f"⏺ resident organism uninstalled ({AGENT_LABEL})"
            if existed else "⎿ no resident agent was installed"
        )
    except Exception as exc:  # noqa: BLE001
        return f"⚠ uninstall failed: {exc}"


__all__ = [
    "AGENT_LABEL",
    "agent_plist_path",
    "await_socket",
    "build_agent_plist",
    "clean_stale_socket",
    "daemon_argv",
    "daemon_log_path",
    "ensure_daemon",
    "install_agent",
    "probe_socket",
    "probe_tcp",
    "probe_http",
    "repo_root",
    "spawn_daemon",
    "thin_client_enabled",
    "uninstall_agent",
]
