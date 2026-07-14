"""Slice 18 — the batch plane's nervous system.

Regression spine for the "Cancelled batches" incident (Seb @ Doubleword,
2026-07-14). Two batches on ``mistralai/Devstral-2-123B-Instruct-2512`` were
accepted by DW, flipped to ``in_progress`` after one second, and then did
nothing at all for the full 1h completion window — empty output file, empty
error file, ``request_counts={total:1, completed:0, failed:0}`` — until a human
at DoubleWord cancelled them by hand and emailed us.

That model answers **403 in 0.68s** on the real-time endpoint. The information
existed. The batch plane simply had no way to hold it, because DW's batch API
does not enforce entitlement at submit time: it returns 200 for a model it will
never serve, and the denial arrives as *silence*.

The fixtures below are the REAL API objects from that incident, transcribed
verbatim from ``GET /v1/batches/{id}``. Every assertion here is anchored on what
DoubleWord actually sent us, not on what we imagine it might send.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.doubleword_provider import (
    _batch_admission_verdict,
    _batch_made_progress,
    _batch_progress_counts,
)
from backend.core.ouroboros.governance.dw_batch_ledger import (
    STATE_ABANDONED,
    STATE_CANCELLED,
    BatchLedger,
)
from backend.core.ouroboros.governance.dw_transport_profile import (
    TRANSPORT_BATCH,
    TRANSPORT_REALTIME,
    TransportProfile,
    get_transport_profile,
)

# ── Real fixtures from the incident ───────────────────────────────────

DEVSTRAL = "mistralai/Devstral-2-123B-Instruct-2512"
# Slice 17's Run-25c class: 403s on real-time, serves batch fine.
DOTTXT = "Qwen/Qwen3.5-397B-A17B-FP8-dottxt"

# GET /v1/batches/3d302917-fae0-44fd-a024-1a623433a63f — verbatim.
REAL_STALLED_BATCH = {
    "id": "3d302917-fae0-44fd-a024-1a623433a63f",
    "object": "batch",
    "endpoint": "/v1/chat/completions",
    "completion_window": "1h",
    "status": "cancelled",
    "created_at": 1784001358,
    "in_progress_at": 1784001359,   # accepted and "started" after ONE second
    "expires_at": 1784004958,
    "expired_at": 1784004958,       # ...and did nothing for the full hour
    "cancelled_at": 1784019705,     # a human at DW, 5.1h later
    "request_counts": {"total": 1, "completed": 0, "failed": 0},
    "output_file_id": "9b28c2bc-291a-4581-b52f-36ac13d0bf45",  # EMPTY
    "error_file_id": "79034ced-238d-4048-bdd4-bf446df235bf",   # ALSO EMPTY
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_BATCH_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_BATCH_ADMISSION_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DW_TRANSPORT_PROFILE_STATE_PATH", str(tmp_path / "tp.json"),
    )
    monkeypatch.setenv(
        "JARVIS_DW_BATCH_LEDGER_STATE_PATH", str(tmp_path / "bl.json"),
    )
    from backend.core.ouroboros.governance import dw_batch_ledger, dw_transport_profile
    dw_batch_ledger.reset_batch_ledger()
    # Rebuild the process-wide singletons against tmp. This MUST name the real
    # module-level singleton (_DEFAULT_PROFILE): resetting a misspelled name is
    # a silent no-op that leaks state between tests and makes them order-dependent.
    dw_transport_profile._DEFAULT_PROFILE = None  # type: ignore[attr-defined]
    yield
    dw_transport_profile._DEFAULT_PROFILE = None  # type: ignore[attr-defined]
    dw_batch_ledger.reset_batch_ledger()


# ── The progress nerve ────────────────────────────────────────────────

def test_the_real_stalled_batch_reads_as_dead():
    """The exact object DW handed us must be classified as a stall.

    ``total:1, completed:0, failed:0`` past the deadline is not slowness —
    nothing is queued behind anything. DW never scheduled the request."""
    counts = _batch_progress_counts(REAL_STALLED_BATCH)
    assert counts == {"total": 1, "completed": 0, "failed": 0}
    assert _batch_made_progress(counts) is False


def test_a_working_batch_is_never_blamed():
    """A batch that has finished ANY request is doing real work. It may be too
    slow for our budget — that is a lane/budget decision — but its model is
    innocent and must keep its ladder slot."""
    assert _batch_made_progress({"total": 4, "completed": 2, "failed": 0}) is True
    # Even a FAILED request proves DW is scheduling work on this model.
    assert _batch_made_progress({"total": 3, "completed": 0, "failed": 1}) is True


@pytest.mark.parametrize("counts", [
    {},                                    # request_counts absent entirely
    {"completed": 0, "failed": 0},         # present but no `total`
])
def test_absent_evidence_never_demotes_a_model(counts):
    """**Absent evidence is not negative evidence.**

    ``total == 0`` does not mean "DW finished nothing"; it means DW told us
    nothing. Reading that silence as proof of death would demote a model on data
    we never received — the same mis-attribution that condemned healthy models in
    Run-25c, arrived at from the opposite direction. A stall claim requires DW to
    have affirmatively told us there was work to do."""
    assert _batch_made_progress(counts) is True


@pytest.mark.parametrize("raw", [
    {"request_counts": None},
    {"request_counts": "garbage"},
    {},
])
def test_malformed_counts_are_survivable(raw):
    """A malformed payload must degrade to "no claim", never to an exception on
    the poll path."""
    assert _batch_made_progress(_batch_progress_counts(raw)) is True


# ── Batch admission (the gate that would have prevented the incident) ──

def test_devstral_is_refused_admission():
    """THE INCIDENT, in one assertion.

    Real-time proved this model is denied (403, 0.68s). The batch plane cannot
    prove that itself — it would accept the job and answer with silence for an
    hour. So a model with an RT denial and no positive batch evidence does not
    get a completion window."""
    prof = get_transport_profile()
    prof.record_unavailable(DEVSTRAL, TRANSPORT_REALTIME, status=403)
    ok, reason = _batch_admission_verdict(DEVSTRAL)
    assert ok is False
    assert reason == "rt_denied_and_batch_unproven"


def test_rt_denied_but_batch_proven_is_still_admitted():
    """Slice 17's Run-25c class must survive Slice 18.

    ``Qwen3.5-397B-…-dottxt`` 403s on real-time and serves batch fine: an account
    can hold batch entitlement while a routing rule forbids RT. A blanket
    "RT-403 → no batch" rule would re-starve the very ladder Slice 17 un-starved.
    Positive batch evidence overrides the RT denial."""
    prof = get_transport_profile()
    prof.record_unavailable(DOTTXT, TRANSPORT_REALTIME, status=403)
    prof.record_batch_success(DOTTXT)
    ok, reason = _batch_admission_verdict(DOTTXT)
    assert ok is True
    assert reason == "rt_denied_but_batch_proven"


def test_unproven_model_is_admitted():
    """No evidence against a model is not a reason to refuse it. Batch is the
    cheap lane, and an unproven model is how a model becomes proven."""
    ok, reason = _batch_admission_verdict("vendor/never-seen")
    assert ok is True
    assert reason == "no_evidence_against"


def test_a_proven_stall_refuses_the_next_batch():
    """The batch plane learning on its OWN plane: a real batch stalled, so the
    next one is refused. Before Slice 18 ``TRANSPORT_BATCH`` was a dead constant
    with zero writers and zero readers, so this could never be learned."""
    prof = get_transport_profile()
    prof.record_unavailable(
        "vendor/stalls", TRANSPORT_BATCH, status=0,
        reason="batch_stall_no_progress",
    )
    ok, reason = _batch_admission_verdict("vendor/stalls")
    assert ok is False
    assert reason == "batch_denied"


def test_a_completed_batch_invalidates_a_stale_denial():
    """Live evidence beats a fossil — the same invalidation contract
    ``record_rt_success`` honors for real-time. A capacity outage heals."""
    prof = get_transport_profile()
    prof.record_unavailable(
        "vendor/recovers", TRANSPORT_BATCH, status=0, reason="batch_stall",
    )
    assert _batch_admission_verdict("vendor/recovers")[0] is False
    prof.record_batch_success("vendor/recovers")
    assert _batch_admission_verdict("vendor/recovers")[0] is True


def test_admission_off_is_byte_identical_legacy(monkeypatch):
    """Master-off must never withhold a dispatch."""
    monkeypatch.setenv("JARVIS_DW_BATCH_ADMISSION_ENABLED", "false")
    prof = get_transport_profile()
    prof.record_unavailable(DEVSTRAL, TRANSPORT_REALTIME, status=403)
    ok, reason = _batch_admission_verdict(DEVSTRAL)
    assert ok is True
    assert reason == "admission_disabled"


# ── The durable claim ─────────────────────────────────────────────────

def test_the_obligation_survives_the_process(tmp_path):
    """The whole point of the ledger.

    ``BatchFutureRegistry`` is two in-memory dicts, so before Slice 18 every
    ``batch_id`` evaporated when the process died. The batches did not — they
    stayed live on DW's queue with nobody coming for them, and the only entity
    that ever cleaned one up was a human at DoubleWord."""
    path = tmp_path / "ledger.json"
    a = BatchLedger(state_path=path)
    a.record_open(
        "batch-abc", model=DEVSTRAL, op_id="op-1", route="background",
        reasoning_effort="low",
    )
    assert len(a.open_claims()) == 1

    # SIGKILL. New process, same disk.
    b = BatchLedger(state_path=path)
    b.load()
    orphans = b.open_claims()
    assert len(orphans) == 1
    assert orphans[0].batch_id == "batch-abc"
    # ...and we still know what we SENT, so the failure is diagnosable.
    assert orphans[0].model == DEVSTRAL
    assert orphans[0].reasoning_effort == "low"


def test_settled_claims_carry_no_obligation(tmp_path):
    path = tmp_path / "ledger.json"
    led = BatchLedger(state_path=path)
    led.record_open("batch-1", model=DEVSTRAL)
    led.settle("batch-1", STATE_CANCELLED, reason="temporal_breaker")
    assert led.open_claims() == ()


def test_settle_is_idempotent_and_first_writer_wins(tmp_path):
    """A cancel racing a webhook must not rewrite a settled claim."""
    led = BatchLedger(state_path=tmp_path / "l.json")
    led.record_open("b", model=DEVSTRAL)
    led.settle("b", STATE_CANCELLED, reason="first")
    led.settle("b", STATE_ABANDONED, reason="second")
    assert led.get("b").state == STATE_CANCELLED
    assert led.get("b").reason == "first"


def test_only_dead_sessions_claims_are_swept(tmp_path):
    """A claim from THIS process may still have a live poller attached to it.
    Cancelling a batch we are actively awaiting would be a self-inflicted wound,
    so boot reconciliation only sweeps foreign pids."""
    path = tmp_path / "ledger.json"
    led = BatchLedger(state_path=path)
    led.record_open("mine", model=DEVSTRAL)
    assert led.open_claims(foreign_only=True) == ()

    claim = led.get("mine")
    claim.pid = 999_999  # a session that died
    led.save()

    rebooted = BatchLedger(state_path=path)
    rebooted.load()
    assert len(rebooted.open_claims(foreign_only=True)) == 1


def test_a_corrupt_ledger_degrades_to_empty_not_to_a_crashed_boot(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json at all")
    led = BatchLedger(state_path=path)
    led.load()
    assert led.open_claims() == ()


def test_ledger_off_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DW_BATCH_LEDGER_ENABLED", "false")
    led = BatchLedger(state_path=tmp_path / "l.json")
    led.record_open("b", model=DEVSTRAL)
    assert led.open_claims() == ()


# ── The diagnostic reflex (verdict polarity) ──────────────────────────

class _FakeResp:
    """Transport-level fake: status + drainable body. Nothing else."""

    def __init__(self, status: int) -> None:
        self.status = status

    async def read(self) -> bytes:
        return b"{}"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Fakes the WIRE (per-model HTTP status), never the collaborator.

    ``probe_rt_entitlement`` itself runs for real — that is the point. The
    live verification on 2026-07-14 caught the reflex unpacking the probe's
    ``(denied, cleared)`` return as ``(allowed, denied)``, silently inverting
    every verdict: the 403'd model read as innocent, the healthy one as denied.
    A fake that stubbed the probe would have mirrored the same wrong assumption
    and hidden the bug forever."""

    def __init__(self, status_by_model):
        self._status_by_model = status_by_model
        self.closed = False

    def post(self, url, *, headers=None, json=None, timeout=None):
        return _FakeResp(self._status_by_model.get((json or {}).get("model"), 500))


