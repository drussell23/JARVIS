"""Regression spine for the ``UnifiedIntakeRouter`` heap tie-break.

Pins the structural invariant that prevents priority-queue
ordering corruption under collision: every enqueue onto
``self._queue`` (asyncio.PriorityQueue) MUST carry a
strictly-monotonic ``tie_seq`` int in the third tuple position,
so heapq comparison NEVER falls through to ``IntentEnvelope``.

The failure mode locked out here was observed 2026-05-12 in
stage-1 wiring soak (session ``bt-2026-05-13-051420``): a
priority-2 ``swe_bench_pro`` envelope sat in the queue for 14+
minutes while priority-7 / priority-99 envelopes dispatched
ahead of it.  Root cause: ``IntentEnvelope`` is a frozen
dataclass with no ``__lt__``.  When two enqueues collide on the
(priority, submitted_at) prefix, heapq's tuple comparison
reaches the envelope and raises
``TypeError: '<' not supported between instances of 'IntentEnvelope'``.
The exception bubbles out of ``await queue.put(...)`` mid-mutation,
leaving the binary heap invariant violated.  Subsequent dequeues
no longer honor priority ordering.

Two layers of pins:

1. **Structural (AST)** — every ``self._queue.put`` /
   ``self._queue.put_nowait`` call in
   ``unified_intake_router.py`` MUST construct a 4-tuple whose
   third positional element is ``next(_HEAP_TIE_SEQ)``.  A new
   enqueue site that forgets the tie-break fails the test at
   CI time instead of silently corrupting the heap.

2. **Behavioral (runtime burst)** — 100+ enqueues with
   IDENTICAL ``(priority, submitted_at)`` prefixes complete
   without raising AND dequeue in FIFO order among ties (the
   ``tie_seq`` monotonic counter guarantees this).

The deliberate non-choice: we do NOT add ``__lt__`` to
``IntentEnvelope``.  Per the 2026-05-12 operator binding,
``__lt__ returns False`` is not a strict weak order and falls
back on object-id / implementation-defined tie behavior.  The
load-bearing surface is the tuple prefix at the queue seam —
envelope stays inert for ordering.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import itertools
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake import unified_intake_router
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    _HEAP_TIE_SEQ,
)


_ROUTER_SRC = Path(
    inspect.getfile(unified_intake_router),
).read_text(encoding="utf-8")
_ROUTER_AST = ast.parse(_ROUTER_SRC)


# ---------------------------------------------------------------------------
# AST pins — every enqueue site uses the 4-tuple shape
# ---------------------------------------------------------------------------


def _find_queue_put_calls() -> list:
    """Return every Call node whose func is ``self._queue.put`` or
    ``self._queue.put_nowait``."""
    matches = []
    for node in ast.walk(_ROUTER_AST):
        if not isinstance(node, ast.Call):
            continue
        # func should be Attribute(attr='put'|'put_nowait',
        # value=Attribute(attr='_queue', value=Name('self')))
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("put", "put_nowait"):
            continue
        inner = func.value
        if not isinstance(inner, ast.Attribute):
            continue
        if inner.attr != "_queue":
            continue
        if not isinstance(inner.value, ast.Name) or inner.value.id != "self":
            continue
        matches.append(node)
    return matches


def test_heap_tie_seq_is_itertools_count_at_module_scope():
    """``_HEAP_TIE_SEQ`` must be a module-level ``itertools.count()``
    so every enqueue site shares the same monotonic counter."""
    assert isinstance(_HEAP_TIE_SEQ, type(itertools.count())), (
        f"_HEAP_TIE_SEQ is {type(_HEAP_TIE_SEQ).__name__}, expected "
        "itertools.count.  The tie-break invariant requires a single "
        "shared monotonic counter — per-instance / per-call counters "
        "would reset and re-collide on (priority, submitted_at, 0)."
    )
    # Confirm it actually advances
    n1 = next(_HEAP_TIE_SEQ)
    n2 = next(_HEAP_TIE_SEQ)
    assert n2 > n1, (
        f"_HEAP_TIE_SEQ is not strictly monotonic: {n1} -> {n2}"
    )


def test_every_queue_put_site_uses_4_tuple_shape():
    """AST pin: every ``self._queue.put(...)`` /
    ``self._queue.put_nowait(...)`` call MUST pass a 4-tuple.

    A 3-tuple (priority, submitted_at, envelope) would let heapq
    fall through to IntentEnvelope comparison and raise TypeError
    on collision (the v2 failure mode).
    """
    put_calls = _find_queue_put_calls()
    assert put_calls, (
        "Found ZERO self._queue.put / put_nowait calls in router source. "
        "Either the router was refactored away from this seam (in which "
        "case this test needs updating) or grep missed them (which the "
        "test would silently no-op against)."
    )
    bad_sites = []
    for call in put_calls:
        # The argument is a Tuple ast node.  Note: put takes ONE positional
        # arg (the item tuple), so call.args has length 1.
        if len(call.args) != 1:
            bad_sites.append(
                f"line {call.lineno}: {ast.unparse(call)[:120]} "
                f"(expected 1 positional arg, got {len(call.args)})"
            )
            continue
        item = call.args[0]
        if not isinstance(item, ast.Tuple):
            bad_sites.append(
                f"line {call.lineno}: argument is not a Tuple literal "
                f"({ast.unparse(call)[:120]})"
            )
            continue
        if len(item.elts) != 4:
            bad_sites.append(
                f"line {call.lineno}: tuple has {len(item.elts)} elements, "
                f"expected 4 (priority, submitted_at, tie_seq, envelope): "
                f"{ast.unparse(call)[:120]}"
            )
    assert not bad_sites, (
        "Some self._queue.put sites are NOT using the 4-tuple "
        "(priority, submitted_at, tie_seq, envelope) shape — this "
        "re-introduces the IntentEnvelope tie-break TypeError that "
        "corrupted the heap during 2026-05-12 wiring soak.  Offenders:\n"
        + "\n".join(f"  - {s}" for s in bad_sites)
    )


def test_every_queue_put_site_uses_heap_tie_seq():
    """AST pin: the third tuple element at every enqueue site MUST
    be a Call to ``next(_HEAP_TIE_SEQ)``.

    A literal int or a fresh local counter would defeat the
    invariant — only the shared module-level counter guarantees
    strict monotonicity across all enqueue sites (ingest, retry,
    WAL replay).
    """
    put_calls = _find_queue_put_calls()
    bad_sites = []
    for call in put_calls:
        if len(call.args) != 1:
            continue
        item = call.args[0]
        if not isinstance(item, ast.Tuple) or len(item.elts) != 4:
            continue
        third = item.elts[2]
        # Expected: Call(func=Name('next'), args=[Name('_HEAP_TIE_SEQ')])
        is_next_call = (
            isinstance(third, ast.Call)
            and isinstance(third.func, ast.Name)
            and third.func.id == "next"
            and len(third.args) == 1
            and isinstance(third.args[0], ast.Name)
            and third.args[0].id == "_HEAP_TIE_SEQ"
        )
        if not is_next_call:
            bad_sites.append(
                f"line {call.lineno}: third tuple element is "
                f"{ast.unparse(third)!r}, expected "
                "'next(_HEAP_TIE_SEQ)'"
            )
    assert not bad_sites, (
        "Some self._queue.put sites are NOT using next(_HEAP_TIE_SEQ) "
        "as the tie-break field — drift would re-introduce heap "
        "corruption under collision.  Offenders:\n"
        + "\n".join(f"  - {s}" for s in bad_sites)
    )


def test_dequeue_site_unpacks_4_tuple():
    """The single legacy-path dequeue site in ``_dispatch_loop`` MUST
    unpack the 4-tuple shape — a 3-tuple unpack would raise
    ValueError at runtime once the heap actually has items."""
    found_4_tuple_unpack = False
    for node in ast.walk(_ROUTER_AST):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Tuple):
            continue
        # Look for `priority, ts, _tie_seq, envelope = await asyncio.wait_for(...)`
        if len(tgt.elts) != 4:
            continue
        names = [
            e.id for e in tgt.elts if isinstance(e, ast.Name)
        ]
        if (
            "priority" in names
            and "envelope" in names
            and any("tie_seq" in n for n in names)
        ):
            found_4_tuple_unpack = True
            break
    assert found_4_tuple_unpack, (
        "Did not find the 4-tuple unpack "
        "(priority, ts, _tie_seq, envelope) at the legacy-path "
        "dequeue site.  A 3-tuple unpack would raise ValueError at "
        "runtime once the queue is non-empty — guarding against drift "
        "back to the 3-tuple shape on either side of the seam."
    )


# ---------------------------------------------------------------------------
# Behavioral burst — 100+ collisions complete cleanly + FIFO preserved
# ---------------------------------------------------------------------------


def _mk_envelope(causal: str) -> IntentEnvelope:
    """Construct a minimal valid envelope.  All fields static EXCEPT
    causal_id (so we can verify FIFO ordering downstream)."""
    return IntentEnvelope(
        schema_version="2c.1",
        source="swe_bench_pro",
        description="tie-break burst test",
        target_files=("tests/test_smoke.py",),
        repo="r",
        confidence=1.0,
        urgency="low",
        dedup_key="k",
        causal_id=causal,
        signal_id="s",
        idempotency_key="i",
        lease_id="",
        evidence={},
        requires_human_ack=False,
        submitted_at=1.0,  # IDENTICAL across all 100 — force collisions
        routing_override="",
    )


@pytest.mark.asyncio
async def test_burst_100_colliding_ties_complete_without_typeerror():
    """Hard regression: 100 enqueues with IDENTICAL
    (priority, submitted_at) prefixes must complete cleanly.

    Before the fix, the second put would raise
    ``TypeError: '<' not supported between instances of 'IntentEnvelope'``
    because heapq's tuple comparison fell through to the envelope.
    The 4-tuple + tie_seq prefix ensures heap comparison never
    reaches the envelope.
    """
    q: asyncio.PriorityQueue = asyncio.PriorityQueue()
    # Use a local counter mirroring the production invariant.  In the
    # actual router, _HEAP_TIE_SEQ is the module-level counter; this
    # test asserts the SHAPE is collision-safe, not the specific
    # counter identity.
    tie = itertools.count()
    for i in range(100):
        await q.put((2, 1.0, next(tie), _mk_envelope(f"c{i}")))
    # If we got here without TypeError, the shape is safe at enqueue.
    assert q.qsize() == 100


@pytest.mark.asyncio
async def test_burst_100_colliding_ties_dequeue_in_fifo_order():
    """100 enqueues with identical (priority, submitted_at) prefixes
    MUST dequeue in FIFO order — tie_seq guarantees this.

    Without tie_seq (or with a non-monotonic counter), tied items
    would dequeue in arbitrary (heap-internal) order, breaking the
    Manifesto §5 "deterministic routing" contract for envelopes
    that genuinely arrive in lockstep.
    """
    q: asyncio.PriorityQueue = asyncio.PriorityQueue()
    tie = itertools.count()
    causal_ids_enqueued = []
    for i in range(100):
        cid = f"c{i:03d}"
        causal_ids_enqueued.append(cid)
        await q.put((2, 1.0, next(tie), _mk_envelope(cid)))

    causal_ids_dequeued = []
    while not q.empty():
        _p, _ts, _seq, env = await q.get()
        causal_ids_dequeued.append(env.causal_id)

    assert causal_ids_dequeued == causal_ids_enqueued, (
        "FIFO ordering broken under ties.  First 10 enqueued: "
        f"{causal_ids_enqueued[:10]}.  First 10 dequeued: "
        f"{causal_ids_dequeued[:10]}.  The tie_seq guarantee "
        "(strictly-monotonic counter shared across all enqueue "
        "sites) is load-bearing here — drift to a per-call counter "
        "or a non-int third element would break this."
    )


@pytest.mark.asyncio
async def test_higher_priority_still_wins_under_collision():
    """Tie-break does NOT compromise priority ordering: a
    higher-priority item (lower int) MUST dequeue before
    lower-priority items, even when all have the same submitted_at.

    Mixes priority-2 and priority-7 items, all with identical
    submitted_at, to prove the tie-break field doesn't accidentally
    promote stale items past hot ones.
    """
    q: asyncio.PriorityQueue = asyncio.PriorityQueue()
    tie = itertools.count()

    # Enqueue 10 priority-7 items FIRST (smaller tie_seq values)
    for i in range(10):
        await q.put((7, 1.0, next(tie), _mk_envelope(f"low{i}")))
    # Enqueue 10 priority-2 items SECOND (larger tie_seq values)
    for i in range(10):
        await q.put((2, 1.0, next(tie), _mk_envelope(f"high{i}")))

    # Priority-2 items MUST come out first despite later enqueue
    causal_ids_dequeued = []
    while not q.empty():
        _p, _ts, _seq, env = await q.get()
        causal_ids_dequeued.append(env.causal_id)

    high = [c for c in causal_ids_dequeued if c.startswith("high")]
    low = [c for c in causal_ids_dequeued if c.startswith("low")]
    assert causal_ids_dequeued == high + low, (
        "Priority ordering was violated by the tie-break field — "
        "higher-priority items did NOT preempt lower-priority items "
        "despite the heap shape.  Dequeue order: "
        f"{causal_ids_dequeued}"
    )
    # Within each priority class, FIFO is preserved
    assert high == [f"high{i}" for i in range(10)], "FIFO broken in high tier"
    assert low == [f"low{i}" for i in range(10)], "FIFO broken in low tier"


def test_intent_envelope_is_NOT_given_lt():
    """Pin: ``IntentEnvelope`` MUST remain inert for ordering.

    Per the 2026-05-12 operator binding, the load-bearing surface
    is the tuple prefix at the queue seam, not envelope
    comparability.  Adding ``__lt__`` would create two ordering
    surfaces (tuple-prefix AND envelope) that could disagree under
    drift — strictly worse than the single-seam design.
    """
    # IntentEnvelope is a frozen dataclass.  We assert it does NOT
    # define a custom __lt__ — i.e. it inherits object.__lt__ which
    # raises TypeError.
    assert IntentEnvelope.__lt__ is object.__lt__, (
        "IntentEnvelope has been given a custom __lt__.  Per the "
        "2026-05-12 operator binding, ordering authority must stay "
        "at the (priority, submitted_at, tie_seq) tuple prefix in "
        "unified_intake_router; adding __lt__ to the envelope "
        "creates a second ordering surface that can drift apart "
        "from the tuple shape under future edits.  If you genuinely "
        "need IntentEnvelope-level ordering for a different queue, "
        "compose tie_seq INTO the envelope OR add a stable-field "
        "lexicographic __lt__ (op_id, source, tie_seq) — never "
        "'always False' (not a strict weak order)."
    )
