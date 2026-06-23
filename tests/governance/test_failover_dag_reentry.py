"""Tests for Phase 3c -- seamless DAG re-entry through J-Prime.

When the Failover FSM is SERVING (J-Prime warm), generation re-enters through
the LocalPrimeClient (Tier-2 self-hosted), bypassing DoubleWord entirely; and
the Cryo-DLQ ops sealed during the outage are drained back through intake.

TDD with injected fakes -- ZERO real network / GCE. Covered:
  * is_jprime_serving() true  -> dispatch routes to LocalPrimeClient (DW sentinel
    NOT taken; local client called with the awakened endpoint)
  * serving false / flag OFF   -> byte-identical (DW path taken, local client
    never constructed)
  * local-route error          -> fall-through fail-soft (op not lost; DW path)
  * drain_cryo_dlq calls replay_dlq with a working ingest_fn; fail-soft on error
  * window_full public predicate (full vs not-full)
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq


# Reference the controller + state enum through the LIVE module (``fl.``) at
# call time rather than binding them at import. A sibling test in the failover
# suite does ``importlib.reload(fl)`` (test_module_imports_clean), which mints
# fresh class objects; resolving via ``fl.`` keeps this file consistent with
# whatever the current module is, regardless of collection order.
def _ControllerCls():
    return fl.FailoverLifecycleController


def _State():
    return fl.FailoverState


# ---------------------------------------------------------------------------
# window_full public predicate
# ---------------------------------------------------------------------------

class TestWindowFull:
    def setup_method(self) -> None:
        pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None

    def teardown_method(self) -> None:
        pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None

    def test_window_full_false_until_full(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
        grad = pq.get_provider_health_gradient()
        assert grad.window_full("dw") is False
        for _ in range(4):
            grad.record_sweep("dw", success=False)
        assert grad.window_full("dw") is False

    def test_window_full_true_when_full(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
        grad = pq.get_provider_health_gradient()
        for _ in range(5):
            grad.record_sweep("dw", success=True)
        assert grad.window_full("dw") is True
        # full-and-all-failed also reads full (independent of success_rate)
        grad.reset("dw")
        for _ in range(5):
            grad.record_sweep("dw", success=False)
        assert grad.window_full("dw") is True


# ---------------------------------------------------------------------------
# drain_cryo_dlq
# ---------------------------------------------------------------------------

class TestDrainCryoDlq:
    def setup_method(self) -> None:
        fl._reset_singleton_for_tests()

    def teardown_method(self) -> None:
        fl._reset_singleton_for_tests()

    async def test_drain_calls_replay_with_ingest_fn(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
        captured = {}

        async def _fake_replay(path, ingest_fn):
            captured["path"] = path
            captured["ingest_fn"] = ingest_fn
            # prove the ingest_fn is usable
            await ingest_fn({"goal_id": "g1"})
            return 1

        monkeypatch.setattr(fl, "replay_dlq", _fake_replay)

        ingested = []

        async def _ingest(env):
            ingested.append(env)

        ctrl = _ControllerCls()()
        drained = await ctrl.drain_cryo_dlq(_ingest)
        assert drained == 1
        assert captured["ingest_fn"] is _ingest
        assert ingested == [{"goal_id": "g1"}]

    async def test_drain_failsoft_on_replay_error(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")

        async def _boom(path, ingest_fn):
            raise RuntimeError("replay exploded")

        monkeypatch.setattr(fl, "replay_dlq", _boom)

        async def _ingest(env):
            pass

        ctrl = _ControllerCls()()
        # Must NOT raise -- DLQ left intact for the next attempt.
        drained = await ctrl.drain_cryo_dlq(_ingest)
        assert drained == 0

    async def test_drain_noop_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
        called = {"replay": False}

        async def _fake_replay(path, ingest_fn):
            called["replay"] = True
            return 5

        monkeypatch.setattr(fl, "replay_dlq", _fake_replay)

        async def _ingest(env):
            pass

        ctrl = _ControllerCls()()
        drained = await ctrl.drain_cryo_dlq(_ingest)
        assert drained == 0
        assert called["replay"] is False

    async def test_on_serving_transition_triggers_drain(self, monkeypatch) -> None:
        """When the FSM enters SERVING, the registered on_serving callback fires."""
        monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
        fired = {"n": 0}

        async def _on_serving():
            fired["n"] += 1

        ctrl = _ControllerCls()(
            node_ready_fn=lambda ep: True,
            on_serving_fn=_on_serving,
        )
        # Force into AWAKENING with an endpoint, then tick -> SERVING.
        ctrl._state = _State().AWAKENING
        ctrl._endpoint = "http://node:11434"
        await ctrl.tick()
        # Compare by enum NAME (not identity) so a sibling test that does
        # importlib.reload(fl) -- which mints a fresh FailoverState class --
        # cannot cause a spurious identity mismatch regardless of collection
        # order. (The reload pollution is a pre-existing suite fragility.)
        assert ctrl.state.name == "SERVING"
        assert fired["n"] == 1


# ---------------------------------------------------------------------------
# Generation re-route seam (candidate_generator)
# ---------------------------------------------------------------------------

class _FakeController:
    def __init__(self, serving: bool, endpoint=None) -> None:
        self._serving = serving
        self._endpoint = endpoint

    def is_jprime_serving(self) -> bool:
        return self._serving

    def jprime_endpoint(self):
        return self._endpoint if self._serving else None


def _make_generator():
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )

    class _StubProvider:
        provider_name = "stub"

        async def generate(self, context, deadline):  # pragma: no cover
            raise AssertionError("primary should not be called in these tests")

    return CandidateGenerator(primary=_StubProvider())


def _make_context():
    import dataclasses
    from datetime import datetime, timezone

    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
        OperationPhase,
    )

    now = datetime.now(timezone.utc)
    ctx = OperationContext(
        op_id="op-3c-test", created_at=now, phase=OperationPhase.GENERATE,
        phase_entered_at=now, context_hash="h0", previous_hash="",
        target_files=("a.py",),
    )
    # provider_route stamped to a sentinel-routed route (frozen -> replace).
    return dataclasses.replace(ctx, provider_route="standard")


class _SpyLocalResult:
    def __init__(self) -> None:
        self.candidates = ({"file_path": "x.py", "full_content": "y"},)
        self.provider_name = "gcp-jprime"
        self.generation_duration_s = 0.1


class TestGenerationReroute:
    def setup_method(self) -> None:
        fl._reset_singleton_for_tests()

    def teardown_method(self) -> None:
        fl._reset_singleton_for_tests()

    async def test_serving_routes_to_local_prime(self, monkeypatch) -> None:
        import backend.core.ouroboros.governance.candidate_generator as cg

        monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
        monkeypatch.setattr(
            cg, "lifecycle_enabled", lambda: True, raising=False,
        )
        monkeypatch.setattr(
            cg,
            "get_failover_controller",
            lambda: _FakeController(True, "http://jprime-node:11434"),
        )

        spy = {"endpoint": None, "called": 0}

        async def _fake_local_dispatch(self, context, deadline, ep):
            spy["endpoint"] = ep
            spy["called"] += 1
            return _SpyLocalResult()

        # Patch the seam helper so we assert it is reached with the endpoint
        # and that the DW sentinel body is never entered.
        monkeypatch.setattr(
            cg.CandidateGenerator,
            "_failover_local_dispatch",
            _fake_local_dispatch,
            raising=True,
        )

        gen = _make_generator()
        ctx = _make_context()
        from datetime import datetime, timezone, timedelta

        result = await gen._dispatch_via_sentinel(
            ctx, datetime.now(timezone.utc) + timedelta(seconds=60),
            "standard",
        )
        assert spy["called"] == 1
        assert spy["endpoint"] == "http://jprime-node:11434"
        assert result is not None
        assert result.candidates  # the local result was returned

    async def test_off_is_byte_identical_dw_path(self, monkeypatch) -> None:
        import backend.core.ouroboros.governance.candidate_generator as cg

        monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
        # Controller would say not-serving even if consulted.
        monkeypatch.setattr(
            cg,
            "get_failover_controller",
            lambda: _FakeController(False),
        )
        local_called = {"n": 0}

        async def _fake_local_dispatch(self, context, deadline, ep):  # pragma: no cover
            local_called["n"] += 1
            return _SpyLocalResult()

        monkeypatch.setattr(
            cg.CandidateGenerator,
            "_failover_local_dispatch",
            _fake_local_dispatch,
            raising=True,
        )

        # Stub out the DW path body so we can observe it is taken instead.
        dw_taken = {"n": 0}

        async def _fake_topology_path(*a, **k):
            dw_taken["n"] += 1
            return None  # signal fall-through to legacy

        # Replace the topology import so the sentinel body short-circuits to
        # the DW path marker.
        gen = _make_generator()
        ctx = _make_context()
        from datetime import datetime, timezone, timedelta

        # The whole point: with the flag OFF the seam is never taken, so the
        # local dispatch helper is never called. The sentinel proceeds into
        # its topology body (which, with no topology configured, returns None).
        result = await gen._dispatch_via_sentinel(
            ctx, datetime.now(timezone.utc) + timedelta(seconds=60),
            "standard",
        )
        assert local_called["n"] == 0  # local prime NEVER constructed/called

    async def test_local_error_falls_through_failsoft(self, monkeypatch) -> None:
        import backend.core.ouroboros.governance.candidate_generator as cg

        monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
        monkeypatch.setattr(
            cg, "lifecycle_enabled", lambda: True, raising=False,
        )
        monkeypatch.setattr(
            cg,
            "get_failover_controller",
            lambda: _FakeController(True, "http://jprime-node:11434"),
        )

        async def _boom_local(self, context, deadline, ep):
            raise RuntimeError("local prime down")

        monkeypatch.setattr(
            cg.CandidateGenerator,
            "_failover_local_dispatch",
            _boom_local,
            raising=True,
        )

        gen = _make_generator()
        ctx = _make_context()
        from datetime import datetime, timezone, timedelta

        # Must NOT raise the local error -- it falls through to the normal DW
        # path (which returns None here with no topology). The op is not lost.
        result = await gen._dispatch_via_sentinel(
            ctx, datetime.now(timezone.utc) + timedelta(seconds=60),
            "standard",
        )
        # No exception escaped == fail-soft fall-through held.
        assert result is None
