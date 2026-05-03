"""AdmissionGate Slice 3 — graduation regression suite.

Pins the four graduation deliverables:

  * Master flag flip: default-True post-graduation
  * 4 ``shipped_code_invariants`` AST pins (taxonomy + bug-fix
    regression pin on _call_fallback + total-function pin +
    no-caller-imports pin)
  * 5 FlagRegistry seeds (master + 4 env-knob accessors)
  * ``EVENT_TYPE_ADMISSION_DECISION_EMITTED`` SSE event registered
  * 1 GET route ``/observability/admission-gate``
  * RecentDecisionsRing thread-safety + bounded memory
"""
from __future__ import annotations

import ast
import inspect
import json
import threading
from typing import Any, Dict, List
from unittest import mock

import pytest

from backend.core.ouroboros.governance.admission_estimator import (
    ADMISSION_ESTIMATOR_SCHEMA_VERSION,
    RecentDecisionsRing,
    WaitTimeEstimator,
    get_default_estimator,
    get_default_history,
    history_ring_size,
    reset_singletons_for_tests,
)
from backend.core.ouroboros.governance.admission_gate import (
    AdmissionContext,
    AdmissionDecision,
    admission_gate_enabled,
    compute_admission_decision,
    register_flags,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category, FlagSpec, FlagType,
)
from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
    EVENT_TYPE_ADMISSION_DECISION_EMITTED,
    _VALID_EVENT_TYPES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in (
        "JARVIS_ADMISSION_GATE_ENABLED",
        "JARVIS_ADMISSION_MIN_VIABLE_CALL_S",
        "JARVIS_ADMISSION_BUDGET_SAFETY_FACTOR",
        "JARVIS_ADMISSION_QUEUE_DEPTH_HARD_CAP",
        "JARVIS_ADMISSION_ESTIMATOR_ALPHA",
        "JARVIS_ADMISSION_HISTORY_RING_SIZE",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_singletons_for_tests()
    yield
    reset_singletons_for_tests()


# ---------------------------------------------------------------------------
# §A — Master flag graduation
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADMISSION_GATE_ENABLED", raising=False,
        )
        assert admission_gate_enabled() is True

    @pytest.mark.parametrize(
        "val", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_explicit_truthy(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_GATE_ENABLED", val,
        )
        assert admission_gate_enabled() is True

    @pytest.mark.parametrize(
        "val", ["0", "false", "no", "off", "garbage"],
    )
    def test_explicit_falsy_rolls_back(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_GATE_ENABLED", val,
        )
        assert admission_gate_enabled() is False

    @pytest.mark.parametrize("val", ["", "   ", "\t"])
    def test_empty_treats_as_unset(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_GATE_ENABLED", val,
        )
        assert admission_gate_enabled() is True


# ---------------------------------------------------------------------------
# §B — register_shipped_invariants
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_returns_four(self):
        invs = register_shipped_invariants()
        assert len(invs) == 4

    def test_invariant_names(self):
        invs = register_shipped_invariants()
        names = {i.invariant_name for i in invs}
        expected = {
            "admission_decision_vocabulary",
            "candidate_generator_admission_check_present",
            "compute_admission_decision_total",
            "admission_gate_no_caller_imports",
        }
        assert names == expected

    def test_each_invariant_has_validator_and_target(self):
        invs = register_shipped_invariants()
        for inv in invs:
            assert callable(inv.validate)
            assert inv.target_file.startswith("backend/")
            assert inv.description.strip() != ""

    def test_decision_vocabulary_pin_passes_clean_source(self):
        from backend.core.ouroboros.governance import (
            admission_gate,
        )
        src = inspect.getsource(admission_gate)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name
            == "admission_decision_vocabulary"
        )
        assert vocab_inv.validate(tree, src) == ()

    def test_decision_vocabulary_pin_fires_on_added_value(self):
        bad_src = (
            "import enum\n"
            "class AdmissionDecision(str, enum.Enum):\n"
            "    ADMIT = 'a'\n"
            "    SHED_BUDGET_INSUFFICIENT = 'b'\n"
            "    SHED_QUEUE_DEEP = 'c'\n"
            "    DISABLED = 'd'\n"
            "    FAILED = 'e'\n"
            "    NEW_ROGUE_VALUE = 'f'\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name
            == "admission_decision_vocabulary"
        )
        violations = vocab_inv.validate(tree, bad_src)
        assert any("NEW_ROGUE_VALUE" in v for v in violations)

    def test_decision_vocabulary_pin_fires_on_missing_value(self):
        bad_src = (
            "import enum\n"
            "class AdmissionDecision(str, enum.Enum):\n"
            "    ADMIT = 'a'\n"
            "    DISABLED = 'd'\n"
            "    FAILED = 'e'\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name
            == "admission_decision_vocabulary"
        )
        violations = vocab_inv.validate(tree, bad_src)
        joined = " ".join(violations)
        assert "SHED_BUDGET_INSUFFICIENT" in joined
        assert "SHED_QUEUE_DEEP" in joined

    def test_call_fallback_admission_check_pin_passes_clean(self):
        # THE BUG-FIX REGRESSION PIN. Validates that
        # candidate_generator's _call_fallback body contains the
        # admission-gate dispatch wired in Slice 2.
        from backend.core.ouroboros.governance import (
            candidate_generator,
        )
        src = inspect.getsource(candidate_generator)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        check_inv = next(
            i for i in invs
            if i.invariant_name
            == "candidate_generator_admission_check_present"
        )
        violations = check_inv.validate(tree, src)
        assert violations == (), (
            "BUG-FIX regression pin violated: "
            f"{violations} — Slice 2's _call_fallback "
            "admission-gate wiring was removed; the IMMEDIATE-"
            "route saturation bug has regressed"
        )

    def test_call_fallback_pin_fires_on_synthetic_removal(self):
        bad_src = (
            "class FakeGenerator:\n"
            "    async def _call_fallback(self, context, deadline):\n"
            "        # Refactor accidentally removed the gate\n"
            "        async with self._fallback_sem:\n"
            "            pass\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        check_inv = next(
            i for i in invs
            if i.invariant_name
            == "candidate_generator_admission_check_present"
        )
        violations = check_inv.validate(tree, bad_src)
        joined = " ".join(violations)
        assert "compute_admission_decision" in joined
        assert "is_shed" in joined
        assert "pre_admission_shed" in joined

    def test_total_function_pin_passes_clean(self):
        from backend.core.ouroboros.governance import (
            admission_gate,
        )
        src = inspect.getsource(admission_gate)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        total_inv = next(
            i for i in invs
            if i.invariant_name
            == "compute_admission_decision_total"
        )
        assert total_inv.validate(tree, src) == ()

    def test_total_function_pin_fires_on_synthetic_raise(self):
        bad_src = (
            "def compute_admission_decision(ctx, *, enabled):\n"
            "    if not enabled:\n"
            "        raise RuntimeError('boom')\n"
            "    return None\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        total_inv = next(
            i for i in invs
            if i.invariant_name
            == "compute_admission_decision_total"
        )
        violations = total_inv.validate(tree, bad_src)
        assert len(violations) >= 1
        assert "raise" in violations[0]

    def test_no_caller_imports_pin_passes_clean(self):
        from backend.core.ouroboros.governance import (
            admission_gate,
        )
        src = inspect.getsource(admission_gate)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        no_caller_inv = next(
            i for i in invs
            if i.invariant_name
            == "admission_gate_no_caller_imports"
        )
        assert no_caller_inv.validate(tree, src) == ()

    def test_no_caller_imports_pin_fires_on_synthetic_import(self):
        bad_src = (
            "from backend.core.ouroboros.governance.candidate_generator import X\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        no_caller_inv = next(
            i for i in invs
            if i.invariant_name
            == "admission_gate_no_caller_imports"
        )
        violations = no_caller_inv.validate(tree, bad_src)
        assert any(
            "candidate_generator" in v for v in violations
        )


