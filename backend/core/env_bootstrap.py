"""Centralized .env bootstrap — the ONE config entry point.

Phase 0 (operator authorization 2026-07-19). Every process boundary
(unified_supervisor, backend.main, the ov thin-client daemon) calls
:func:`load_env_once` at its highest bootstrap point, BEFORE any heavy
ML/vision module is instantiated (mandate 3 — exactly once).

Strict precedence (mandate 2): ``override=False`` — a variable already
in the real environment (macOS shell, ``launchd`` plist,
``ov daemon --install`` EnvironmentVariables) ALWAYS wins over the
local ``.env`` file. The ``.env`` provides DEFAULTS, never overrides
an explicit operator/launchd decision.

Idempotent (a process-wide flag) so a second call from a re-entrant
boot path is a no-op. NEVER raises — a missing python-dotenv or a
malformed ``.env`` degrades to "use the real environment as-is", it
never blocks boot.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Jarvis.EnvBootstrap")

_LOADED = False
_LOCK = threading.Lock()


def _repo_root() -> Path:
    # backend/core/env_bootstrap.py → repo root is parents[2].
    return Path(__file__).resolve().parents[2]


def env_file_path() -> Path:
    """``JARVIS_ENV_FILE`` override, else the repo-root ``.env``."""
    override = os.environ.get("JARVIS_ENV_FILE", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _repo_root() / ".env"


def load_env_once(path: Optional[Path] = None) -> bool:
    """Load the ``.env`` with real-environment precedence, EXACTLY
    once per process. Returns True when a file was applied (or already
    was). NEVER raises."""
    global _LOADED
    with _LOCK:
        if _LOADED:
            return True
        _LOADED = True
        try:
            target = path or env_file_path()
            if not target.exists():
                logger.debug("[env] no .env at %s — using environment as-is",
                             target)
                return False
            try:
                from dotenv import load_dotenv  # noqa: PLC0415
            except Exception:  # noqa: BLE001 — dependency optional
                logger.warning(
                    "[env] python-dotenv unavailable — %s NOT loaded; "
                    "using environment as-is", target,
                )
                return False
            # override=False: explicit env / launchd plist ALWAYS wins.
            load_dotenv(dotenv_path=str(target), override=False)
            logger.info("[env] loaded %s (override=False — env/launchd wins)",
                        target)
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[env] load degraded", exc_info=True)
            return False


async def terminate_frontend_subprocess(
    process: object,
    *,
    graceful_timeout_s: float = 10.0,
    kill_timeout_s: float = 5.0,
) -> str:
    """Zombie-Process Prevention (mandate 2): gracefully terminate a
    frontend subprocess and AWAIT its closure before returning —
    guaranteeing port release. SIGTERM (process-group when possible) →
    bounded wait → SIGKILL escalation → bounded wait. Extracted here
    so the teardown contract is unit-provable without booting the
    supervisor. Returns the outcome (``terminated`` / ``killed`` /
    ``already_exited`` / ``none``). NEVER raises."""
    import asyncio  # noqa: PLC0415
    import signal  # noqa: PLC0415
    try:
        if process is None:
            return "none"
        if getattr(process, "returncode", None) is not None:
            return "already_exited"
        pid = getattr(process, "pid", None)
        # SIGTERM the whole group (npm + children) when we can.
        try:
            if pid is not None:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=graceful_timeout_s)
            return "terminated"
        except asyncio.TimeoutError:
            pass
        # Escalate to SIGKILL.
        try:
            if pid is not None:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=kill_timeout_s)
        except asyncio.TimeoutError:
            pass
        return "killed"
    except Exception:  # noqa: BLE001
        return "none"


def frontend_autolaunch_enabled() -> bool:
    """``JARVIS_FRONTEND_AUTOLAUNCH`` — default OFF (Phase 0: no
    browser popup). NEVER raises."""
    return os.environ.get(
        "JARVIS_FRONTEND_AUTOLAUNCH", "",
    ).strip().lower() in ("1", "true", "yes", "on")


__all__ = [
    "env_file_path",
    "frontend_autolaunch_enabled",
    "load_env_once",
]
