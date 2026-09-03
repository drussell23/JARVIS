"""A session's ledger is the size of a session, is read off the loop, and
cannot outgrow its cap.

Three defects measured in soak bt-2026-09-02-220948, each pinned here:

1. **Every soak wrote to ``default/``.** ``resolve_session_id`` honoured
   ``OUROBOROS_BATTLE_SESSION_ID`` -- documented as "set by the harness" and
   set only by the replay tool. The live harness stamps
   ``JARVIS_OUROBOROS_SESSION_ID``. So ``.jarvis/determinism/default/
   decisions.jsonl`` reached 578 MB / 8,375 records across every session
   since the ledger existed.
2. **Three readers scanned it on the main loop, per op.** The sidecar
   profiler named the frames: ``evidence_ledger._iter_records`` (108
   STUCK_FRAME events), ``property_capture.get_recorded_claims`` (56),
   ``postmortem.list_recent_postmortems`` (52); 300 control-plane
   starvation events, stalls of 10-65 s; the harness force-cancelled an op
   that had been starved for 2,101 s and ended the run at 40% of budget.
3. **Nothing bounded the file.** Partitioning bounds a ledger to one run;
   a run that outgrows the cap must still not accumulate a synchronous
   bottleneck.

The fixes compose ONE segment rule (``decision_runtime.ledger_segments``),
ONE record walk (``property_capture.iter_ledger_records``), and the
existing ``cooperative_fs_io.offload`` substrate. These tests drive the
REAL writer through a tiny cap so a seal actually happens, then assert
every consumer still sees every record.
"""
from __future__ import annotations

import asyncio
import json

import pytest

DR = "backend.core.ouroboros.governance.determinism.decision_runtime"
SI = "backend.core.ouroboros.governance.determinism.session_identity"
PC = "backend.core.ouroboros.governance.verification.property_capture"
PM = "backend.core.ouroboros.governance.verification.postmortem"
EL = "backend.core.ouroboros.governance.verification.evidence_ledger"
EC = "backend.core.ouroboros.governance.verification.evidence_collectors"
DRD = "backend.core.ouroboros.governance.determinism.decisions_reader"
PO = "backend.core.ouroboros.governance.postmortem_observability"