# ---------------------------------------------------------------------------
# §C — register_flags
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self) -> None:
        self.specs: List[FlagSpec] = []

    def bulk_register(self, specs, *, override=False) -> int:
        self.specs.extend(specs)
        return len(specs)


class TestFlagRegistry:
    def test_register_returns_five(self):
        reg = _StubRegistry()
        n = register_flags(reg)
        assert n == 5

    def test_master_flag_default_true(self):
        reg = _StubRegistry()
        register_flags(reg)
        master = next(
            s for s in reg.specs
            if s.name == "JARVIS_ADMISSION_GATE_ENABLED"
        )
        assert master.default is True
        assert master.type is FlagType.BOOL
        assert master.category is Category.SAFETY

    def test_all_five_flag_names(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = {s.name for s in reg.specs}
        expected = {
            "JARVIS_ADMISSION_GATE_ENABLED",
            "JARVIS_ADMISSION_MIN_VIABLE_CALL_S",
            "JARVIS_ADMISSION_BUDGET_SAFETY_FACTOR",
            "JARVIS_ADMISSION_QUEUE_DEPTH_HARD_CAP",
            "JARVIS_ADMISSION_ESTIMATOR_ALPHA",
        }
        assert names == expected

    def test_all_specs_documented(self):
        reg = _StubRegistry()
        register_flags(reg)
        for spec in reg.specs:
            assert isinstance(spec.category, Category)
            assert spec.description.strip() != ""
            assert spec.source_file.endswith(".py")
            assert spec.since.startswith(
                "AdmissionGate Slice 3"
            )

    def test_no_duplicate_names(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = [s.name for s in reg.specs]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# §D — SSE event registration
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_type_constant(self):
        assert (
            EVENT_TYPE_ADMISSION_DECISION_EMITTED
            == "admission_decision_emitted"
        )

    def test_event_type_in_valid_set(self):
        assert (
            EVENT_TYPE_ADMISSION_DECISION_EMITTED
            in _VALID_EVENT_TYPES
        )


# ---------------------------------------------------------------------------
# §E — RecentDecisionsRing
# ---------------------------------------------------------------------------


class TestRecentDecisionsRing:
    def test_default_capacity(self):
        ring = RecentDecisionsRing()
        assert ring.capacity == 64

    def test_custom_capacity(self):
        ring = RecentDecisionsRing(capacity=10)
        assert ring.capacity == 10

    def test_capacity_clamped(self):
        ring = RecentDecisionsRing(capacity=1)
        assert ring.capacity == 4  # floor
        ring = RecentDecisionsRing(capacity=999999)
        assert ring.capacity == 4096  # ceiling

    def test_records_appended_in_order(self):
        ring = RecentDecisionsRing(capacity=10)
        for i in range(5):
            ring.record({"i": i})
        snap = ring.snapshot()
        assert len(snap) == 5
        assert [r["i"] for r in snap] == [0, 1, 2, 3, 4]

    def test_eviction_at_capacity(self):
        ring = RecentDecisionsRing(capacity=4)
        for i in range(10):
            ring.record({"i": i})
        snap = ring.snapshot()
        # Only last 4 survive (FIFO eviction)
        assert len(snap) == 4
        assert [r["i"] for r in snap] == [6, 7, 8, 9]

    def test_snapshot_limit(self):
        ring = RecentDecisionsRing(capacity=20)
        for i in range(15):
            ring.record({"i": i})
        snap = ring.snapshot(limit=5)
        assert len(snap) == 5
        # Last 5
        assert [r["i"] for r in snap] == [10, 11, 12, 13, 14]

    def test_garbage_input_silently_dropped(self):
        ring = RecentDecisionsRing(capacity=10)
        ring.record(None)  # type: ignore[arg-type]
        ring.record("not a dict")  # type: ignore[arg-type]
        ring.record(42)  # type: ignore[arg-type]
        ring.record({"valid": True})
        snap = ring.snapshot()
        assert len(snap) == 1
        assert snap[0]["valid"] is True

    def test_reset_clears(self):
        ring = RecentDecisionsRing(capacity=10)
        for i in range(5):
            ring.record({"i": i})
        ring.reset()
        assert ring.snapshot() == ()
        assert ring.stats()["size"] == 0

    def test_concurrent_record_no_crash(self):
        # Stress: 16 threads × 100 iters = 1,600 concurrent
        # records on a 100-capacity ring. Verify no exceptions
        # + ring stays bounded.
        ring = RecentDecisionsRing(capacity=100)
        errors: List[Exception] = []

        def worker(tid: int):
            try:
                for i in range(100):
                    ring.record(
                        {"tid": tid, "i": i},
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        snap = ring.snapshot()
        assert len(snap) == 100  # bounded


class TestHistoryRingSize:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADMISSION_HISTORY_RING_SIZE", raising=False,
        )
        assert history_ring_size() == 64

    def test_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_HISTORY_RING_SIZE", "0",
        )
        assert history_ring_size() == 4

    def test_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_HISTORY_RING_SIZE", "999999",
        )
        assert history_ring_size() == 4096


