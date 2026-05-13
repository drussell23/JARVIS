"""
Park Signal — Stage 1.6 substrate
==================================

Frozen dataclass that GENERATE returns when it wishes to release its
BG worker slot for the duration of provider I/O.  The BG pool
``_worker_loop`` recognizes a :class:`ParkSignal` return from
``orch.run(ctx)`` and:

  1. persists ``OperationState.PARKED_GENERATE`` to the ledger,
  2. frees the worker slot (returns from one loop iteration),
  3. schedules an out-of-pool task that fulfils the parked descriptor
     via :class:`~backend.core.ouroboros.governance.op_park_store.ParkedOpStore`,
  4. on fulfilment, re-submits ``ctx`` to the BG queue with
     ``resumed=True`` so GENERATE picks up post-provider work.

Why a sentinel, not an exception
--------------------------------
Park is **not** a failure.  Returning a sentinel keeps the BG pool's
existing try/except shape (TimeoutError → ``bg_timebox``,
CancelledError → cancelled, BaseException → failed) byte-identical at
runtime when the master flag is off, and avoids piping a new exception
class through the orchestrator's many phase-runners.

Why frozen
----------
The signal is read by the pool worker after the orchestrator returns;
mutation between produce and consume would be a contract violation.
Freezing matches the §33.5 dataclass discipline used across the
SWE-Bench-Pro arc (EvaluationResult, ScoringResult, ReportCard).

Authority invariant
-------------------
This module imports only ``dataclasses`` + ``typing``.  It carries no
authority — the BG pool decides whether to honor a park signal; the
park store decides whether to admit one; the ledger decides whether to
persist it.  AST-pinned at Slice 1 spine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ParkDescriptor:
    """Opaque-to-the-pool description of the pending provider call.

    The descriptor is produced by the GENERATE phase wrapper (Slice 2)
    and handed to :class:`~op_park_store.ParkedOpStore`.  The pool does
    not introspect it; only the resume continuation does.

    Parameters
    ----------
    kind:
        Free-form tag identifying the parked work (``"generate"`` for
        the canonical Slice 2 site; reserved for future use elsewhere).
    payload:
        Arbitrary mapping carrying whatever the resume continuation
        needs (prompt, route, deadline, tool-loop state).  Treated as
        an opaque envelope by everything except the GENERATE wrapper.
    """

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParkSignal:
    """Sentinel returned from ``orch.run(ctx)`` when the op parks.

    Parameters
    ----------
    op_id:
        The ``OperationContext.op_id`` of the parking op.  The pool
        uses this to write the ``PARKED_GENERATE`` ledger entry under
        the same id the op was dispatched with — identity preservation
        invariant (§1.6 spike).
    token:
        Single-flight key produced by :class:`ParkedOpStore.park` —
        ``"<op_id>::attempt-<n>"``.  Used by the resume continuation
        to look up the descriptor and signal completion.
    attempt_seq:
        Monotonic attempt counter (1 for first GENERATE, 2+ for
        GENERATE_RETRY cycles).  Mirrored into the ledger
        ``entry_id`` so multiple park records under one op_id coexist.
    descriptor:
        The opaque :class:`ParkDescriptor` produced by the GENERATE
        wrapper.
    park_started_at:
        Monotonic timestamp at park emission.  Used by the TTL reaper
        to age out parks whose resume continuation never fires (e.g.
        provider task died before completing).
    """

    op_id: str
    token: str
    attempt_seq: int
    descriptor: ParkDescriptor
    park_started_at: float
