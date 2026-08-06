"""
Grant bridge -- JARVIS's side of the screen-unlock authorization plugin.

WHAT THIS REPLACES
------------------
The path this supersedes fetched the macOS login password in cleartext and
synthesised keystrokes at the lock screen. It could not work on macOS 26
(SecurityAgent holds SecureEventInput and discards them), and it kept an
un-ACL'd copy of the credential for eight months to do it. See
`backend/security/credential_eradication.py`.

Here, no credential exists. JARVIS deposits a *grant*: an assertion that a human
it already authenticated asked for the screen to be unlocked, just now. The
grant carries no key material. A stolen one unlocks a screen once, within its
TTL, on the machine that minted it.

WHY THIS EXECS A BINARY INSTEAD OF SPEAKING XPC
-----------------------------------------------
PyObjC can open an NSXPCConnection, and doing so would remove a process hop. It
would also make the broker's code-signing check meaningless.

The broker authorises callers by designated requirement. A requirement naming
`python3.11` authorises *every script that interpreter ever runs* -- including
one an attacker drops in a directory JARVIS imports from. The check would be
syntactically present and semantically empty.

So the deposit privilege belongs to `jarvis-unlock-grant`: ~100 lines, one job,
no configuration surface, signable as itself. The extra hop IS the security
boundary, not overhead in front of it.

CONTRACT
--------
Every failure is a typed result, never an exception into the caller. This runs
on the voice-command path; a traceback there costs the operator a spoken reply.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "GrantOutcome",
    "GrantResult",
    "GrantBridge",
    "deposit_unlock_grant",
]


class GrantOutcome(IntEnum):
    """
    Mirrors the exit codes of ``jarvis-unlock-grant``.

    These values are the wire protocol between the helper and this module. They
    are duplicated across a language boundary that no build system spans, so
    ``tests/security/test_grant_bridge.py`` parses the Objective-C source and
    asserts the two enumerations agree. A silent drift here would turn "the
    broker refused us" into "the broker is down" -- an install problem
    misreported as a daemon problem.

    Values follow sysexits.h so they are meaningful to anything else that ever
    execs the helper.
    """

    DEPOSITED = 0
    USAGE = 64        # EX_USAGE
    UNAVAILABLE = 69  # EX_UNAVAILABLE -- broker absent, or it refused our signature
    TIMEOUT = 75      # EX_TEMPFAIL
    REJECTED = 77     # EX_NOPERM -- broker answered and said no
    CONFIG = 78       # EX_CONFIG

    # Produced by this module, never by the helper: the helper could not be run
    # at all. Distinct from CONFIG, which means the helper ran and found its own
    # configuration missing.
    HELPER_MISSING = 127

    @property
    def succeeded(self) -> bool:
        return self is GrantOutcome.DEPOSITED

    @property
    def is_install_problem(self) -> bool:
        """
        True when the fix is on disk rather than in a running process.

        The distinction is operational: UNAVAILABLE/TIMEOUT suggest restarting
        the daemon, while these suggest the install is wrong. Collapsing them
        sends the operator to the wrong place.
        """
        return self in (
            GrantOutcome.REJECTED,
            GrantOutcome.CONFIG,
            GrantOutcome.USAGE,
            GrantOutcome.HELPER_MISSING,
        )


@dataclass(frozen=True)
class GrantResult:
    """Outcome of a deposit attempt. Never carries credential material."""

    outcome: GrantOutcome
    grant_id: Optional[str] = None
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome.succeeded

    def __str__(self) -> str:
        base = f"{self.outcome.name.lower()}"
        if self.grant_id:
            base = f"{base} grant={self.grant_id}"
        if self.detail:
            base = f"{base} ({self.detail})"
        return base


# --- Configuration -----------------------------------------------------------
# Env-driven, matching the repo convention and the broker's own channel. No
# default for the helper path beyond a search of conventional install locations;
# a guessed absolute path to a binary that mints screen unlocks is exactly the
# thing not to guess.

ENV_HELPER_PATH = "JARVIS_UNLOCK_GRANT_HELPER"
ENV_DEPOSIT_TIMEOUT = "JARVIS_GRANT_DEPOSIT_TIMEOUT_S"

DEFAULT_DEPOSIT_TIMEOUT_S = 5.0
MIN_DEPOSIT_TIMEOUT_S = 0.5
MAX_DEPOSIT_TIMEOUT_S = 30.0

#: Searched in order when the env var is unset. Ordered most-specific first.
DEFAULT_HELPER_SEARCH_PATH = (
    "/usr/local/libexec/jarvis-unlock-grant",
    "/opt/jarvis/libexec/jarvis-unlock-grant",
)


def _resolve_timeout(env: Optional[dict] = None) -> float:
    source = os.environ if env is None else env
    raw = source.get(ENV_DEPOSIT_TIMEOUT)
    if not raw:
        return DEFAULT_DEPOSIT_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %.1fs",
                       ENV_DEPOSIT_TIMEOUT, raw, DEFAULT_DEPOSIT_TIMEOUT_S)
        return DEFAULT_DEPOSIT_TIMEOUT_S
    if value <= 0:
        return DEFAULT_DEPOSIT_TIMEOUT_S
    clamped = min(max(value, MIN_DEPOSIT_TIMEOUT_S), MAX_DEPOSIT_TIMEOUT_S)
    if clamped != value:
        logger.warning("%s=%.1fs clamped to %.1fs", ENV_DEPOSIT_TIMEOUT, value, clamped)
    return clamped


def resolve_helper_path(env: Optional[dict] = None) -> Optional[str]:
    """
    Locate the signed deposit helper.

    Explicit env var wins; otherwise the conventional install locations are
    searched. Returns None rather than a best guess -- a caller that receives
    None reports HELPER_MISSING, which names the real problem, instead of
    exec'ing something that happens to sit at a plausible path.
    """
    source = os.environ if env is None else env

    explicit = (source.get(ENV_HELPER_PATH) or "").strip()
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        logger.error("%s=%r is not an executable file", ENV_HELPER_PATH, explicit)
        return None

    for candidate in DEFAULT_HELPER_SEARCH_PATH:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    found = shutil.which("jarvis-unlock-grant")
    return found or None


class GrantBridge:
    """
    Deposits unlock grants by exec'ing the signed helper.

    Fully async: the helper is spawned via ``asyncio.create_subprocess_exec`` and
    awaited with a bounded timeout, so the event loop is never blocked -- the
    same discipline the boot-path work in this repo has been enforcing.
    """

    def __init__(
        self,
        helper_path: Optional[str] = None,
        timeout_s: Optional[float] = None,
        env: Optional[dict] = None,
    ) -> None:
        self._env = env
        self._explicit_helper = helper_path
        self._timeout_s = timeout_s if timeout_s is not None else _resolve_timeout(env)

    def _helper(self) -> Optional[str]:
        if self._explicit_helper:
            return self._explicit_helper
        return resolve_helper_path(self._env)

    async def deposit(self, reason: str) -> GrantResult:
        """
        Deposit a single-use unlock grant.

        Args:
            reason: Short operator-facing string for the broker's audit log,
                e.g. ``"voice: unlock my screen"``. Must never contain
                credentials -- it is written to a root-owned log verbatim.

        Returns:
            A GrantResult. Never raises for an operational failure; only
            ``asyncio.CancelledError`` propagates, because swallowing
            cancellation would detach this from its caller's lifecycle.
        """
        helper = self._helper()
        if helper is None:
            logger.error(
                "unlock grant helper not found (set %s, or install to one of %s)",
                ENV_HELPER_PATH, ", ".join(DEFAULT_HELPER_SEARCH_PATH),
            )
            return GrantResult(GrantOutcome.HELPER_MISSING,
                               detail="helper not installed")

        safe_reason = (reason or "").strip() or "(unspecified)"

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                helper,
                safe_reason,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate(proc)
            raise
        except asyncio.TimeoutError:
            # The helper has its own shorter internal deadline, so reaching this
            # one means it wedged rather than merely waited. Killed rather than
            # left: an orphaned helper holding a privileged XPC connection is
            # exactly the kind of thing that outlives its reason to exist.
            await self._terminate(proc)
            logger.error("unlock grant helper did not exit within %.1fs", self._timeout_s)
            return GrantResult(GrantOutcome.TIMEOUT, detail="helper wedged")
        except OSError as exc:
            logger.error("could not exec unlock grant helper %s: %s", helper, exc)
            return GrantResult(GrantOutcome.HELPER_MISSING, detail=str(exc))

        detail = (stderr or b"").decode("utf-8", "replace").strip()
        grant_id = (stdout or b"").decode("utf-8", "replace").strip() or None

        try:
            outcome = GrantOutcome(proc.returncode)
        except ValueError:
            # An exit code neither side defines. Reported as REJECTED rather
            # than assumed benign: an unrecognised answer from the component
            # that mints unlocks is not something to shrug at.
            logger.error("unlock grant helper returned unmapped exit %s (%s)",
                         proc.returncode, detail)
            return GrantResult(GrantOutcome.REJECTED,
                               detail=f"unmapped exit {proc.returncode}: {detail}")

        if outcome.succeeded:
            logger.info("unlock grant deposited (%s)", grant_id or "no id")
            return GrantResult(outcome, grant_id=grant_id)

        # grant_id is only meaningful on success; carrying stdout forward on a
        # failure would attach a plausible-looking id to a grant that does not
        # exist.
        logger.warning("unlock grant refused: %s%s",
                       outcome.name.lower(), f" ({detail})" if detail else "")
        return GrantResult(outcome, detail=detail)

    @staticmethod
    async def _terminate(proc) -> None:
        """Terminate, then kill. Never raises."""
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # pragma: no cover - process already gone
                pass
        except Exception:  # pragma: no cover - defensive
            pass


async def deposit_unlock_grant(reason: str) -> GrantResult:
    """Module-level convenience for the common one-shot case."""
    return await GrantBridge().deposit(reason)
