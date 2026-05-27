"""Slice 26 — Asynchronous Process-Linked Power Assertion Engine.

Closes the host-sleep wedge surfaced by v19 (bt-2026-05-27-003843):
the operator's Mac suspended ~6 minutes after the SWE-Bench-Pro
injection landed, killing the soak before any dispatch could occur.
The LoopDeadman correctly detected the wedged event loop and fired
``os._exit(75)`` — the architecture worked — but we should not rely on
the operator manually wrapping the soak script with ``caffeinate``.
J.A.R.M.A.T.R.I.X. commands its underlying hardware runtime natively.

# Mechanism

At boot, spawn an asyncio subprocess invoking the platform's native
sleep-prevention utility, **bound to the current Python process PID**.
When the parent process exits, crashes, or is SIGTERM'd, the kernel
automatically releases the assertion — no cleanup needed, no orphan
risk.

Platform matrix:

* **darwin** (macOS): ``caffeinate -w <PID>`` (system-provided since
  10.8; lives at ``/usr/bin/caffeinate``). The ``-w`` flag tells
  caffeinate to wait for the given PID to exit before releasing the
  IOPMAssertionCreateWithName lock. Process-linked = no orphan.

* **linux** / **win32** / **other**: no native equivalent that's
  universally available without additional packages (systemd-inhibit
  exists but isn't guaranteed; PowerThrottling on Windows requires
  pywin32). Slice 26 is darwin-only by design — operators on other
  platforms either don't have the suspend problem (server class hosts)
  or need to configure their own power management.

# Defensive shape

* **Master flag default-on for darwin, default-off elsewhere** — the
  load-bearing safety net only fires where it's both wanted and
  available.
* **Binary-missing → graceful skip** with warning; boot proceeds.
* **Subprocess-spawn-raised → graceful skip** with warning; boot
  proceeds. Power assertion is enhancement, not correctness path.
* **Return value carries the subprocess handle** so the integration
  site can store it for visibility (the kernel manages the lifecycle;
  Python doesn't need to wait on it).
* **No double-arm protection needed**: spawning a second caffeinate
  bound to the same PID is idempotent at the kernel level (multiple
  assertions stack additively; both release when the PID exits).
  But we provide ``power_assertion_active()`` so callers can check
  before re-spawning.

# §5 transparency

The attestation log line is operator-attested verbatim per the Slice
26 directive:

  [PowerSupervisor] Active process-linked host sleep assertion
  established via IOKit/Caffeinate for PID: <N>.

AST-pinned via the test suite so future copy edits don't silently
weaken the audit trail.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

_ENV_MASTER = "JARVIS_POWER_ASSERTION_ENABLED"
_CAFFEINATE_BINARY = "caffeinate"


# ──────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PowerAssertion:
    """One active power-management assertion. Frozen — kernel owns the
    actual lifecycle; this is just a Python-side handle for visibility.

    ``subprocess_pid`` is the caffeinate process's own PID (NOT the
    parent it's waiting on). Useful for log forensics + the rare case
    where an operator wants to manually inspect / verify.
    ``parent_pid`` is the Python process PID the assertion is bound to.
    """

    platform: str
    parent_pid: int
    subprocess_pid: int
    binary: str


# ──────────────────────────────────────────────────────────────────────
# Platform + flag gates
# ──────────────────────────────────────────────────────────────────────


def _is_supported_platform() -> bool:
    """Currently only darwin has a universally-available native binary
    we can rely on (caffeinate, shipped with macOS since 10.8)."""
    return sys.platform == "darwin"


def is_power_assertion_enabled() -> bool:
    """Master flag — default-on for supported platforms, default-off
    elsewhere. Operator can explicitly override either way."""
    raw = os.environ.get(_ENV_MASTER, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    # Default: on for darwin (where we have a clean native primitive),
    # off elsewhere (where the safe choice is operator-managed power).
    return _is_supported_platform()


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────


async def assert_power_lock(
    *,
    parent_pid: Optional[int] = None,
) -> Optional[PowerAssertion]:
    """Spawn a non-blocking, process-linked power assertion.

    Returns the :class:`PowerAssertion` handle on success, or ``None``
    when:

      * master flag explicitly off (operator opt-out)
      * platform unsupported (non-darwin)
      * binary not found on PATH (caffeinate missing — rare even on
        macOS but defensive)
      * subprocess spawn raised (any exception → swallow + warn)

    The returned handle is purely for visibility — the kernel manages
    the assertion's lifecycle. When the parent Python process exits,
    crashes, or is SIGTERM'd, caffeinate's ``-w`` flag triggers
    automatic release. No Python-side cleanup needed; no orphan risk.

    ``parent_pid`` defaults to ``os.getpid()`` of the calling process.
    Tests pass an explicit PID to verify the wiring without binding
    the test process itself.
    """
    if not is_power_assertion_enabled():
        logger.debug(
            "[PowerSupervisor] skipping: master flag off "
            "(JARVIS_POWER_ASSERTION_ENABLED=false OR non-darwin "
            "platform=%s)",
            sys.platform,
        )
        return None

    if not _is_supported_platform():
        logger.info(
            "[PowerSupervisor] platform=%s has no native power-assertion "
            "primitive; skipping. Operator-managed sleep prevention is "
            "required if soak-class continuity matters on this host.",
            sys.platform,
        )
        return None

    # Resolve binary on PATH — defensive against minimal containers
    # where caffeinate might be missing (Apple-shipped binaries get
    # stripped in some Nix / docker-on-darwin scenarios).
    binary_path = shutil.which(_CAFFEINATE_BINARY)
    if not binary_path:
        logger.warning(
            "[PowerSupervisor] %s binary not found on PATH; sleep "
            "assertion skipped. Soak continuity is operator-managed.",
            _CAFFEINATE_BINARY,
        )
        return None

    pid = parent_pid if parent_pid is not None else os.getpid()

    try:
        # asyncio.create_subprocess_exec is the established codebase
        # pattern (browser_bridge.py:141, cross_process_jsonl.py).
        # No shell=True — pure argv list, safe against injection.
        # No stdout/stderr capture — caffeinate is silent on success;
        # any noise it emits goes to /dev/null via DEVNULL.
        proc = await asyncio.create_subprocess_exec(
            binary_path,
            "-w",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 — power assertion is enhancement
        logger.warning(
            "[PowerSupervisor] failed to spawn %s -w %d: %r; "
            "sleep assertion skipped. Boot continues.",
            binary_path, pid, exc,
        )
        return None

    assertion = PowerAssertion(
        platform=sys.platform,
        parent_pid=pid,
        subprocess_pid=proc.pid,
        binary=binary_path,
    )

    # §5 attestation — operator-attested verbatim per Slice 26
    # directive. AST-pinned via test suite.
    logger.info(
        "[PowerSupervisor] Active process-linked host sleep assertion "
        "established via IOKit/Caffeinate for PID: %d.",
        pid,
    )
    logger.debug(
        "[PowerSupervisor] caffeinate subprocess_pid=%d binary=%s "
        "platform=%s — kernel releases assertion automatically when "
        "PID %d exits.",
        proc.pid, binary_path, sys.platform, pid,
    )
    return assertion
