"""Autonomous daemon auto-spawn reflex — `ov` summons the audio plane.

The friction this removes
------------------------
`ov` boots `scripts/ouroboros_battle_test.py`, which contains no audio pipeline
at all. The mic lives in `unified_supervisor.py`, whose
``wire_conversation_pipeline()`` is the sole thing that binds AudioBus. They are
separate programs, so typing ``wake`` in a bare `ov` session has historically
had nothing to arm.

The fix preserves the boundary rather than erasing it. `ov` does NOT import the
audio pipeline and never touches CoreAudio — it stays a thin IPC relayer. It
simply *starts the process that owns the hardware* and then subscribes over the
existing UDS, exactly as it already does when a supervisor happens to be up.

Why a refused connection is the right trigger
---------------------------------------------
A missing socket file is ambiguous — it can mean "never started", "crashed
leaving a stale inode", or "starting right now". ``ConnectionRefusedError``
(and its absence) is the authoritative signal, because it comes from the
kernel's view of whether anything is *listening*. So the reflex probes by
connecting, not by stat-ing a path.

Concurrency
-----------
No lock is taken here on purpose. ``unified_supervisor`` already guards itself:
``_fast_kernel_check()`` runs BEFORE its heavy imports and exits immediately if
a healthy kernel exists — so a duplicate spawn costs a short-lived process, not
a second audio owner. (Note this is a *different* mechanism from the battle
test's ``exit 75`` single-flight lock; both make blind spawning safe, but the
supervisor's is an early-exit rather than a lock.) Racing `ov` instances
therefore converge on one supervisor without coordination.

Failure policy
--------------
Every path degrades to "no audio, carry on". A cockpit that cannot start the
audio plane must still be a working cockpit — this reflex may never raise, and
may never block the UI loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Tuple

logger = logging.getLogger(__name__)


def reflex_enabled() -> bool:
    """Master gate. Default ON — but the reflex is inert unless a voice
    capability is actually requested, so it never spawns a 98K-line kernel
    behind an operator who only wanted the text cockpit."""
    return os.environ.get(
        "JARVIS_OV_AUDIO_AUTOSPAWN", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _boot_budget_s() -> float:
    """Total time to wait for the supervisor to start listening.

    10s, MEASURED not guessed. The original 90 was my own conservative
    invention and was never observed: `--status` completes in 2.9s and
    `--help` in 4.2s once the Py_FinalizeEx segfault is bypassed. A budget an
    order of magnitude above reality is not caution — it is a UI that appears
    hung for 90 seconds when a supervisor genuinely cannot start."""
    try:
        return max(1.0, float(os.environ.get("JARVIS_OV_AUDIO_BOOT_BUDGET_S", "10")))
    except (TypeError, ValueError):
        return 10.0


def _backoff_base_s() -> float:
    try:
        return max(0.05, float(os.environ.get("JARVIS_OV_AUDIO_BACKOFF_BASE_S", "0.25")))
    except (TypeError, ValueError):
        return 0.25


def _backoff_cap_s() -> float:
    try:
        return max(0.1, float(os.environ.get("JARVIS_OV_AUDIO_BACKOFF_CAP_S", "3.0")))
    except (TypeError, ValueError):
        return 3.0


def audio_host_path() -> Optional[Path]:
    """Locate the audio-plane host from THIS module's position rather than a
    cwd-relative guess: `ov` is launched from arbitrary directories.

    ``backend/audio/audio_plane_host.py``, NOT ``unified_supervisor.py``. The
    supervisor does own a microphone, but it reaches one by booting the
    websocket router, the legacy web app and the local model stack — a web UI
    nobody asked for, and a local model loaded into the same unified memory the
    audio path is competing for. The host is the same
    ``wire_conversation_pipeline`` call with a process around it."""
    try:
        root = Path(__file__).resolve().parents[4]
        candidate = root / "backend" / "audio" / "audio_plane_host.py"
        return candidate if candidate.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def supervisor_path() -> Optional[Path]:
    """Deprecated alias for :func:`audio_host_path`.

    Kept because the name is load-bearing in existing callers and tests; the
    TARGET moved, the seam did not."""
    return audio_host_path()


async def probe_socket(path: Optional[Path] = None, *, timeout: float = 0.5) -> bool:
    """Is something LISTENING on the audio-state socket right now?

    Connect-and-close rather than ``path.exists()``: a stale socket inode
    survives a SIGKILL, so file presence proves nothing. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
            socket_path,
        )
        sock = Path(path) if path is not None else socket_path()
    except Exception:  # noqa: BLE001
        return False
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(sock)), timeout=timeout,
        )
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError, asyncio.TimeoutError):
        return False
    except Exception:  # noqa: BLE001
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


def host_log_path() -> Optional[Path]:
    """Where a spawned host's output goes. Repo-anchored, never cwd-relative.

    stdio used to go to DEVNULL on the reasoning that a chatty boot must not
    corrupt the TUI's terminal. That reasoning is right and the destination was
    wrong: a detached process that dies leaves NO trace, so "the cockpit says
    no audio plane" became unanswerable — the one question the operator asks is
    the one the design made unanswerable.

    A file satisfies both: the terminal stays clean and the failure is
    readable. NEVER raises."""
    try:
        root = Path(__file__).resolve().parents[4]
        log_dir = root / ".jarvis"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "audio_plane.log"
    except Exception:  # noqa: BLE001
        return None


