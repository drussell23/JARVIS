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
from typing import Literal, Optional, Protocol

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

    def on_op_classified(
        self,
        *,
        op_id: str,
        signal_source: str,
        urgency: str,
        risk_tier: str,
    ) -> None:
        """The op's causal origin became known at the INTENT seam.

        Additive (PRD §42 Slice 2): carries the one causal edge the
        other three callbacks structurally cannot — *signal → op*. It
        flows through THIS canonical telemetry seam (not a parallel
        op_id→envelope registry) so the OperationTimeline read-model
        can complete the causal join without any new authority or
        duplicated state. Default-noop in legacy implementers — only
        the timeline consumes it; SessionRecorder ignores it.
        """


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

    def on_op_classified(
        self,
        *,
        op_id: str,
        signal_source: str,
        urgency: str,
        risk_tier: str,
    ) -> None:
        return


class _CompositeOpsDigestObserver:
    """Fan-out observer (PRD §42 Slice 2 — the root fix for coexistence).

    The module-global pointer is single-slot: a naive
    ``register_ops_digest_observer(timeline)`` would EVICT the harness's
    ``SessionRecorder`` and silently break ``LastSessionSummary``. The
    correct, non-workaround composition is a multiplexing observer that
    forwards every protocol method to an ordered list of members, each
    call defensively isolated so a misbehaving member cannot starve the
    others. This composes the existing single seam — it does NOT add a
    parallel registry. ``register_/get_/reset_`` semantics are
    untouched; :func:`add_ops_digest_observer` transparently wraps the
    current observer into one of these.

    Members are deduplicated by identity (idempotent ``add``). Every
    forwarded call is wrapped in try/except per the protocol's
    fail-closed contract.
    """

    def __init__(self) -> None:
        self._members: list = []
        self._members_lock = threading.Lock()

    # -- membership ----------------------------------------------------

    def add(self, observer: object) -> None:
        if observer is None:
            return
        with self._members_lock:
            if any(m is observer for m in self._members):
                return  # idempotent — never double-register by identity
            self._members.append(observer)

    def remove(self, observer: object) -> None:
        with self._members_lock:
            self._members = [m for m in self._members if m is not observer]

    def members(self) -> tuple:
        with self._members_lock:
            return tuple(self._members)

    def __len__(self) -> int:
        with self._members_lock:
            return len(self._members)

    # -- fan-out (each member isolated) --------------------------------

    def _fan_out(self, method_name: str, **kwargs: object) -> None:
        for member in self.members():
            try:
                getattr(member, method_name)(**kwargs)
            except Exception:  # noqa: BLE001 — one member must not
                # starve the others; protocol is fire-and-forget.
                logger.debug(
                    "[OpsDigest] composite member %r raised on %s",
                    type(member).__name__, method_name, exc_info=True,
                )

    def on_apply_succeeded(self, *, op_id: str, mode: str, files: int) -> None:
        self._fan_out(
            "on_apply_succeeded", op_id=op_id, mode=mode, files=files,
        )

    def on_verify_completed(
        self,
        *,
        op_id: str,
        passed: int,
        total: int,
        scoped_to_applied_op: bool = True,
    ) -> None:
        self._fan_out(
            "on_verify_completed",
            op_id=op_id,
            passed=passed,
            total=total,
            scoped_to_applied_op=scoped_to_applied_op,
        )

    def on_commit_succeeded(self, *, op_id: str, commit_hash: str) -> None:
        self._fan_out(
            "on_commit_succeeded", op_id=op_id, commit_hash=commit_hash,
        )

    def on_op_classified(
        self,
        *,
        op_id: str,
        signal_source: str,
        urgency: str,
        risk_tier: str,
    ) -> None:
        self._fan_out(
            "on_op_classified",
            op_id=op_id,
            signal_source=signal_source,
            urgency=urgency,
            risk_tier=risk_tier,
        )


_OBSERVER_LOCK = threading.Lock()
_OBSERVER: OpsDigestObserver = _NoopObserver()


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
    """Return the currently-registered observer (never ``None``)."""
    with _OBSERVER_LOCK:
        return _OBSERVER


def reset_ops_digest_observer() -> None:
    """Restore the default no-op observer. Primarily for tests."""
    register_ops_digest_observer(None)


def add_ops_digest_observer(observer: Optional[OpsDigestObserver]) -> None:
    """Add ``observer`` to the fan-out set (PRD §42 Slice 2).

    Unlike :func:`register_ops_digest_observer` (single-slot SET — kept
    byte-identical for back-compat: the harness still registers
    SessionRecorder that way), this COMPOSES: the current observer is
    transparently wrapped into a :class:`_CompositeOpsDigestObserver`
    so multiple consumers (SessionRecorder + the OperationTimeline
    read-model) coexist. Idempotent by identity. NEVER raises — a
    telemetry-wiring failure must never derail boot.

    Order of operations is robust to any boot sequence: calling
    ``add`` before or after ``register`` both converge on a composite
    containing every distinct member; a plain ``_NoopObserver`` is
    discarded (it carries no state) rather than fanned to.
    """
    if observer is None:
        return
    try:
        with _OBSERVER_LOCK:
            global _OBSERVER
            current = _OBSERVER
            if isinstance(current, _CompositeOpsDigestObserver):
                current.add(observer)
                return
            composite = _CompositeOpsDigestObserver()
            # Preserve any real existing observer; a bare no-op carries
            # no state and is intentionally dropped from the fan-out.
            if not isinstance(current, _NoopObserver):
                composite.add(current)
            composite.add(observer)
            _OBSERVER = composite
    except Exception:  # noqa: BLE001 — wiring must never crash boot
        logger.debug(
            "[OpsDigest] add_ops_digest_observer failed", exc_info=True,
        )


def remove_ops_digest_observer(observer: Optional[OpsDigestObserver]) -> None:
    """Remove ``observer`` from the fan-out set. If the composite
    empties, the default :class:`_NoopObserver` is restored. NEVER
    raises. Primarily for tests / clean teardown."""
    if observer is None:
        return
    try:
        with _OBSERVER_LOCK:
            global _OBSERVER
            current = _OBSERVER
            if not isinstance(current, _CompositeOpsDigestObserver):
                return
            current.remove(observer)
            if len(current) == 0:
                _OBSERVER = _NoopObserver()
            elif len(current) == 1:
                # Collapse a single-member composite back to the bare
                # observer — keeps the common path allocation-free.
                _OBSERVER = current.members()[0]
    except Exception:  # noqa: BLE001
        logger.debug(
            "[OpsDigest] remove_ops_digest_observer failed", exc_info=True,
        )
