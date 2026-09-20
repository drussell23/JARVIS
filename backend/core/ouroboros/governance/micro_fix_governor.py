"""Quota and regression control for the micro-fix repair loop.

What was already bounded
------------------------

``InteractiveRepairLoop.repair`` iterates ``for iteration in
range(_max_iterations())``. There is no unbounded loop to close: a single
invocation has always been deterministically bounded, and the ladder that
calls it is bounded in turn. Adding a second per-invocation ceiling would
duplicate the one that exists.

What was not
------------

1. **Nothing spanned invocations.** ``repair()`` is called once per
   VALIDATE_RETRY iteration, and each call starts its counter at zero. An
   op with three ladder iterations could therefore spend three full
   iteration budgets on the same file -- bounded at every level and
   unbounded across them.

2. **Nothing noticed a fix that made things worse.** If a patch introduces
   a syntax error the file stops parsing, every subsequent traceback comes
   from the damage rather than the original defect, and the loop spends the
   rest of its budget chasing its own edit. This is the failure that
   justifies the whole mechanism.

3. **Nothing detected a cycle.** A model that re-emits the same text is not
   converging, and the iteration ceiling was the only thing that stopped it
   -- after paying for every iteration.

(3) is a solved problem in this tree: ``ForwardProgressDetector`` already
does consecutive-content-hash cycle detection for the GENERATE retry loop,
with TTL pruning and env configuration. It is composed here rather than
reimplemented, keyed per ``(op, file)`` so two files in one op cannot
collide and so the count survives across ``repair()`` calls.

Async because VALIDATE validates candidates concurrently
(``asyncio.gather``), so admission is a read-modify-write that two coroutines
can enter at once. The lock makes the decision atomic; a quota that can be
double-spent under concurrency is not a quota.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from backend.core.ouroboros.governance.forward_progress import (
    ForwardProgressDetector,
)

logger = logging.getLogger("Ouroboros.MicroFixGovernor")

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_TTL_S = 3600.0


def _env_int(name: str, default: int) -> int:
    """Call-time, never raises. Matches the CostGovernor/ForwardProgress shape."""
    try:
        value = int((os.environ.get(name, "") or "").strip() or default)
        return value if value > 0 else default
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        value = float((os.environ.get(name, "") or "").strip() or default)
        return value if value > 0.0 else default
    except (ValueError, TypeError):
        return default


class MicroFixExhaustionFault(Exception):
    """A micro-fix quota was spent on one ``(op, file)`` without converging.

    Carries the accounting rather than a bare message so the caller can log
    the decision without re-deriving it, and so the ladder can distinguish
    "spent its attempts" from "made the file worse".
    """

    def __init__(
        self, message: str, *, op_id: str, file_path: str,
        attempts: int, limit: int, reason: str,
    ) -> None:
        super().__init__(message)
        self.op_id = op_id
        self.file_path = file_path
        self.attempts = attempts
        self.limit = limit
        self.reason = reason


@dataclass(frozen=True)
class QuotaVerdict:
    """Whether a micro-fix may proceed, and the arithmetic behind it."""

    permitted: bool
    reason: str
    attempts: int
    limit: int

    def render(self) -> str:
        return (
            f"permitted={self.permitted} reason={self.reason} "
            f"attempts={self.attempts}/{self.limit}"
        )


@dataclass
class _Entry:
    attempts: int = 0
    created_at: float = field(default_factory=time.monotonic)
    severed: str = ""


def content_fingerprint(text: str) -> str:
    """SHA-256 of the text under repair. Empty text yields no fingerprint.

    An empty fingerprint is a deliberate no-op in ``ForwardProgressDetector``
    -- absence of content is not evidence of repetition.
    """
    if not text:
        return ""
    try:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def parses_cleanly(text: str, file_path: str) -> Optional[bool]:
    """Whether *text* is syntactically valid Python.

    ``None`` means "not a question worth asking" -- a non-Python file, or
    text that cannot be examined -- and callers must treat it as neither
    pass nor fail. A tri-state matters here: judging a ``.json`` fixture by
    Python's grammar would call every valid one a regression.
    """
    if not file_path.endswith(".py"):
        return None
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False
    except Exception:  # noqa: BLE001
        return None


class MicroFixQuotaGovernor:
    """Per-``(op, file)`` admission for the micro-fix loop.

    Deliberately not a singleton by import: the orchestrator owns one so
    tests can construct an isolated instance, and ``default_governor()``
    serves the production path that has no natural owner to thread one from.
    """

    def __init__(
        self,
        *,
        max_attempts: Optional[int] = None,
        ttl_s: Optional[float] = None,
        detector: Optional[ForwardProgressDetector] = None,
    ) -> None:
        self._max_attempts = (
            max_attempts if max_attempts is not None
            else _env_int("JARVIS_MICRO_FIX_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
        )
        self._ttl_s = (
            ttl_s if ttl_s is not None
            else _env_float("JARVIS_MICRO_FIX_TTL_S", _DEFAULT_TTL_S)
        )
        self._detector = detector or ForwardProgressDetector()
        self._entries: Dict[Tuple[str, str], _Entry] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def _prune(self) -> None:
        """Drop entries older than the TTL so a long-lived process cannot
        accumulate one per op forever. Called under the lock."""
        now = time.monotonic()
        stale = [
            key for key, entry in self._entries.items()
            if now - entry.created_at > self._ttl_s
        ]
        for key in stale:
            self._entries.pop(key, None)

    async def admit(self, *, op_id: str, file_path: str) -> QuotaVerdict:
        """Claim one attempt for ``(op_id, file_path)``.

        The claim is taken at admission, not at completion: an attempt that
        crashes still consumed a turn, and a quota that only counts clean
        exits is a quota a crash loop can evade.
        """
        key = (op_id, file_path)
        async with self._lock:
            self._prune()
            entry = self._entries.setdefault(key, _Entry())
            if entry.severed:
                return QuotaVerdict(
                    False, f"severed:{entry.severed}",
                    entry.attempts, self._max_attempts,
                )
            if entry.attempts >= self._max_attempts:
                entry.severed = "quota"
                logger.warning(
                    "[MicroFixGovernor] op=%s file=%s spent %d/%d attempts "
                    "without converging — severing",
                    op_id, file_path, entry.attempts, self._max_attempts,
                )
                return QuotaVerdict(
                    False, "quota_exhausted", entry.attempts, self._max_attempts,
                )
            entry.attempts += 1
            return QuotaVerdict(
                True, "admitted", entry.attempts, self._max_attempts,
            )

    async def observe(
        self, *, op_id: str, file_path: str, content: str,
    ) -> QuotaVerdict:
        """Record the text a micro-fix produced, and detect a cycle.

        Severs when the same content is produced repeatedly: the model is
        not converging, and every further iteration pays full price for a
        result already seen.
        """
        key = (op_id, file_path)
        fingerprint = content_fingerprint(content)
        async with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            stuck = self._detector.observe(
                f"microfix::{op_id}::{file_path}", fingerprint,
            )
            if stuck and not entry.severed:
                entry.severed = "no_progress"
                logger.warning(
                    "[MicroFixGovernor] op=%s file=%s re-emitted identical "
                    "content — severing as non-converging",
                    op_id, file_path,
                )
            return QuotaVerdict(
                not entry.severed,
                f"severed:{entry.severed}" if entry.severed else "progressing",
                entry.attempts, self._max_attempts,
            )

    async def observe_regression(
        self, *, op_id: str, file_path: str, before: str, after: str,
    ) -> QuotaVerdict:
        """Sever when a fix breaks a file that previously parsed.

        The loop's own edit then authors every later traceback, so it spends
        the remaining budget chasing damage it caused. Only a clean->broken
        transition counts: a file that was already unparseable is precisely
        what the micro-fix is for, and a non-Python file is not judged at
        all.
        """
        key = (op_id, file_path)
        was_clean = parses_cleanly(before, file_path)
        now_clean = parses_cleanly(after, file_path)
        async with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            if was_clean is True and now_clean is False and not entry.severed:
                entry.severed = "regression"
                logger.warning(
                    "[MicroFixGovernor] op=%s file=%s — the fix introduced a "
                    "syntax error into a file that parsed; severing before "
                    "the loop starts repairing its own damage",
                    op_id, file_path,
                )
            return QuotaVerdict(
                not entry.severed,
                f"severed:{entry.severed}" if entry.severed else "no_regression",
                entry.attempts, self._max_attempts,
            )

    async def release(self, *, op_id: str, file_path: str) -> None:
        """Forget an entry — the op reached a terminal state. NEVER raises."""
        async with self._lock:
            self._entries.pop((op_id, file_path), None)
        try:
            self._detector.finish(f"microfix::{op_id}::{file_path}")
        except Exception:  # noqa: BLE001
            logger.debug("[MicroFixGovernor] detector finish degraded", exc_info=True)

    async def state(self, *, op_id: str, file_path: str) -> QuotaVerdict:
        """Current accounting without claiming anything."""
        async with self._lock:
            entry = self._entries.get((op_id, file_path))
            if entry is None:
                return QuotaVerdict(True, "unseen", 0, self._max_attempts)
            return QuotaVerdict(
                not entry.severed,
                f"severed:{entry.severed}" if entry.severed else "active",
                entry.attempts, self._max_attempts,
            )


_default: Optional[MicroFixQuotaGovernor] = None


def default_governor() -> MicroFixQuotaGovernor:
    """The process-wide governor for callers with no instance to thread.

    Built on first use rather than at import so the env is read after the
    process has finished loading its configuration.
    """
    global _default  # noqa: PLW0603
    if _default is None:
        _default = MicroFixQuotaGovernor()
    return _default


__all__ = [
    "MicroFixExhaustionFault",
    "MicroFixQuotaGovernor",
    "QuotaVerdict",
    "content_fingerprint",
    "default_governor",
    "parses_cleanly",
]