def _open_host_log() -> Any:
    """Append-mode handle for the host's stdio, or DEVNULL if unavailable.

    Append, not truncate: the interesting case is a host that dies and gets
    respawned, and truncating would erase the death that explains the retry."""
    path = host_log_path()
    if path is None:
        return subprocess.DEVNULL
    try:
        return open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return subprocess.DEVNULL


def spawn_supervisor(
    *, script: Optional[Path] = None, extra_args: Optional[list] = None,
) -> Optional[int]:
    """Start the audio-plane host DETACHED. Returns its pid, or None.

    ``start_new_session=True`` puts it in its own process group so it survives
    the cockpit exiting — the audio plane is meant to outlive an ephemeral
    client, and this mirrors how the codebase already detaches `say`/`afplay`.
    stdio goes to DEVNULL so a chatty boot cannot corrupt the TUI's terminal.

    No duplicate-guard here by design: the host probes the audio-state socket
    before binding and exits 75 when another already serves it, so a duplicate
    spawn costs a short-lived process rather than a second microphone owner —
    which CoreAudio would refuse anyway."""
    path = Path(script) if script is not None else audio_host_path()
    # Validate REGARDLESS of where the path came from. Popen succeeds on a
    # missing script — python3 starts, then dies — so without this check the
    # reflex would report a pid, believe it spawned an audio plane, and then
    # burn the full boot budget waiting for a socket that can never appear.
    if path is None or not path.is_file():
        logger.debug("[AudioReflex] supervisor script not found: %s", path)
        return None
    try:
        python = sys.executable or shutil.which("python3") or "python3"
        argv = [python, str(path)] + list(extra_args or [])
        sink = _open_host_log()
        proc = subprocess.Popen(
            argv,
            # REPO ROOT, not the script's directory. `cwd=path.parent` sent
            # the host to backend/audio/, where every cwd-relative path it
            # touched resolved somewhere nobody else was looking — it bound
            # backend/audio/.jarvis/audio_state.sock while the cockpit waited
            # on the repo-root one. socket_path() is anchored now, but the
            # child's cwd should still be the tree it belongs to.
            cwd=str(path.resolve().parents[2]),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logger.info(
            "[AudioReflex] spawned audio plane host pid=%s (log: %s)",
            proc.pid, host_log_path(),
        )
        return proc.pid
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AudioReflex] spawn failed: %r", exc)
        return None


async def await_socket(
    *,
    budget_s: Optional[float] = None,
    probe: Optional[Callable[[], Awaitable[bool]]] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> bool:
    """Exponential backoff with FULL JITTER until the socket listens.

    Full jitter (``uniform(0, delay)``) rather than fixed steps: several `ov`
    instances started together would otherwise probe in lockstep and hammer the
    booting kernel at exactly the same instants.

    The delay grows toward a cap so a slow boot costs few probes, while the
    total is bounded so an operator is never stranded. NEVER raises."""
    _probe = probe or (lambda: probe_socket())
    _sleep = sleep or asyncio.sleep
    _now = clock or time.monotonic
    budget = budget_s if budget_s is not None else _boot_budget_s()
    base, cap = _backoff_base_s(), _backoff_cap_s()

    deadline = _now() + budget
    attempt = 0
    while _now() < deadline:
        try:
            if await _probe():
                return True
        except Exception:  # noqa: BLE001
            pass
        delay = min(cap, base * (2 ** attempt))
        remaining = deadline - _now()
        if remaining <= 0:
            break
        await _sleep(max(0.0, min(random.uniform(0.0, delay), remaining)))
        attempt += 1
    # One last look: the socket may have bound during the final sleep.
    try:
        return await _probe()
    except Exception:  # noqa: BLE001
        return False


async def ensure_audio_daemon(
    *,
    probe: Optional[Callable[[], Awaitable[bool]]] = None,
    spawn: Optional[Callable[[], Optional[int]]] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    clock: Optional[Callable[[], float]] = None,
    budget_s: Optional[float] = None,
) -> Tuple[bool, str]:
    """Guarantee an audio plane, spawning one only if none is listening.

    Returns ``(available, reason)``. Reasons are a closed set so a caller can
    render an honest status line rather than a boolean:

      ``already_live``   — a supervisor was already listening; nothing spawned.
      ``spawned``        — none was listening; one was started and came up.
      ``spawn_failed``   — could not start one (script missing, exec refused).
      ``boot_timeout``   — started, but did not begin listening in budget.
      ``disabled``       — the reflex is gated off.

    NEVER raises. Every non-``already_live``/``spawned`` outcome means the
    cockpit continues in text-only mode."""
    if not reflex_enabled():
        return (False, "disabled")
    _probe = probe or (lambda: probe_socket())
    _spawn = spawn or spawn_supervisor
    try:
        # Probe FIRST. The common case is a live supervisor, and that path must
        # cost one connect — never a spawn, never a sleep.
        if await _probe():
            return (True, "already_live")

        if _spawn() is None:
            return (False, "spawn_failed")

        ok = await await_socket(
            budget_s=budget_s, probe=_probe, sleep=sleep, clock=clock,
        )
        return (True, "spawned") if ok else (False, "boot_timeout")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AudioReflex] ensure failed: %r", exc)
        return (False, "spawn_failed")


__all__ = [
    "audio_host_path",
    "await_socket",
    "host_log_path",
    "ensure_audio_daemon",
    "probe_socket",
    "reflex_enabled",
    "spawn_supervisor",
    "supervisor_path",
]
