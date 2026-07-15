"""Slice 19 — Soak Budget & Compute Circuit-Breaker.

The fail-closed boundary that lets an unattended graduation soak run without
runaway GCE/API spend. Two independent triggers (cumulative cost, GCE runtime),
a sticky trip that refuses ALL new resource acquisition + cancels active batch
queues, a one-shot 80% warning, and restart-durable boot reconstruction from
the Aegis spend WAL + live GCP node runtime.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import soak_circuit_breaker as SCB


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Arm the breaker with known caps + a fresh singleton per test."""
    monkeypatch.setenv("JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SOAK_MAX_COST_USD", "1.00")
    monkeypatch.setenv("JARVIS_SOAK_MAX_GCE_RUNTIME_S", "600")
    monkeypatch.setenv("JARVIS_SOAK_BUDGET_WARN_PCT", "0.8")
    SCB.reset_for_tests()
    yield
    SCB.reset_for_tests()


class _FakeVM:
    def __init__(self, uptime_s: float, rate: float = 0.03):
        self.uptime_hours = uptime_s / 3600.0
        self.cost_per_hour = rate


class _FakeMgr:
    def __init__(self, vms=None):
        self.managed_vms = vms or {}


# ── Config purity (mandate 2) ────────────────────────────────────────

def test_config_resolves_from_env(monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_MAX_COST_USD", "7.50")
    monkeypatch.setenv("JARVIS_SOAK_MAX_GCE_RUNTIME_S", "1800")
    cfg = SCB.SoakBreakerConfig.from_env()
    assert cfg.enabled is True
    assert cfg.max_cost_usd == 7.50
    assert cfg.max_gce_runtime_s == 1800.0
    assert cfg.cost_trigger_active and cfg.runtime_trigger_active


def test_warn_pct_out_of_range_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_BUDGET_WARN_PCT", "1.5")
    assert SCB.SoakBreakerConfig.from_env().warn_pct == 0.8
    monkeypatch.setenv("JARVIS_SOAK_BUDGET_WARN_PCT", "0")
    assert SCB.SoakBreakerConfig.from_env().warn_pct == 0.8


def test_zero_cap_disables_that_trigger(monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_MAX_COST_USD", "0")
    cfg = SCB.SoakBreakerConfig.from_env()
    assert cfg.cost_trigger_active is False
    assert cfg.runtime_trigger_active is True


def test_disabled_master_is_byte_identical(monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED", "false")
    assert SCB.soak_breaker_enabled() is False
    # Even a runaway node yields NO refusal when unarmed.
    b = SCB.get_soak_breaker()
    mgr = _FakeMgr({"n": _FakeVM(9999)})
    b.register_vm_manager(mgr)
    assert SCB.soak_dispatch_refusal_reason() is None
    assert b.is_tripped() is False


# ── Trigger 1: GCE runtime (mandate 1) ───────────────────────────────

def test_gce_runtime_trip():
    b = SCB.get_soak_breaker()
    mgr = _FakeMgr({"n": _FakeVM(700)})  # 700s > 600 cap
    b.register_vm_manager(mgr)
    a = b.assess()
    assert a.over_budget is True
    assert a.runtime_pct > 1.0
    reason = SCB.soak_dispatch_refusal_reason()
    assert reason is not None and "runtime" in reason
    assert b.is_tripped() is True


def test_gce_runtime_healthy_no_trip():
    b = SCB.get_soak_breaker()
    mgr = _FakeMgr({"n": _FakeVM(120)})  # well under 600
    b.register_vm_manager(mgr)
    assert SCB.soak_dispatch_refusal_reason() is None
    assert b.is_tripped() is False


# ── Trigger 2: cumulative cost (mandate 1) ───────────────────────────

def test_cost_trip(monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_MAX_GCE_RUNTIME_S", "0")  # cost only
    from backend.core.ouroboros.governance import cost_governor as CG
    gov = CG.CostGovernor()
    CG.set_default_cost_governor(gov)
    gov.charge("op-1", 1.25, provider="claude")
    b = SCB.get_soak_breaker()
    a = b.assess()
    assert a.over_budget is True
    assert a.cost_used_usd >= 1.25
    reason = SCB.soak_dispatch_refusal_reason()
    assert reason is not None and "cost" in reason
    CG.set_default_cost_governor(None)


def test_gce_compute_cost_counts_toward_cost_cap(monkeypatch):
    # A running node's compute $ counts against the COST cap independently
    # of the runtime cap. Disable runtime trigger; node runs long enough to
    # accrue > $1 at $2/hr.
    monkeypatch.setenv("JARVIS_SOAK_MAX_GCE_RUNTIME_S", "0")
    b = SCB.get_soak_breaker()
    mgr = _FakeMgr({"n": _FakeVM(3600, rate=2.0)})  # 1h * $2 = $2 > $1
    b.register_vm_manager(mgr)
    a = b.assess()
    assert a.cost_used_usd >= 2.0
    assert a.over_budget is True


# ── Sticky trip + idempotency ────────────────────────────────────────

def test_trip_is_sticky_and_idempotent():
    b = SCB.get_soak_breaker()
    assert b.trip(reason="first") is True
    assert b.trip(reason="second") is False  # already tripped
    assert b.is_tripped() is True
    snap = b.snapshot()
    assert snap["tripped"] is True
    assert snap["trip_reason"] == "first"


def test_healthy_then_recovered_stays_tripped():
    """A trip never auto-recovers within a session even if usage drops."""
    b = SCB.get_soak_breaker()
    mgr = _FakeMgr({"n": _FakeVM(700)})
    b.register_vm_manager(mgr)
    assert SCB.soak_dispatch_refusal_reason() is not None
    # Node terminates → usage drops to zero, but the latch holds.
    mgr.managed_vms.clear()
    assert b.is_tripped() is True
    assert SCB.soak_dispatch_refusal_reason() is not None


# ── One-shot 80% warning (mandate 2) ─────────────────────────────────

def test_warning_fires_once_at_80pct(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        SCB.SoakCircuitBreaker, "_publish",
        staticmethod(lambda kind, detail: emitted.append((kind, detail))),
    )
    monkeypatch.setattr(
        SCB.SoakCircuitBreaker, "_durable_append",
        staticmethod(lambda tag, detail: None),
    )
    b = SCB.get_soak_breaker()
    mgr = _FakeMgr({"n": _FakeVM(500)})  # 500/600 = 83% → warn, not trip
    b.register_vm_manager(mgr)
    b.assess_and_maybe_trip()
    b.assess_and_maybe_trip()  # second call must NOT re-emit
    warns = [e for e in emitted if e[0] == "warning"]
    assert len(warns) == 1
    assert b.is_tripped() is False


# ── Trip cancels active batch queues (mandate 1 + DRY mandate 3) ─────

def test_trip_cancels_batch_queue():
    async def _run():
        from backend.core.ouroboros.governance.batch_future_registry import (
            BatchFutureRegistry,
        )
        reg = BatchFutureRegistry()
        fut = reg.register("batch-abc")
        assert reg.pending_count == 1
        b = SCB.get_soak_breaker()
        b.register_batch_registry(reg)
        out = await b.cancel_active_queues()
        assert out["batch_futures_cancelled"] == 1
        assert reg.pending_count == 0
        assert fut.cancelled()
    asyncio.run(_run())


def test_cancel_all_is_idempotent_and_safe():
    from backend.core.ouroboros.governance.batch_future_registry import (
        BatchFutureRegistry,
    )
    async def _run():
        reg = BatchFutureRegistry()
        reg.register("b1")
        assert reg.cancel_all("x") == 1
        assert reg.cancel_all("x") == 0  # nothing left
    asyncio.run(_run())


# ── Boot reconciliation / restart durability (mandate 4) ─────────────

def test_boot_reconcile_rebuilds_cost_baseline(tmp_path, monkeypatch):
    """A durable Aegis spend-WAL is replayed into the cost baseline so a
    restarted soak resumes at the right utilization — and trips on boot if
    the reconstructed spend already exceeds the cap."""
    monkeypatch.setenv("JARVIS_SOAK_MAX_GCE_RUNTIME_S", "0")  # cost only
    wal = tmp_path / "spend.jsonl"
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(wal))
    from backend.core.ouroboros.aegis import flags as AF
    from backend.core.ouroboros.aegis.spend_wal import (
        SpendEntry, SpendEntryKind, append_entry_sync,
    )
    # Seed durable spend that already blew the $1.00 cap.
    append_entry_sync(AF.wal_path(), SpendEntry(
        kind=SpendEntryKind.RECONCILE, ts=1.0, op_id="o1",
        route="immediate", actual_cost_usd=1.40,
    ))
    b = SCB.get_soak_breaker()
    summary = asyncio.run(b.reconcile_on_boot())
    assert summary["cost_baseline_usd"] >= 1.40
    assert summary["tripped_on_boot"] is True
    assert b.is_tripped() is True


def test_boot_reconcile_syncs_live_gce_runtime(monkeypatch):
    """Boot calls the manager's GCP sync so live-node runtime is the REAL
    node age, not this process's age."""
    monkeypatch.setenv("JARVIS_SOAK_MAX_COST_USD", "0")  # runtime only

    class SyncingMgr:
        def __init__(self):
            self.managed_vms = {}
            self.synced = False

        async def _sync_managed_vms_with_gcp(self):
            # Simulate discovering a long-running orphan node on GCP.
            self.managed_vms["orphan"] = _FakeVM(900)  # > 600 cap
            self.synced = True

    mgr = SyncingMgr()
    b = SCB.get_soak_breaker()
    b.register_vm_manager(mgr)
    summary = asyncio.run(b.reconcile_on_boot())
    assert mgr.synced is True
    assert summary["gce_synced"] is True
    assert summary["tripped_on_boot"] is True


def test_boot_reconcile_inert_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED", "false")
    b = SCB.get_soak_breaker()
    summary = asyncio.run(b.reconcile_on_boot())
    assert summary["enabled"] is False
    assert b.is_tripped() is False


# ── Fail-soft contract (never raises) ────────────────────────────────

def test_assess_never_raises_on_bad_manager():
    class Exploding:
        @property
        def managed_vms(self):
            raise RuntimeError("boom")
    b = SCB.get_soak_breaker()
    mgr = Exploding()
    b.register_vm_manager(mgr)
    # Must degrade to a healthy assessment, never propagate.
    a = b.assess()
    assert a.over_budget is False


def test_refusal_reason_fail_open_on_fault(monkeypatch):
    # If the breaker singleton accessor throws, the module hook fails OPEN.
    monkeypatch.setattr(SCB, "get_soak_breaker",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert SCB.soak_dispatch_refusal_reason() is None


# ── SSE event wiring ─────────────────────────────────────────────────

def test_sse_event_types_registered():
    from backend.core.ouroboros.governance import ide_observability_stream as S
    assert S.EVENT_TYPE_SOAK_BUDGET_WARNING in S._VALID_EVENT_TYPES
    assert S.EVENT_TYPE_SOAK_CIRCUIT_TRIPPED in S._VALID_EVENT_TYPES


def test_sse_publish_soak_trip(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    from backend.core.ouroboros.governance import ide_observability_stream as S
    S.reset_default_broker()
    broker = S.get_default_broker()
    broker.subscribe()
    eid = S.publish_soak_circuit_tripped({"reason": "test", "cost_used_usd": 2.0})
    assert eid is not None
    hist = broker.recent_history(
        limit=5, event_type=S.EVENT_TYPE_SOAK_CIRCUIT_TRIPPED,
    )
    assert len(hist) == 1
    assert hist[0].payload["reason"] == "test"
    S.reset_default_broker()
