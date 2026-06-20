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
        # Native Asynchronous GCS Vault (2026-06-20): the ``gcs`` backend is
        # handled by the NATIVE google-cloud-storage SDK path (``_gcs_sync_native``
        # in ``run_once``), NOT a ``gsutil`` subprocess — the lean soak container
        # has no gcloud CLI, only the Python SDK + ADC from the instance metadata
        # server. Returning [] here signals "no argv; the native path owns gcs."
        return []
    if b == "git":
        # A self-contained git repo INSIDE src pushing to a private remote.
        msg = f"state-vault snapshot {int(time.time())}"
        return [
            ["git", "-C", src, "add", "-A"],
            ["git", "-C", src, "commit", "-m", msg, "--allow-empty"],
            ["git", "-C", src, "push", target, "HEAD"],
        ]
    return []


# ---------------------------------------------------------------------------
# Native GCS Vault — google-cloud-storage SDK, ADC from the instance metadata
# server, thread-offloaded so it never blocks the event loop. No gsutil / CLI.
# ---------------------------------------------------------------------------


def _parse_gs_uri(target: str) -> "Optional[tuple]":
    """``gs://bucket/prefix`` → ``(bucket, prefix)``. None if not a gs URI."""
    t = (target or "").strip()
    if not t.startswith("gs://"):
        return None
    rest = t[len("gs://"):]
    if not rest:
        return None
    parts = rest.split("/", 1)
    bucket = parts[0].strip()
    prefix = (parts[1].strip("/") if len(parts) > 1 else "")
    if not bucket:
        return None
    return (bucket, prefix)


def _gcs_push_blocking(src: str, target: str) -> bool:
    """Upload every file under ``src`` to ``gs://bucket/prefix`` via the native
    SDK (ADC auto-inherited from the GCE metadata server). Append-only ledger →
    upload-only (no destructive remote prune). Blocking; call via to_thread.
    NEVER raises — returns False on any failure."""
    try:
        parsed = _parse_gs_uri(target)
        if parsed is None:
            logger.warning("[StateVault] gcs target not a gs:// URI: %s", _redact(target))
            return False
        bucket_name, prefix = parsed
        if not os.path.isdir(src):
            logger.warning("[StateVault] gcs src dir missing: %s", src)
            return False
        from google.cloud import storage  # lazy — SDK optional at import time
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        n = 0
        for root, _dirs, files in os.walk(src):
            for fn in files:
                local = os.path.join(root, fn)
                rel = os.path.relpath(local, src)
                blob_name = f"{prefix}/{rel}" if prefix else rel
                bucket.blob(blob_name).upload_from_filename(local)
                n += 1
        logger.info("[StateVault] native GCS push: %d files %s → %s",
                    n, src, _redact(target))
        return True
    except Exception as exc:  # noqa: BLE001 — vault must never crash the soak
        logger.warning("[StateVault] native GCS push failed: %s", exc)
        return False


def _gcs_pull_blocking(target: str, dest: str) -> bool:
    """Download ``gs://bucket/prefix`` into ``dest`` via the native SDK (ADC).
    Used for preemption-resume on boot. Blocking; call via to_thread. NEVER
    raises — returns False on any failure (treated as 'fresh node')."""
    try:
        parsed = _parse_gs_uri(target)
        if parsed is None:
            return False
        bucket_name, prefix = parsed
        from google.cloud import storage  # lazy
        client = storage.Client()
        n = 0
        for blob in client.list_blobs(bucket_name, prefix=prefix or None):
            rel = blob.name[len(prefix):].lstrip("/") if prefix else blob.name
            if not rel:
                continue
            local = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
            blob.download_to_filename(local)
            n += 1
        logger.info("[StateVault] native GCS pull: %d files %s → %s",
                    n, _redact(target), dest)
        return n > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("[StateVault] native GCS pull failed (fresh node): %s", exc)
        return False


async def gcs_push(src: str, target: str) -> bool:
    """Async native GCS push — offloads the blocking SDK to a worker thread so
    the caller's event loop is never blocked. NEVER raises."""
    return await asyncio.to_thread(_gcs_push_blocking, src, target)


async def gcs_pull(target: str, dest: str) -> bool:
    """Async native GCS pull (preemption-resume). NEVER raises."""
    return await asyncio.to_thread(_gcs_pull_blocking, target, dest)


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
        # Native GCS path — no CLI subprocess (the lean container has the SDK,
        # not gsutil). Authenticates via ADC from the instance metadata server.
        if backend == "gcs":
            if not src or not target:
                logger.debug("[StateVault] gcs no-op (src/target unset)")
                return False
            ok = await gcs_push(src, target)
            if ok:
                logger.info("[StateVault] backed up %s → %s (gcs-native)",
                            src, _redact(target))
            return ok
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
    """CLI:
      (no args)   → continuous backup daemon (run_forever)
      --once      → a single push now (used by the Crucible cadence after each soak)
      --restore   → a single native GCS pull into JARVIS_BACKUP_SRC (boot resume)
    The --once / --restore one-shots are gate-INDEPENDENT (the cadence decides
    when to call them) so a master-off daemon flag doesn't block an explicit sync.
    """
    import sys
    logging.basicConfig(level=logging.INFO)
    args = set(sys.argv[1:])
    target, src = _target(), _src()
    if "--restore" in args:
        if _backend() == "gcs" and target:
            ok = asyncio.run(gcs_pull(target, src))
            logger.info("[StateVault] restore %s → %s ok=%s",
                        _redact(target), src, ok)
        else:
            logger.info("[StateVault] --restore no-op (backend!=gcs or no target)")
        return
    if "--once" in args:
        if _backend() == "gcs" and target:
            ok = asyncio.run(gcs_push(src, target))
        else:
            ok = asyncio.run(run_once())
        logger.info("[StateVault] --once push ok=%s", ok)
        return
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
    "gcs_push",
    "gcs_pull",
    "_parse_gs_uri",
]


if __name__ == "__main__":  # pragma: no cover
    _main()
