"""transcript_milestones — APPLY / VERIFY / commit, in the one transcript.

Step 6 of the transcript durability arc.

Why these belong in the spine
-----------------------------

The spine's premise is that "what happened next" must have an answer
across vocabularies. Diffs, tool bodies, op blocks and narrative already
share its order. The three facts an operator most often needs to place
in time — *this was applied*, *its tests passed*, *it was committed* —
did not: they went to ``SessionRecorder`` and ended up in
``summary.json``, ordered only relative to each other.

So a transcript could show the diff ``d-7`` and the tool calls around
it, and still have no position for "and then it was committed". This
module gives that fact a ``seq`` alongside everything else.

Reusing the fan-out rather than adding one
------------------------------------------

``ops_digest_observer`` already carries exactly these three callbacks to
exactly the right places (``orchestrator`` at APPLY/VERIFY,
``AutoCommitter`` at commit). It holds one primary slot — owned by
``SessionRecorder`` — plus an additive listener list precisely so a
second consumer need not displace the first. :func:`install` subscribes
there. No new eventing, no second emit site, and nothing about session
recording changes.

One path to disk
----------------

This module writes to the **spine**, not to the log. Persistence is the
spine's sink (slice 2, see :mod:`transcript_writer`), so milestones
become durable through the same single path as every other record —
rather than acquiring a second writer with its own ordering, its own
failure modes, and its own opinion about ``seq``.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.TranscriptMilestones")


__all__ = [
    "MILESTONE_BUFFER_SIZE_ENV_VAR",
    "REF_PREFIX",
    "TranscriptMilestoneListener",
    "install",
    "uninstall",
]


#: Discovered by ``transcript_spine.known_prefixes()``, which reads this
#: name off the module rather than restating it — the fifth vocabulary.
REF_PREFIX: str = "m-"

#: Read by ``transcript_spine._store_capacity`` through the same
#: ``*_SIZE_ENV_VAR`` / ``_DEFAULT_*_SIZE`` convention the other four
#: stores publish. A producer that added records without adding budget
#: would make the spine evict sooner than the union it promises.
MILESTONE_BUFFER_SIZE_ENV_VAR: str = "JARVIS_MILESTONE_BUFFER_SIZE"
_DEFAULT_MILESTONE_BUFFER_SIZE: int = 60

MILESTONE_APPLY: str = "apply"
MILESTONE_VERIFY: str = "verify"
MILESTONE_COMMIT: str = "commit"

_KIND: str = "milestone"

#: Mirrors ``ops_digest_observer``'s own ingest caps, so a pathological
#: op_id cannot reach the log through this door either.
_MAX_OP_ID_LEN: int = 64
_MAX_HASH_LEN: int = 40


class TranscriptMilestoneListener:
    """An ``OpsDigestObserver`` that files each milestone on the spine.

    Best-effort and non-raising, per the observer protocol: a telemetry
    listener must never be able to fail the op it is observing. Its own
    failures are counted rather than logged-and-forgotten, so "no
    milestones in the transcript" can be told apart from "no milestones
    happened".
    """

    def __init__(self, spine: Optional[Any] = None) -> None:
        self._spine = spine
        self._lock = threading.Lock()
        self._n = 0
        self.recorded = 0
        self.failures = 0

    # ---- internals ----------------------------------------------------

    def _resolve_spine(self) -> Optional[Any]:
        if self._spine is not None:
            return self._spine
        try:
            from backend.core.ouroboros.battle_test.transcript_spine import (
                get_default_spine,
            )
            return get_default_spine()
        except Exception:  # noqa: BLE001
            return None

    def _file(self, event: str, op_id: str, payload: Dict[str, Any]) -> None:
        try:
            spine = self._resolve_spine()
            if spine is None:
                return
            with self._lock:
                self._n += 1
                ref = f"{REF_PREFIX}{self._n}"
            body = {"event": event, **payload}
            spine.append(
                _KIND, ref, payload=body,
                op_id=str(op_id or "")[:_MAX_OP_ID_LEN],
            )
            self.recorded += 1
        except Exception:  # noqa: BLE001
            self.failures += 1
            logger.debug("[Milestones] %s degraded", event, exc_info=True)

    # ---- OpsDigestObserver protocol ------------------------------------

    def on_apply_succeeded(self, *, op_id: str, mode: str, files: int) -> None:
        self._file(
            MILESTONE_APPLY, op_id,
            {"mode": str(mode or ""), "files": int(files or 0)},
        )

    def on_verify_completed(
        self,
        *,
        op_id: str,
        passed: int,
        total: int,
        scoped_to_applied_op: bool = True,
    ) -> None:
        self._file(
            MILESTONE_VERIFY, op_id,
            {
                "passed": int(passed or 0),
                "total": int(total or 0),
                # Carried verbatim: "12/12 passed" means something
                # different when the counts are repo-wide health rather
                # than this op's tests, and dropping the qualifier here
                # would make the transcript overstate the evidence.
                "scoped_to_applied_op": bool(scoped_to_applied_op),
            },
        )

    def on_commit_succeeded(self, *, op_id: str, commit_hash: str) -> None:
        self._file(
            MILESTONE_COMMIT, op_id,
            {"commit": str(commit_hash or "")[:_MAX_HASH_LEN]},
        )


_INSTALLED: Optional[TranscriptMilestoneListener] = None
_INSTALL_LOCK = threading.Lock()


def install(spine: Optional[Any] = None) -> Optional[TranscriptMilestoneListener]:
    """Subscribe to the ops-digest fan-out. Idempotent. NEVER raises."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED is not None:
            return _INSTALLED
        try:
            from backend.core.ouroboros.governance.ops_digest_observer import (
                add_ops_digest_listener,
            )
        except ImportError:
            logger.debug("[Milestones] ops-digest fan-out unavailable")
            return None
        listener = TranscriptMilestoneListener(spine)
        add_ops_digest_listener(listener)
        _INSTALLED = listener
        logger.info("[Milestones] APPLY/VERIFY/commit now share the spine order")
        return listener


def uninstall() -> None:
    """Unsubscribe. For tests and symmetric teardown. NEVER raises."""
    global _INSTALLED
    with _INSTALL_LOCK:
        listener, _INSTALLED = _INSTALLED, None
        if listener is None:
            return
        try:
            from backend.core.ouroboros.governance.ops_digest_observer import (
                remove_ops_digest_listener,
            )
            remove_ops_digest_listener(listener)
        except Exception:  # noqa: BLE001
            logger.debug("[Milestones] uninstall degraded", exc_info=True)