def test_probe_return_order_is_denied_first():
    """Pin ``probe_rt_entitlement``'s return contract: ``(denied, cleared)``.

    Positional tuples have no names at the call site; this pin is what turns a
    silent future reorder into a loud red."""
    import asyncio
    from backend.core.ouroboros.governance.dw_discovery_runner import (
        probe_rt_entitlement,
    )
    session = _FakeSession({DEVSTRAL: 403, "openai/gpt-oss-120b": 200})
    first, second = asyncio.run(
        probe_rt_entitlement(
            session=session, base_url="https://api.example.test/v1",
            api_key="k", model_ids=(DEVSTRAL, "openai/gpt-oss-120b"),
        )
    )
    assert first == [DEVSTRAL], "first element must be DENIED"
    assert second == ["openai/gpt-oss-120b"], "second element must be CLEARED"


def test_diagnostic_reflex_verdicts_are_not_inverted(monkeypatch):
    """The reflex must say 'denied' for the 403'd model and 'model_alive' for
    the healthy one — through the REAL probe, end to end.

    Inverted, it commits both cardinal sins at once: keeps the dead model on
    the batch ladder (to stall again) and demotes an innocent one (Run-25c)."""
    import asyncio
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    monkeypatch.setenv("JARVIS_DW_BATCH_DIAGNOSTIC_REFLEX_ENABLED", "true")
    session = _FakeSession({DEVSTRAL: 403, "openai/gpt-oss-120b": 200})

    async def main():
        p = DoublewordProvider(api_key="k")

        async def _fake_get_session():
            return session
        monkeypatch.setattr(p, "_get_session", _fake_get_session)
        dead = await p._diagnose_stalled_model(DEVSTRAL)
        alive = await p._diagnose_stalled_model("openai/gpt-oss-120b")
        return dead, alive

    dead, alive = asyncio.run(main())
    assert dead == "denied"
    assert alive == "model_alive"