def _mod(name):
    import importlib
    return importlib.import_module(name)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """The ledger fixture the capture tests use, verbatim in spirit."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"))
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "seal-session")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MAX_BYTES", raising=False)
    dr = _mod(DR)
    dr.reset_all_for_tests()
    yield tmp_path / "det"
    dr.reset_all_for_tests()


def _claim(pc, op_id: str, name: str):
    from backend.core.ouroboros.governance.verification.property_oracle import Property
    return pc.PropertyClaim(
        op_id=op_id, claimed_at_phase="PLAN",
        property=Property.make(kind="x", name=name),
    )


# ---------------------------------------------------------------------------
# 1. Session identity: the id the harness actually stamps
# ---------------------------------------------------------------------------

def test_canonical_session_id_partitions_the_ledger(monkeypatch):
    si = _mod(SI)
    monkeypatch.delenv(si.SESSION_ENV, raising=False)
    monkeypatch.setenv("JARVIS_OUROBOROS_SESSION_ID", "bt-2026-09-02-220948")
    assert si.resolve_session_id() == "bt-2026-09-02-220948"


def test_the_battle_env_still_outranks_the_canonical_one(monkeypatch):
    si = _mod(SI)
    monkeypatch.setenv(si.SESSION_ENV, "explicit-battle")
    monkeypatch.setenv("JARVIS_OUROBOROS_SESSION_ID", "canonical")
    assert si.resolve_session_id() == "explicit-battle"


def test_without_either_env_pytest_isolation_is_unchanged(monkeypatch):
    si = _mod(SI)
    monkeypatch.delenv(si.SESSION_ENV, raising=False)
    monkeypatch.delenv("JARVIS_OUROBOROS_SESSION_ID", raising=False)
    got = si.resolve_session_id()
    assert got.startswith(si.TEST_SESSION_PREFIX)


def test_a_hostile_canonical_id_cannot_escape_the_ledger_root(monkeypatch):
    si = _mod(SI)
    monkeypatch.delenv(si.SESSION_ENV, raising=False)
    monkeypatch.setenv("JARVIS_OUROBOROS_SESSION_ID", "../../etc")
    assert "/" not in si.resolve_session_id() and ".." not in si.resolve_session_id()


# ---------------------------------------------------------------------------
# 2. One segment rule
# ---------------------------------------------------------------------------

def test_segments_sort_by_seal_order_with_live_last(tmp_path):
    dr = _mod(DR)
    live = tmp_path / "decisions.jsonl"
    for n in (2, 10, 1):
        (tmp_path / f"decisions.{n:06d}.jsonl").write_text(f"{n}\n")
    live.write_text("live\n")
    segs = dr.ledger_segments(live)
    assert [p.name for p in segs] == [
        "decisions.000001.jsonl", "decisions.000002.jsonl",
        "decisions.000010.jsonl", "decisions.jsonl",
    ]
    assert dr.read_ledger_lines(live) == ["1", "2", "10", "live"]
    assert list(dr.iter_ledger_lines(live)) == ["1\n", "2\n", "10\n", "live\n"]


def test_ledger_exists_sees_a_sealed_segment_without_a_live_file(tmp_path):
    dr = _mod(DR)
    live = tmp_path / "decisions.jsonl"
    assert dr.ledger_exists(live) is False
    (tmp_path / "decisions.000001.jsonl").write_text("x\n")
    assert dr.ledger_exists(live) is True


def test_read_lines_tolerates_a_missing_live_file(tmp_path):
    dr = _mod(DR)
    live = tmp_path / "decisions.jsonl"
    (tmp_path / "decisions.000001.jsonl").write_text("a\n")
    assert list(dr.iter_ledger_lines(live)) == ["a\n"]


# ---------------------------------------------------------------------------
# 3. The REAL writer seals, and every consumer still sees every record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_writer_seals_at_the_cap_and_readers_span_segments(isolated, monkeypatch):
    """Cap of one byte: every append seals the previous file first."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MAX_BYTES", "1")
    dr, pc, drd = _mod(DR), _mod(PC), _mod(DRD)
    dr.reset_all_for_tests()

    n = await pc.capture_claims(
        op_id="op-seal", claims=[_claim(pc, "op-seal", f"c{i}") for i in range(4)],
    )
    assert n == 4

    live = pc._ledger_path_for_session("seal-session")
    segs = pc.ledger_segments_for_session("seal-session")
    sealed = [p for p in segs if p != live and p.exists()]
    assert len(sealed) >= 3, [p.name for p in segs]
    assert all(p.name.startswith("decisions.") and p.name.endswith(".jsonl") for p in sealed)

    # claims reader: all four, across segments
    claims = pc.get_recorded_claims(op_id="op-seal", session_id="seal-session")
    assert sorted(c.property.name for c in claims) == ["c0", "c1", "c2", "c3"]

    # decisions reader: counts the whole session, not just the live file
    res = drd.read_records_for_session("seal-session")
    assert res.total_records_in_file >= 4
    assert len(res.records) >= 4

    # the runtime's index: a sealed record is still findable
    rt = dr.runtime_for_session("seal-session")
    rec = await rt.lookup(op_id="op-seal", phase="PLAN", kind="property_claim", ordinal=0)
    assert rec is not None

    # positional indices are stable over the concatenation
    lines = dr.read_ledger_lines(live)
    assert len(lines) >= 4
    assert all(json.loads(ln)["op_id"] == "op-seal" for ln in lines if ln.strip())


def test_sealing_is_off_at_zero_and_never_raises(tmp_path, monkeypatch):
    dr = _mod(DR)
    live = tmp_path / "decisions.jsonl"
    live.write_text("x" * 1000)
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MAX_BYTES", "0")
    dr._maybe_seal(live)
    assert live.exists() and not list(tmp_path.glob("decisions.*.jsonl"))
    # a missing file is fine
    dr._maybe_seal(tmp_path / "nope.jsonl")


def test_seal_numbering_increments_past_existing_segments(tmp_path, monkeypatch):
    dr = _mod(DR)
    live = tmp_path / "decisions.jsonl"
    (tmp_path / "decisions.000007.jsonl").write_text("old\n")
    live.write_text("y" * 64)
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MAX_BYTES", "8")
    dr._maybe_seal(live)
    assert (tmp_path / "decisions.000008.jsonl").exists()
    assert not live.exists()


def test_default_cap_matches_the_readers_refusal_threshold(monkeypatch):
    dr, drd = _mod(DR), _mod(DRD)
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MAX_BYTES", raising=False)
    assert dr.ledger_max_bytes() == drd._MAX_LEDGER_FILE_BYTES


