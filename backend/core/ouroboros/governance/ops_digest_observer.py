"""OpsDigestObserver — stable hook contract for session-digest event reporting.

Provides a process-global observer protocol that orchestrator / AutoCommitter
callers invoke when APPLY / VERIFY / commit milestones succeed. A concrete
implementer (typically :class:`backend.core.ouroboros.battle_test.session_recorder.SessionRecorder`)
registers at harness boot; orchestrator/AutoCommitter never import the recorder
directly — they only know this observer hook.

Design (mirrors ``register_protected_path_provider`` in
``conversation_bridge``):
  * Module-global ``_OBSERVER`` pointer, guarded by a lock.
  * :func:`get_ops_digest_observer` returns a :class:`NoopObserver`
    when none is registered — callers can invoke methods unconditionally
    without a None check.
  * Observer methods are **never** expected to raise; callers still wrap
    them in ``try/except`` defensively because a misbehaving observer
    must not derail APPLY / VERIFY / commit flows.

Scope contract (Manifesto §1, §8):
  The digest observer is a telemetry-only surface. No observer method
  has any authority over op outcomes, routing, gating, or policy. It
  exists purely to let the harness record what the session did so
  :class:`backend.core.ouroboros.governance.last_session_summary.LastSessionSummary`
  can render quote-quality facts (``apply=multi/4 verify=20/20 commit=...``)
  in the next session's CONTEXT_EXPANSION.

Authority invariant (same §9 pattern as the rest of the prompt-surface stack):
  Observer notifications are consumed by the session recorder only.
  The data round-trips into ``summary.json``, gets read by
  ``LastSessionSummary`` next session, and lands in ``strategic_memory_prompt``
  at CONTEXT_EXPANSION. **Zero authority** over Iron Gate, UrgencyRouter,
  risk tier, policy, FORBIDDEN_PATH, or approval gating.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, List, Literal, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

# Apply-mode enum values. Keep as string constants (not an enum class) so
# callers can pass the literal "single" / "multi" / "none" strings without
# importing an enum. Deterministic and greppable.
APPLY_MODE_NONE = "none"
APPLY_MODE_SINGLE = "single"
APPLY_MODE_MULTI = "multi"

ApplyMode = Literal["none", "single", "multi"]

# Sanity caps for ingested data — defends against pathologically long
# op_ids or commit hashes sneaking into summary.json. Enforcement lives
# in the recorder, not here; constants are shared for test parity.
MAX_OP_ID_LEN = 64
MAX_COMMIT_HASH_LEN = 40


class OpsDigestObserver(Protocol):
    """Stable contract for session-digest milestone reporting.

    All methods are **best-effort fire-and-forget**. Implementations MUST
    NOT raise; if a method does raise, callers are wrapped in try/except
    and swallow silently — but a raising observer indicates a bug in the
    implementer, not the caller.
    """

    def on_apply_succeeded(
        self, *, op_id: str, mode: str, files: int,
    ) -> None:
        """An APPLY phase concluded with ``DECISION outcome=applied``.

        Parameters
        ----------
        op_id : str
            Op identifier (truncated / sanitized by the implementer).
        mode : str
            One of ``APPLY_MODE_NONE``, ``APPLY_MODE_SINGLE``,
            ``APPLY_MODE_MULTI``. Unknown values are coerced to
            ``APPLY_MODE_NONE`` by the implementer.
        files : int
            Count of files affected by this APPLY. ``0`` when unknown.
        """

    def on_verify_completed(
        self,
        *,
        op_id: str,
        passed: int,
        total: int,
        scoped_to_applied_op: bool = True,
    ) -> None:
        """A VERIFY phase finished with test counts for the APPLY just made.

        Parameters
        ----------
        scoped_to_applied_op : bool
            ``True`` when the counts cover tests tied to the applied op
            (not repo-wide health). Plan tightening #1 requires this
            flag to be honest; implementers may ignore-or-pass-through.
        """

    def on_commit_succeeded(
        self, *, op_id: str, commit_hash: str,
    ) -> None:
        """AutoCommitter published a commit for ``op_id``. Hash may be shortened."""


class _NoopObserver:
    """Default registered observer — silently drops every call.

    Used when no harness is running (e.g., unit tests that don't boot
    the battle-test stack, or daemon restarts where registration is
    pending). Prevents call sites from needing ``if observer is None``
    guards.
    """

    def on_apply_succeeded(self, *, op_id: str, mode: str, files: int) -> None:
        return

    def on_verify_completed(
        self,
        *,
        op_id: str,
        passed: int,
        total: int,
        scoped_to_applied_op: bool = True,
    ) -> None:
        return

    def on_commit_succeeded(self, *, op_id: str, commit_hash: str) -> None:
        return


class _FanOut:
    """Delivers every callback to the primary observer and each listener.

    Why this exists
    ---------------
    The registry held ONE slot, and the harness's ``SessionRecorder`` owns
    it. Any second consumer of APPLY/VERIFY/commit telemetry therefore had
    exactly two options: displace session recording, or duplicate the call at
    every emitting site. Both are the same defect — a single-slot registry is
    the reason a second consumer cannot exist — so the slot became a list.

    ``register_ops_digest_observer`` keeps its exact meaning (set the
    primary). Additive subscribers use :func:`add_ops_digest_listener`.

    Isolation: a listener that raises is logged and skipped. A telemetry
    observer must never be able to fail the op it is observing, and one
    listener must never be able to starve another.
    """

    def __init__(self, primary: "OpsDigestObserver",
                 listeners: Sequence["OpsDigestObserver"]) -> None:
        self._targets = [primary, *listeners]

    def _dispatch(self, name: str, **kwargs: Any) -> None:
        for target in self._targets:
            try:
                getattr(target, name)(**kwargs)
            except Exception:  # noqa: BLE001
                logger.debug("[OpsDigest] %s listener raised", name,
                             exc_info=True)

    def on_apply_succeeded(self, *, op_id: str, mode: str, files: int) -> None:
        self._dispatch("on_apply_succeeded", op_id=op_id, mode=mode,
                       files=files)

    def on_verify_completed(
        self, *, op_id: str, passed: int, total: int,
        scoped_to_applied_op: bool = True,
    ) -> None:
        self._dispatch("on_verify_completed", op_id=op_id, passed=passed,
                       total=total, scoped_to_applied_op=scoped_to_applied_op)

    def on_commit_succeeded(self, *, op_id: str, commit_hash: str) -> None:
        self._dispatch("on_commit_succeeded", op_id=op_id,
                       commit_hash=commit_hash)


_OBSERVER_LOCK = threading.Lock()
_OBSERVER: OpsDigestObserver = _NoopObserver()
_LISTENERS: List[OpsDigestObserver] = []


def add_ops_digest_listener(listener: OpsDigestObserver) -> None:
    """Subscribe *listener* additively, alongside the primary observer.

    Idempotent by identity, so a module that arms itself lazily on several
    code paths cannot register itself twice and double-count.
    """
    global _LISTENERS  # noqa: PLW0603
    with _OBSERVER_LOCK:
        if not any(existing is listener for existing in _LISTENERS):
            _LISTENERS.append(listener)


def remove_ops_digest_listener(listener: OpsDigestObserver) -> None:
    """Unsubscribe *listener*. Silent when it was never registered."""
    global _LISTENERS  # noqa: PLW0603
    with _OBSERVER_LOCK:
        _LISTENERS = [x for x in _LISTENERS if x is not listener]


def register_ops_digest_observer(observer: Optional[OpsDigestObserver]) -> None:
    """Install (or clear) the process-global ops-digest observer.

    Passing ``None`` restores the default :class:`_NoopObserver`. The
    battle-test harness registers its :class:`SessionRecorder` at boot
    and clears at teardown, so downstream flows see a real observer
    only during a live session.
    """
    global _OBSERVER
    with _OBSERVER_LOCK:
        _OBSERVER = observer if observer is not None else _NoopObserver()


def get_ops_digest_observer() -> OpsDigestObserver:
    """Return the observer call sites should notify (never ``None``).

    With no additive listeners this returns the primary object ITSELF, not a
    wrapper — so ``get_ops_digest_observer() is my_recorder`` stays true for
    every existing caller and test. The fan-out proxy appears only once a
    second consumer actually exists, which is the only moment it is needed.
    """
    with _OBSERVER_LOCK:
        if not _LISTENERS:
            return _OBSERVER
        return _FanOut(_OBSERVER, tuple(_LISTENERS))


def reset_ops_digest_observer() -> None:
    """Restore the default no-op observer and drop listeners. For tests."""
    global _LISTENERS  # noqa: PLW0603
    register_ops_digest_observer(None)
    with _OBSERVER_LOCK:
        _LISTENERS = []