def test_reflex_off_is_inconclusive(monkeypatch):
    """Master-off must record nothing and blame nobody."""
    import asyncio
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    monkeypatch.setenv("JARVIS_DW_BATCH_DIAGNOSTIC_REFLEX_ENABLED", "false")

    async def main():
        p = DoublewordProvider(api_key="k")
        return await p._diagnose_stalled_model(DEVSTRAL)

    assert asyncio.run(main()) == "inconclusive"


# ── Review-hardening pins (the 2026-07-14 adversarial review) ────────

def test_batch_first_ladder_keeps_rt_denied_batch_proven_models(monkeypatch):
    """The ladder filter must not starve the admission gate.

    _entitlement_filtered used to drop every RT-denied model from EVERY route,
    so the Run-25c model (403 on RT, serves batch fine) never even reached
    submit_batch and the gate's 'rt_denied_but_batch_proven → ADMIT' branch was
    dead code. On batch-first routes, PROVEN batch service overrides the RT
    denial; without proof the model stays out; RT-first routes are unchanged."""
    from backend.core.ouroboros.governance.provider_topology import (
        _entitlement_filtered,
    )
    prof = get_transport_profile()
    prof.record_unavailable(DOTTXT, TRANSPORT_REALTIME, status=403)
    prof.record_batch_success(DOTTXT)
    prof.record_unavailable(DEVSTRAL, TRANSPORT_REALTIME, status=403)

    ladder = (DOTTXT, DEVSTRAL, "openai/gpt-oss-120b")
    # Batch-first route: the proven model survives, the unproven one is out.
    assert _entitlement_filtered("background", ladder) == (
        DOTTXT, "openai/gpt-oss-120b",
    )
    # RT-first route: both RT-denied models are out (Slice 17, unchanged).
    assert _entitlement_filtered("standard", ladder) == (
        "openai/gpt-oss-120b",
    )
    # All-denied still EMPTIES on every route (the Slice 17 law).
    assert _entitlement_filtered("standard", (DEVSTRAL,)) == ()
    assert _entitlement_filtered("background", (DEVSTRAL,)) == ()