# ---------------------------------------------------------------------------
# 4. Off the loop: same answer, sentinel-safe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offloaded_readers_agree_with_their_sync_twins(isolated):
    pc, pm, el = _mod(PC), _mod(PM), _mod(EL)
    await pc.capture_claims(op_id="op-off", claims=[_claim(pc, "op-off", "k")])
    sync_claims = pc.get_recorded_claims(op_id="op-off", session_id="seal-session")
    off_claims = await pc.get_recorded_claims_offloaded(op_id="op-off", session_id="seal-session")
    assert [c.property.name for c in off_claims] == [c.property.name for c in sync_claims] == ["k"]
    assert await pm.list_recent_postmortems_offloaded(session_id="seal-session") == \
        pm.list_recent_postmortems(session_id="seal-session")
    assert await el.recorded_evidence_offloaded(op_id="op-off", session_id="seal-session") == \
        el.recorded_evidence(op_id="op-off", session_id="seal-session")


@pytest.mark.asyncio
async def test_an_offload_sentinel_degrades_to_empty_not_an_exception(monkeypatch):
    """``offload`` RETURNS on failure; the readers must check, not catch."""
    pc, pm, el = _mod(PC), _mod(PM), _mod(EL)
    from backend.core.ouroboros.governance import cooperative_fs_io as cfs

    async def _broken(fn, *a, **k):
        return cfs.OffloadError(fn_name="reader", exc_type="OSError",
                                message="disk gone", cpu_bound=False)

    monkeypatch.setattr(cfs, "offload", _broken)
    assert await pc.get_recorded_claims_offloaded(op_id="x") == ()
    assert await pm.list_recent_postmortems_offloaded() == ()
    assert await el.recorded_evidence_offloaded(op_id="x") == {}
    assert await el.recorded_providers_used_offloaded(op_id="x") == ()


@pytest.mark.asyncio
async def test_ctx_wins_and_the_ledger_is_only_read_for_missing_keys(monkeypatch):
    ec = _mod(EC)
    calls = []

    async def _spy(*, op_id):
        calls.append(op_id)
        return {"diff_text": "from-ledger", "other": 1}

    el = _mod(EL)
    monkeypatch.setattr(el, "recorded_evidence_offloaded", _spy)

    class _Ctx:
        op_id = "op-ctx"
        diff_text = "from-ctx"

    got = await ec._from_ctx_or_ledger_async(_Ctx(), "diff_text")
    assert got == {"diff_text": "from-ctx"} and calls == []

    class _Ctx2:
        op_id = "op-ctx"
        diff_text = None

    got = await ec._from_ctx_or_ledger_async(_Ctx2(), "diff_text")
    assert got == {"diff_text": "from-ledger"} and calls == ["op-ctx"]


# ---------------------------------------------------------------------------
# 5. The postmortem observer hook leaves the loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_observer_hook_is_dispatched_to_the_blast_executor(monkeypatch):
    po = _mod(PO)
    from backend.core.ouroboros.governance import operation_advisor as oa
    from backend.core.ouroboros.governance import auto_action_router as aar
    from concurrent.futures import ThreadPoolExecutor
    import threading

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blast-test")
    monkeypatch.setattr(oa, "_get_advisor_blast_executor", lambda: pool)
    seen = {}

    class _Obs:
        def on_terminal_postmortem_persisted(self, **kw):
            seen["thread"] = threading.current_thread().name
            seen["kw"] = kw

    monkeypatch.setattr(aar, "get_post_postmortem_observer", lambda: _Obs())
    po.publish_terminal_postmortem_persisted(
        op_id="op-h", record_id="op-h", terminal_phase="VERIFY",
        total_claims=1, has_blocking_failures=False,
    )
    for _ in range(50):
        if "thread" in seen:
            break
        await asyncio.sleep(0.01)
    assert seen["thread"].startswith("blast-test"), seen
    assert seen["kw"]["op_id"] == "op-h"
    pool.shutdown(wait=True)


def test_observer_hook_runs_inline_without_a_loop(monkeypatch):
    po = _mod(PO)
    from backend.core.ouroboros.governance import auto_action_router as aar
    seen = {}

    class _Obs:
        def on_terminal_postmortem_persisted(self, **kw):
            seen["kw"] = kw

    monkeypatch.setattr(aar, "get_post_postmortem_observer", lambda: _Obs())
    po.publish_terminal_postmortem_persisted(
        op_id="op-i", record_id="op-i", terminal_phase="VERIFY",
        total_claims=0, has_blocking_failures=False,
    )
    assert seen["kw"]["op_id"] == "op-i"
