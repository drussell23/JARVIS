"""Transactional-lifecycle guard for the APPLY/VERIFY phase runner.

A durability-substrate primitive. When the runner records the terminal
``APPLIED`` ledger row for an op, the file mutation is already on disk and the
op is inside a transactional span whose ONLY legitimate exits are:

* reaching Phase 8b (auto-commit), or
* a governed rollback / gate-failure that CLOSES the transaction (records a
  ``FAILED`` ledger row, restores the pre-apply snapshot, publishes an
  outcome).

The failure this module makes impossible is the *silent third path*: an op
that records ``APPLIED`` (mutation on disk, VERIFY green) yet exits the runner
without ever reaching auto-commit and without any terminal signal -- no commit
attempt, no exception, no log. That op simply vanishes; the ledger claims
``APPLIED`` while nothing downstream ever learns the transaction closed.

:class:`TransactionLifecycleError` converts that silence into a loud failure.
The guard is enforced in a ``finally`` in the runner: it fires a
``logger.critical`` ALWAYS, and raises this exception ONLY when the span is
exiting without an in-flight exception (so a ``CancelledError`` or any other
live exception propagates UNCONVERTED -- the guard never masks it).
"""

from __future__ import annotations

import os


class TransactionLifecycleError(RuntimeError):
    """An op recorded APPLIED but exited the runner before auto-commit.

    Raised only on the *silent* exit path (no in-flight exception). Carries the
    ``op_id`` and the transactional ``boundary`` that was breached so the
    failure is auditable.
    """

    def __init__(self, op_id: str, boundary: str) -> None:
        self.op_id = op_id
        self.boundary = boundary
        super().__init__(
            "op={op} recorded APPLIED but never reached transactional "
            "boundary '{boundary}' -- silent post-apply exit".format(
                op=op_id, boundary=boundary
            )
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            "TransactionLifecycleError(op_id={op!r}, boundary={boundary!r}): "
            "recorded APPLIED but never reached '{boundary}'".format(
                op=self.op_id, boundary=self.boundary
            )
        )


def txn_lifecycle_guard_enabled() -> bool:
    """Return whether the transactional-lifecycle guard is armed.

    Env ``JARVIS_TXN_LIFECYCLE_GUARD_ENABLED``, default ``true``. Read at call
    time (never cached) so the kill switch is live. NEVER raises: any parsing
    surprise resolves to the default-on posture.
    """
    try:
        raw = os.environ.get("JARVIS_TXN_LIFECYCLE_GUARD_ENABLED", "true")
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001 - a gate must never raise
        return True


__all__ = ["TransactionLifecycleError", "txn_lifecycle_guard_enabled"]