def test_webhook_resolve_settles_claim_and_lays_fossil(tmp_path, monkeypatch):
    """Settlement is owned by BatchFutureRegistry, not per-webhook-branch.

    On webhook-ingress hosts the webhook wins the await race and the poll task
    (which carries the poll-side settle) is cancelled — so if resolve() did not
    settle, every completed batch leaked an OPEN claim that close() would then
    'cancel', and the admission gate never received the proof-of-service it
    reads. This pins the fast path's settlement + fossil."""
    import asyncio
    from backend.core.ouroboros.governance.batch_future_registry import (
        BatchFutureRegistry,
    )
    from backend.core.ouroboros.governance.dw_batch_ledger import (
        STATE_COMPLETED, STATE_TERMINAL, get_batch_ledger,
    )

    async def main():
        reg = BatchFutureRegistry()
        led = get_batch_ledger()
        prof = get_transport_profile()

        led.record_open("wh-done", model=DOTTXT, op_id="op-wh")
        fut = reg.register("wh-done")
        assert reg.resolve("wh-done", "out-file-1") is True
        assert await fut == "out-file-1"
        led.flush_sync()
        assert led.get("wh-done").state == STATE_COMPLETED
        assert prof.has_served_batch(DOTTXT) is True

        led.record_open("wh-dead", model=DEVSTRAL, op_id="op-wh2")
        reg.register("wh-dead")
        assert reg.reject("wh-dead", "batch.cancelled: routing") is True
        led.flush_sync()
        claim = led.get("wh-dead")
        assert claim.state == STATE_TERMINAL
        # A terminal batch is NOT proof of service.
        assert prof.has_served_batch(DEVSTRAL) is False

    asyncio.run(main())


