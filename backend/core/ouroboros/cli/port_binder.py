"""Resilient port selection — survives a cold-boot network stack.

Operator authorization 2026-07-19 (Phase 3, Resilient Startup mandate).
``unified_supervisor._detect_best_port`` scanned ``8010..8100`` and
returned the first bindable port — but on a fresh macOS boot the loopback
interface may not be configured yet, so EVERY bind raises
``EADDRNOTAVAIL`` and the old code silently fell back to an *unverified*
``start`` port that the real server then failed to bind.

Root-cause distinction (mandate 2 — Resilient Startup):
  * **EADDRINUSE** — the port is genuinely taken → advance to the next
    port immediately (no wait; this is normal contention).
  * **EADDRNOTAVAIL / transient** — the network stack isn't ready yet →
    the WHOLE scan is retried a bounded number of times with a short,
    capped backoff. Not a brute-force ``sleep(30)``: it returns the
    instant a port binds, and it fails fast to launchd (which restarts
    via ``KeepAlive{SuccessfulExit=false}``) once the bounded budget is
    spent. No hardcoded waits — every bound is env-tunable.

Pure stdlib; the injectable ``sleeper``/``binder`` seams make it fully
unit-testable without touching the real network. NEVER raises out of the
public entry point — a boot helper that crashes is worse than a fallback.
"""
from __future__ import annotations

import errno
import os
import socket
import time
from typing import Callable, Optional

#: Errnos that mean "this port is taken" — advance to the next port.
_IN_USE = {errno.EADDRINUSE}
#: Errnos that mean "the stack isn't ready" — retry the whole scan.
_TRANSIENT = {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, getattr(errno, "ENETDOWN", 50)}


def _max_scan_retries() -> int:
    try:
        return max(1, int(os.environ.get("JARVIS_PORT_BIND_MAX_RETRIES", "8")))
    except (TypeError, ValueError):
        return 8


def _base_backoff_s() -> float:
    try:
        return max(0.01, float(os.environ.get(
            "JARVIS_PORT_BIND_BACKOFF_S", "0.25")))
    except (TypeError, ValueError):
        return 0.25


def _max_backoff_s() -> float:
    try:
        return max(0.1, float(os.environ.get(
            "JARVIS_PORT_BIND_BACKOFF_CAP_S", "2.0")))
    except (TypeError, ValueError):
        return 2.0


def _try_bind(port: int, host: str = "127.0.0.1") -> Optional[int]:
    """Bind-test a single port. Returns the port on success, None if
    it's in use, and RE-RAISES a transient (stack-not-ready) OSError so
    the caller can retry the whole scan."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return port
    except OSError as exc:
        if exc.errno in _IN_USE:
            return None                    # taken — caller tries next port
        if exc.errno in _TRANSIENT:
            raise                          # stack not ready — caller retries
        # Unknown bind error — treat as "taken" so the scan moves on.
        return None


def resilient_detect_port(
    start: int,
    end: int,
    *,
    host: str = "127.0.0.1",
    binder: Callable[[int, str], Optional[int]] = _try_bind,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """First bindable port in ``[start, end]``, resilient to a cold-boot
    network stack. Returns ``start`` as a last resort only after the
    bounded transient-retry budget is exhausted (so launchd can restart
    us rather than us binding a lie). NEVER raises."""
    retries = _max_scan_retries()
    base, cap = _base_backoff_s(), _max_backoff_s()
    for attempt in range(retries):
        transient_seen = False
        for port in range(start, end + 1):
            try:
                got = binder(port, host)
            except OSError:
                # Transient on THIS port — the whole stack is likely not
                # up. Abort this scan pass and back off before retrying.
                transient_seen = True
                break
            if got is not None:
                return got
        if not transient_seen:
            # Scanned the full range with no transient fault: every port
            # is genuinely in use. Backing off won't help — fail fast.
            break
        # Bounded exponential backoff, capped. Returns immediately on the
        # next pass once the stack comes up.
        if attempt < retries - 1:
            sleeper(min(cap, base * (2 ** attempt)))
    return start


__all__ = ["resilient_detect_port"]
