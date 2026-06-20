"""Slice 138 — Autonomous State Persistence Daemon (the Evidence Vault).

Continuously backs up the ``.jarvis/`` directory — the organism's episodic memory
(``episodic_memory.jsonl``), tamper-evident evidence chain
(``dissertation_evidence.jsonl``), and signed roadmap — to a remote target so the
12-month T5 evidence + memory survive host-death. No manual backups.

**Pluggable backend** (``JARVIS_BACKUP_BACKEND`` = ``rsync`` | ``s3`` | ``git``)
to a ``JARVIS_BACKUP_TARGET``. **Gated** ``JARVIS_STATE_BACKUP_ENABLED``
default-FALSE. **Async + fail-soft**: a backup failure (network down, bad target,
non-zero exit) is logged and swallowed — it must NEVER crash the soak. The shell
runner is injectable so the command-building + loop logic is fully unit-tested
without touching the network or disk.

Run as its own process / systemd unit:
    python3 -m backend.core.ouroboros.governance.state_persistence_daemon
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_STATE_BACKUP_ENABLED"
_ENV_BACKEND = "JARVIS_BACKUP_BACKEND"
_ENV_TARGET = "JARVIS_BACKUP_TARGET"
_ENV_SRC = "JARVIS_BACKUP_SRC"
_ENV_INTERVAL = "JARVIS_BACKUP_INTERVAL_S"
_ENV_CMD_TIMEOUT = "JARVIS_BACKUP_CMD_TIMEOUT_S"

_DEFAULT_SRC = ".jarvis"
_DEFAULT_INTERVAL = 900.0   # 15 min
_DEFAULT_CMD_TIMEOUT = 300.0


def state_backup_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def _backend() -> str:
    return (os.getenv(_ENV_BACKEND, "rsync") or "rsync").strip().lower()


def _target() -> str:
    return (os.getenv(_ENV_TARGET, "") or "").strip()


def _src() -> str:
    return (os.getenv(_ENV_SRC, _DEFAULT_SRC) or _DEFAULT_SRC).strip()


def _interval_s() -> float:
    try:
        return max(1.0, float(os.getenv(_ENV_INTERVAL, _DEFAULT_INTERVAL)))
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL


def _cmd_timeout_s() -> float:
    try:
        return max(1.0, float(os.getenv(_ENV_CMD_TIMEOUT, _DEFAULT_CMD_TIMEOUT)))
    except (TypeError, ValueError):
        return _DEFAULT_CMD_TIMEOUT


def _redact(target: str) -> str:
    """Never log a target that may embed a credential (e.g. https://tok@host)."""
    if "@" in target:
        return target.split("@", 1)[-1]  # drop anything before '@'
    return target


def build_backup_commands(backend: str, src: str, target: str) -> List[List[str]]:
    """Pure: map (backend, src, target) → an ordered list of argv command lists.
    rsync/s3 are one command; git is add → commit → push. Unknown → [] (no-op).
    NEVER raises."""
    b = (backend or "").strip().lower()
    if not src or not target:
        return []
    if b == "rsync":
        # Mirror the dir (trailing slash → contents), prune deletions remotely.
        return [["rsync", "-az", "--delete", f"{src.rstrip('/')}/", target]]
    if b == "s3":
        return [["aws", "s3", "sync", src, target, "--delete"]]
    if b == "gcs":
        # Sovereign Cognitive Crucible amnesia-proofing (2026-06-20): mirror the
        # state dir to a GCS bucket so a preempted Spot node resumes its
        # graduation ledger + soak history on restart. ``-m`` parallel, ``-r``
        # recursive, ``-d`` prune remote deletions (mirror semantics, matching
        # rsync/s3). The Spot VM's default compute SA authenticates via ADC.
        return [["gsutil", "-m", "rsync", "-r", "-d", src.rstrip("/"), target]]
    if b == "git":
        # A self-contained git repo INSIDE src pushing to a private remote.
        msg = f"state-vault snapshot {int(time.time())}"
        return [
            ["git", "-C", src, "add", "-A"],
            ["git", "-C", src, "commit", "-m", msg, "--allow-empty"],
            ["git", "-C", src, "push", target, "HEAD"],
        ]
    return []


async def _default_runner(cmd: List[str]) -> int:
    """Run ``cmd`` off the event loop with a hard timeout. Returns the exit code
    (non-zero on failure / timeout). NEVER raises out."""
    def _call() -> int:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=_cmd_timeout_s(),
            )
            return int(proc.returncode)
        except Exception:  # noqa: BLE001
            return 1
    return await asyncio.to_thread(_call)


_Runner = Callable[[List[str]], Awaitable[int]]


async def run_once(*, runner: Optional[_Runner] = None) -> bool:
    """Back up once. Gated + fail-soft. Returns True iff every command in the
    backend sequence exited 0. NEVER raises."""
    if not state_backup_enabled():
        return False
    try:
        backend, src, target = _backend(), _src(), _target()
        cmds = build_backup_commands(backend, src, target)
        if not cmds:
            logger.debug("[StateVault] no-op (backend=%s target set=%s)",
                         backend, bool(target))
            return False
        run = runner or _default_runner
        for cmd in cmds:
            rc = await run(cmd)
            if rc != 0:
                logger.warning("[StateVault] backup step failed rc=%s backend=%s → %s",
                               rc, backend, _redact(target))
                return False
        logger.info("[StateVault] backed up %s → %s (%s)", src, _redact(target), backend)
        return True
    except Exception as exc:  # noqa: BLE001 — backup must never crash the soak
        logger.warning("[StateVault] backup swallowed: %s", exc)
        return False


async def run_forever(
    *, interval_s: Optional[float] = None,
    runner: Optional[_Runner] = None,
    stop: Optional[asyncio.Event] = None,
) -> None:
    """Continuous backup loop. Backs up every ``interval_s`` until ``stop`` is
    set. Each iteration is fail-soft. NEVER raises out."""
    interval = interval_s if interval_s is not None else _interval_s()
    logger.info("[StateVault] daemon started (interval=%.0fs, backend=%s)",
                interval, _backend())
    while True:
        await run_once(runner=runner)
        if stop is not None and stop.is_set():
            return
        try:
            if stop is not None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return  # stop fired during the wait
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(interval)


def _main() -> None:  # pragma: no cover — process entrypoint
    logging.basicConfig(level=logging.INFO)
    if not state_backup_enabled():
        logger.warning("[StateVault] JARVIS_STATE_BACKUP_ENABLED not set — exiting")
        return
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        pass


__all__ = [
    "state_backup_enabled",
    "build_backup_commands",
    "run_once",
    "run_forever",
]


if __name__ == "__main__":  # pragma: no cover
    _main()