def test_reconcile_defers_on_transient_and_terminates_on_404(monkeypatch):
    """Absent evidence is not evidence of absence, at the reconcile layer.

    A transient 5xx during boot must leave the orphan claim OPEN (retried next
    sweep) — the first draft settled it as COMPLETED, permanently hiding a
    possibly-live batch from every future sweep without cancelling it. A 404 is
    affirmative ('DW does not know this batch') and settles TERMINAL — never
    COMPLETED, which feeds positive-servability accounting."""
    import asyncio
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    from backend.core.ouroboros.governance.dw_batch_ledger import (
        STATE_TERMINAL, get_batch_ledger,
    )
    monkeypatch.setenv("JARVIS_DW_BATCH_RECONCILE_ENABLED", "true")

    led = get_batch_ledger()
    for bid in ("orphan-transient", "orphan-gone"):
        led.record_open(bid, model=DEVSTRAL, op_id="op-dead")
        led.get(bid).pid = 999_999
    led.flush_sync()

    async def main():
        p = DoublewordProvider(api_key="k")

        async def _fake_peek(batch_id):
            return ("error", None) if batch_id == "orphan-transient" else ("gone", None)
        monkeypatch.setattr(p, "_peek_batch_status", _fake_peek)
        return await p.reconcile_orphan_batches()

    stats = asyncio.run(main())
    led.flush_sync()
    assert stats.get("deferred") == 1
    assert led.get("orphan-transient").state == "open"      # retried next boot
    assert led.get("orphan-gone").state == STATE_TERMINAL   # affirmative 404
    assert get_transport_profile().has_served_batch(DEVSTRAL) is False