class TestSingletons:
    def test_get_default_estimator_returns_same_instance(self):
        a = get_default_estimator()
        b = get_default_estimator()
        assert a is b

    def test_get_default_history_returns_same_instance(self):
        a = get_default_history()
        b = get_default_history()
        assert a is b

    def test_reset_creates_new(self):
        a = get_default_estimator()
        b = get_default_history()
        reset_singletons_for_tests()
        a2 = get_default_estimator()
        b2 = get_default_history()
        assert a is not a2
        assert b is not b2


# ---------------------------------------------------------------------------
# §F — GET route
# ---------------------------------------------------------------------------


def _aiohttp_available() -> bool:
    try:
        from aiohttp.test_utils import make_mocked_request  # noqa
        return True
    except ImportError:
        return False


def _make_request(path: str):
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", path)
    req._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return req


@pytest.mark.skipif(
    not _aiohttp_available(),
    reason="aiohttp not available",
)
class TestGETRoute:
    @pytest.fixture(autouse=True)
    def _ide_obs_on(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ADMISSION_GATE_ENABLED", "true",
        )

    @pytest.fixture
    def router(self):
        from backend.core.ouroboros.governance.ide_observability import (  # noqa: E501
            IDEObservabilityRouter,
        )
        return IDEObservabilityRouter()

    def test_route_registers(self, router):
        from aiohttp import web
        app = web.Application()
        router.register_routes(app)
        paths = [
            getattr(r, "resource", None)
            and r.resource.canonical
            for r in app.router.routes()
        ]
        assert "/observability/admission-gate" in paths

    @pytest.mark.asyncio
    async def test_get_returns_200_when_master_on(self, router):
        resp = await router._handle_admission_gate(
            _make_request("/observability/admission-gate"),
        )
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["enabled"] is True
        assert "config" in body
        for key in (
            "min_viable_call_s", "budget_safety_factor",
            "queue_depth_hard_cap", "estimator_alpha",
        ):
            assert key in body["config"]
        assert "estimator" in body
        assert "history" in body
        assert "capacity" in body["history"]
        assert "recent" in body["history"]

    @pytest.mark.asyncio
    async def test_get_returns_403_when_master_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_GATE_ENABLED", "false",
        )
        resp = await router._handle_admission_gate(
            _make_request("/observability/admission-gate"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert (
            body["reason_code"]
            == "ide_observability.admission_gate_disabled"
        )

    @pytest.mark.asyncio
    async def test_get_returns_403_when_umbrella_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "false",
        )
        resp = await router._handle_admission_gate(
            _make_request("/observability/admission-gate"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.disabled"

    @pytest.mark.asyncio
    async def test_get_malformed_limit_400(self, router):
        resp = await router._handle_admission_gate(
            _make_request(
                "/observability/admission-gate?limit=garbage",
            ),
        )
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert (
            body["reason_code"]
            == "ide_observability.malformed_limit"
        )

    @pytest.mark.asyncio
    async def test_get_recent_history_after_records(
        self, router,
    ):
        # Pre-populate the history ring with a synthetic record.
        ring = get_default_history()
        ctx = AdmissionContext(
            route="immediate", remaining_s=100.0,
            queue_depth=2, projected_wait_s=10.0,
            op_id="op-test",
        )
        rec = compute_admission_decision(
            ctx, enabled=True, decided_at_ts=1.0,
        )
        ring.record(rec.to_dict())
        resp = await router._handle_admission_gate(
            _make_request("/observability/admission-gate"),
        )
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        recent = body["history"]["recent"]
        assert len(recent) >= 1
        # Find our record in the projection.
        ours = [
            r for r in recent
            if r.get("op_id") == "op-test"
        ]
        assert len(ours) == 1
        assert ours[0]["decision"] == "admit"


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_estimator_schema_pin():
    assert (
        ADMISSION_ESTIMATOR_SCHEMA_VERSION
        == "admission_estimator.v1"
    )