def test_failed_cancel_leaves_claim_open_for_retry(tmp_path):
    """A cancel we could not deliver must NOT settle the claim: settling
    removes it from open_claims — the only set shutdown release and boot
    reconciliation sweep — silently re-creating the leak. (The first draft's
    terminal ABANDONED state was exactly that dead-end.)"""
    import asyncio
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    from backend.core.ouroboros.governance.dw_batch_ledger import get_batch_ledger

    led = get_batch_ledger()
    led.record_open("cant-cancel", model=DEVSTRAL, op_id="op-x")

    async def main():
        p = DoublewordProvider(api_key="k")

        async def _boom():
            raise ConnectionError("dw unreachable")
        # Session acquisition fails → the cancel cannot be delivered.
        import unittest.mock as _m
        with _m.patch.object(p, "_get_session", side_effect=ConnectionError("down")):
            return await p._cancel_batch("cant-cancel", reason="test")

    ok = asyncio.run(main())
    assert ok is False
    claim = led.get("cant-cancel")
    assert claim.state == "open", (
        "an undeliverable cancel must leave the obligation visible"
    )
    assert len(led.open_claims()) == 1


# ── poll_and_retrieve propagation contract (teardown-drill find) ─────

def test_stall_error_propagates_through_poll_and_retrieve(monkeypatch):
    """SovereignBatchTimeoutError/BatchStalledError MUST escape
    poll_and_retrieve — the dw_fault_taxonomy predicate walks the class
    ancestry to rotate the op off the batch lane (the class docstring's
    explicit contract). The generic except was swallowing it into None,
    losing the type AND tripping UnboundLocalError on the unbound `content`
    preview. Caught by the live teardown drill of 2026-07-14."""
    import asyncio
    from backend.core.ouroboros.governance.doubleword_provider import (
        BatchStalledError,
        DoublewordProvider,
        PendingBatch,
    )

    async def main():
        p = DoublewordProvider(api_key="k")

        async def _stalls(batch_id, *, op_id=""):
            raise BatchStalledError(
                elapsed_s=45.0, deadline_s=45.0, model_id=DEVSTRAL,
                batch_id="b-1", counts={"total": 1, "completed": 0, "failed": 0},
            )
        monkeypatch.setattr(p, "_await_batch_result", _stalls)
        pending = PendingBatch(
            op_id="op-1", batch_id="b-1", file_id="f-1",
            prompt="x", submitted_at=0.0,
        )
        with pytest.raises(BatchStalledError) as exc_info:
            await p.poll_and_retrieve(pending, None)
        assert exc_info.value.batch_id == "b-1"
        assert exc_info.value.model_id == DEVSTRAL

    asyncio.run(main())


def test_pre_retrieval_failure_does_not_unboundlocal(monkeypatch, caplog):
    """A non-typed exception raised BEFORE content retrieval must surface as
    a logged None return — never as UnboundLocalError from inside the except
    handler's content preview."""
    import asyncio
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
        PendingBatch,
    )

    async def main():
        p = DoublewordProvider(api_key="k")

        async def _explodes(batch_id, *, op_id=""):
            raise ValueError("wire glitch before any content existed")
        monkeypatch.setattr(p, "_await_batch_result", _explodes)
        pending = PendingBatch(
            op_id="op-2", batch_id="b-2", file_id="f-2",
            prompt="x", submitted_at=0.0,
        )
        return await p.poll_and_retrieve(pending, None)

    assert asyncio.run(main()) is None
