"""FiringTelemetry — pure-stdlib substrate regression suite.

Pins:
  * Closed 5-value FireCounterOutcome enum
  * Total guarantee: incr / snapshot / get_count / reset NEVER raise
  * Bounded memory: capacity, key length, per-key value caps
  * Thread-safety: concurrent increment from N threads
  * Module-level singleton + per-session reset semantics
  * 4 shipped_code_invariants (vocabulary + total + no-caller-imports
    + schema-version)
  * 5 FlagSpec registrations
  * GET /observability/firing-telemetry: 200 with master on,
    403 master-off, 403 umbrella-off, 400 malformed limit/prefix,
    optional ?key_prefix= filter
"""
from __future__ import annotations

import ast
import enum
import inspect
import json
import threading
from dataclasses import FrozenInstanceError
from typing import List
from unittest import mock

import pytest

from backend.core.ouroboros.governance import firing_telemetry as ft_mod
from backend.core.ouroboros.governance.firing_telemetry import (
    FIRING_TELEMETRY_SCHEMA_VERSION,
    FireCounterEntry,
    FireCounterOutcome,
    FireCounterSnapshot,
    FiringTelemetryRegistry,
    default_capacity,
    firing_telemetry_enabled,
    get_default_registry,
    incr_fire_counter,
    key_max_chars,
    per_key_value_cap,
    register_flags,
    register_shipped_invariants,
    reset_singleton_for_tests,
    snapshot_max_keys,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category, FlagSpec, FlagType,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_FIRING_TELEMETRY_ENABLED",
        "JARVIS_FIRING_TELEMETRY_CAPACITY",
        "JARVIS_FIRING_TELEMETRY_KEY_MAX_CHARS",
        "JARVIS_FIRING_TELEMETRY_PER_KEY_VALUE_CAP",
        "JARVIS_FIRING_TELEMETRY_SNAPSHOT_MAX_KEYS",
        "JARVIS_IDE_OBSERVABILITY_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# §A — Closed vocabulary
# ---------------------------------------------------------------------------


class TestClosedVocabulary:
    def test_outcome_enum_str(self):
        assert issubclass(FireCounterOutcome, enum.Enum)
        assert issubclass(FireCounterOutcome, str)

    def test_outcome_has_five_values(self):
        assert len(list(FireCounterOutcome)) == 5

    def test_outcome_value_names(self):
        names = {m.name for m in FireCounterOutcome}
        assert names == {
            "RECORDED", "DROPPED", "DISABLED", "FAILED", "RESERVED",
        }

    def test_outcome_value_strings(self):
        assert FireCounterOutcome.RECORDED.value == "recorded"
        assert FireCounterOutcome.DROPPED.value == "dropped"
        assert FireCounterOutcome.DISABLED.value == "disabled"
        assert FireCounterOutcome.FAILED.value == "failed"
        assert FireCounterOutcome.RESERVED.value == "reserved"

    def test_schema_version_pin(self):
        assert FIRING_TELEMETRY_SCHEMA_VERSION == "firing_telemetry.v1"


# ---------------------------------------------------------------------------
# §B — Frozen records
# ---------------------------------------------------------------------------


class TestFrozenRecords:
    def test_entry_frozen(self):
        e = FireCounterEntry(
            key="k", count=1, first_seen_ts=1.0, last_seen_ts=2.0,
        )
        with pytest.raises(FrozenInstanceError):
            e.count = 99  # type: ignore[misc]

    def test_snapshot_frozen(self):
        s = FireCounterSnapshot(
            schema_version=FIRING_TELEMETRY_SCHEMA_VERSION,
            counters=(),
            distinct_keys=0,
            total_increments=0,
            capacity=10,
            key_max_chars=128,
            session_started_ts=1.0,
            snapshot_taken_ts=2.0,
            truncated_count=0,
        )
        with pytest.raises(FrozenInstanceError):
            s.distinct_keys = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §C — Env knob clamps
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FIRING_TELEMETRY_ENABLED", raising=False,
        )
        assert firing_telemetry_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_FIRING_TELEMETRY_ENABLED", v)
        assert firing_telemetry_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off", "garbage"],
    )
    def test_falsy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_FIRING_TELEMETRY_ENABLED", v)
        assert firing_telemetry_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t"])
    def test_empty_unset_defaults_true(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_FIRING_TELEMETRY_ENABLED", v)
        assert firing_telemetry_enabled() is True


class TestCapacityKnobs:
    def test_default_capacity(self):
        assert default_capacity() == 4096

    def test_capacity_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FIRING_TELEMETRY_CAPACITY", "0")
        assert default_capacity() == 8

    def test_capacity_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FIRING_TELEMETRY_CAPACITY", "9999999",
        )
        assert default_capacity() == 65536

    def test_key_max_chars_default(self):
        assert key_max_chars() == 128

    def test_key_max_chars_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FIRING_TELEMETRY_KEY_MAX_CHARS", "0",
        )
        assert key_max_chars() == 16

    def test_key_max_chars_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FIRING_TELEMETRY_KEY_MAX_CHARS", "99999",
        )
        assert key_max_chars() == 512

    def test_per_key_value_cap_default(self):
        assert per_key_value_cap() == (1 << 30)

    def test_per_key_value_cap_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FIRING_TELEMETRY_PER_KEY_VALUE_CAP", "0",
        )
        assert per_key_value_cap() == 1024

    def test_snapshot_max_keys_default(self):
        assert snapshot_max_keys() == 1024


# ---------------------------------------------------------------------------
# §D — Increment semantics
# ---------------------------------------------------------------------------


class TestIncrement:
    def test_basic_increment(self):
        reset_singleton_for_tests()
        assert (
            incr_fire_counter("alpha")
            is FireCounterOutcome.RECORDED
        )
        snap = get_default_registry().snapshot()
        assert snap.distinct_keys == 1
        assert snap.total_increments == 1
        assert snap.counters[0].key == "alpha"
        assert snap.counters[0].count == 1

    def test_repeated_increment(self):
        reset_singleton_for_tests()
        for _ in range(5):
            incr_fire_counter("alpha")
        snap = get_default_registry().snapshot()
        assert snap.counters[0].count == 5
        assert snap.total_increments == 5

    def test_explicit_by_arg(self):
        reset_singleton_for_tests()
        incr_fire_counter("beta", by=10)
        assert get_default_registry().get_count("beta") == 10

    def test_negative_by_clamped_to_one(self):
        reset_singleton_for_tests()
        incr_fire_counter("beta", by=-100)
        assert get_default_registry().get_count("beta") == 1

    def test_garbage_by_falls_back_to_one(self):
        reset_singleton_for_tests()
        incr_fire_counter("beta", by="not-an-int")  # type: ignore[arg-type]
        assert get_default_registry().get_count("beta") == 1

    def test_master_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FIRING_TELEMETRY_ENABLED", "false",
        )
        outcome = incr_fire_counter("alpha")
        assert outcome is FireCounterOutcome.DISABLED
        assert get_default_registry().get_count("alpha") == 0

    def test_empty_key_returns_dropped(self):
        assert (
            incr_fire_counter("")
            is FireCounterOutcome.DROPPED
        )
        assert (
            incr_fire_counter("   ")
            is FireCounterOutcome.DROPPED
        )

    def test_non_string_key_returns_dropped(self):
        assert (
            incr_fire_counter(None)  # type: ignore[arg-type]
            is FireCounterOutcome.DROPPED
        )
        assert (
            incr_fire_counter(42)  # type: ignore[arg-type]
            is FireCounterOutcome.DROPPED
        )

    def test_long_key_truncated(self):
        reset_singleton_for_tests()
        long_key = "x" * 500
        incr_fire_counter(long_key)
        snap = get_default_registry().snapshot()
        assert len(snap.counters) == 1
        assert len(snap.counters[0].key) <= 128

    def test_capacity_cap_drops_new_keys(self):
        reset_singleton_for_tests()
        reg = FiringTelemetryRegistry(capacity=8)
        # Fill to capacity.
        for i in range(8):
            assert reg.incr(f"k{i}") is FireCounterOutcome.RECORDED
        # New key beyond cap → DROPPED.
        assert reg.incr("overflow") is FireCounterOutcome.DROPPED
        # Existing keys still increment.
        assert reg.incr("k0") is FireCounterOutcome.RECORDED
        snap = reg.snapshot()
        assert snap.distinct_keys == 8
        assert "overflow" not in {c.key for c in snap.counters}

    def test_per_key_value_cap_enforced(self):
        # Floor on the cap is 1024 (clamp); use a value above floor.
        reg = FiringTelemetryRegistry(
            per_key_value_cap_override=1024,
        )
        for _ in range(2000):
            reg.incr("hot")
        # Hard ceiling enforced.
        assert reg.get_count("hot") == 1024


# ---------------------------------------------------------------------------
# §E — Snapshot semantics
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_sorted_by_count_desc_then_key_asc(self):
        reset_singleton_for_tests()
        # Build counts: a=1, b=3, c=3, d=2
        for _ in range(1):
            incr_fire_counter("a")
        for _ in range(3):
            incr_fire_counter("b")
            incr_fire_counter("c")
        for _ in range(2):
            incr_fire_counter("d")
        snap = get_default_registry().snapshot()
        keys = [c.key for c in snap.counters]
        # b and c tied at 3 → key ascending → b before c
        # then d at 2 → then a at 1
        assert keys == ["b", "c", "d", "a"]

    def test_snapshot_max_keys_truncates_with_count(self):
        reset_singleton_for_tests()
        for i in range(10):
            for _ in range(i + 1):
                incr_fire_counter(f"k{i:02d}")
        snap = get_default_registry().snapshot(max_keys=3)
        assert len(snap.counters) == 3
        assert snap.distinct_keys == 10
        assert snap.truncated_count == 7

    def test_snapshot_to_dict_jsonable(self):
        reset_singleton_for_tests()
        incr_fire_counter("alpha")
        snap = get_default_registry().snapshot()
        d = snap.to_dict()
        json.dumps(d)
        assert (
            d["schema_version"]
            == FIRING_TELEMETRY_SCHEMA_VERSION
        )
        assert d["totals"]["distinct_keys"] == 1
        assert d["counters"][0]["key"] == "alpha"

    def test_session_uptime_monotonic(self):
        reset_singleton_for_tests()
        snap = get_default_registry().snapshot()
        assert snap.session_uptime_s >= 0.0


# ---------------------------------------------------------------------------
# §F — Reset semantics
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_counters(self):
        reset_singleton_for_tests()
        incr_fire_counter("alpha")
        incr_fire_counter("beta")
        get_default_registry().reset_for_session()
        snap = get_default_registry().snapshot()
        assert snap.distinct_keys == 0
        assert snap.total_increments == 0

    def test_reset_resets_session_timer(self):
        reset_singleton_for_tests()
        reg = get_default_registry()
        first_start = reg.session_started_ts
        # Sleep is forbidden; just call reset and verify it bumps.
        reg.reset_for_session()
        assert reg.session_started_ts >= first_start

    def test_singleton_reset_creates_new_instance(self):
        reset_singleton_for_tests()
        a = get_default_registry()
        reset_singleton_for_tests()
        b = get_default_registry()
        assert a is not b


# ---------------------------------------------------------------------------
# §G — Total guarantee (NEVER raises)
# ---------------------------------------------------------------------------


class TestTotalGuarantee:
    def test_incr_never_raises_on_garbage(self):
        reg = FiringTelemetryRegistry()
        # Various garbage inputs.
        assert reg.incr(None) is FireCounterOutcome.DROPPED  # type: ignore[arg-type]
        assert reg.incr({}) is FireCounterOutcome.DROPPED  # type: ignore[arg-type]
        assert reg.incr([]) is FireCounterOutcome.DROPPED  # type: ignore[arg-type]
        assert reg.incr("ok", by=None) is FireCounterOutcome.RECORDED  # type: ignore[arg-type]

    def test_get_count_never_raises(self):
        reg = FiringTelemetryRegistry()
        assert reg.get_count(None) == 0  # type: ignore[arg-type]
        assert reg.get_count("") == 0
        assert reg.get_count(42) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §H — Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_increment_no_loss(self):
        reset_singleton_for_tests()
        reg = get_default_registry()
        N_THREADS = 16
        N_PER_THREAD = 250
        errors: List[Exception] = []

        def worker():
            try:
                for _ in range(N_PER_THREAD):
                    reg.incr("hot")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker) for _ in range(N_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert reg.get_count("hot") == N_THREADS * N_PER_THREAD

    def test_concurrent_distinct_keys(self):
        reset_singleton_for_tests()
        reg = FiringTelemetryRegistry(capacity=4096)
        errors: List[Exception] = []

        def worker(tid: int):
            try:
                for i in range(50):
                    reg.incr(f"thread_{tid}_key_{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert reg.snapshot().distinct_keys == 20 * 50


# ---------------------------------------------------------------------------
# §I — register_shipped_invariants
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_returns_four(self):
        invs = register_shipped_invariants()
        assert len(invs) == 4

    def test_invariant_names(self):
        invs = register_shipped_invariants()
        names = {i.invariant_name for i in invs}
        assert names == {
            "fire_counter_outcome_vocabulary",
            "firing_telemetry_incr_total",
            "firing_telemetry_no_caller_imports",
            "firing_telemetry_schema_version_pinned",
        }

    def test_outcome_vocabulary_pin_passes_clean(self):
        src = inspect.getsource(ft_mod)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        vocab = next(
            i for i in invs
            if i.invariant_name == "fire_counter_outcome_vocabulary"
        )
        assert vocab.validate(tree, src) == ()

    def test_outcome_vocabulary_pin_fires_on_added_value(self):
        bad_src = (
            "import enum\n"
            "class FireCounterOutcome(str, enum.Enum):\n"
            "    RECORDED = 'r'\n"
            "    DROPPED = 'd'\n"
            "    DISABLED = 'x'\n"
            "    FAILED = 'f'\n"
            "    RESERVED = 'v'\n"
            "    NEW_ROGUE = 'n'\n"
        )
        tree = ast.parse(bad_src)
        vocab = next(
            i for i in register_shipped_invariants()
            if i.invariant_name == "fire_counter_outcome_vocabulary"
        )
        violations = vocab.validate(tree, bad_src)
        assert any("NEW_ROGUE" in v for v in violations)

    def test_incr_total_pin_passes_clean(self):
        src = inspect.getsource(ft_mod)
        tree = ast.parse(src)
        total = next(
            i for i in register_shipped_invariants()
            if i.invariant_name == "firing_telemetry_incr_total"
        )
        assert total.validate(tree, src) == ()

    def test_incr_total_pin_fires_on_synthetic_raise(self):
        bad_src = (
            "class FiringTelemetryRegistry:\n"
            "    def incr(self, key, *, by=1):\n"
            "        raise RuntimeError('bug')\n"
        )
        tree = ast.parse(bad_src)
        total = next(
            i for i in register_shipped_invariants()
            if i.invariant_name == "firing_telemetry_incr_total"
        )
        violations = total.validate(tree, bad_src)
        assert any("raise" in v for v in violations)

    def test_no_caller_imports_pin_passes_clean(self):
        src = inspect.getsource(ft_mod)
        tree = ast.parse(src)
        nci = next(
            i for i in register_shipped_invariants()
            if i.invariant_name == "firing_telemetry_no_caller_imports"
        )
        assert nci.validate(tree, src) == ()

    def test_no_caller_imports_pin_fires_on_synthetic(self):
        bad_src = (
            "from backend.core.ouroboros.governance.orchestrator "
            "import X\n"
        )
        tree = ast.parse(bad_src)
        nci = next(
            i for i in register_shipped_invariants()
            if i.invariant_name == "firing_telemetry_no_caller_imports"
        )
        violations = nci.validate(tree, bad_src)
        assert any("orchestrator" in v for v in violations)

    def test_schema_version_pin_passes_clean(self):
        src = inspect.getsource(ft_mod)
        tree = ast.parse(src)
        sv = next(
            i for i in register_shipped_invariants()
            if i.invariant_name
            == "firing_telemetry_schema_version_pinned"
        )
        assert sv.validate(tree, src) == ()

    def test_schema_version_pin_fires_on_drift(self):
        bad_src = (
            "FIRING_TELEMETRY_SCHEMA_VERSION = "
            "'firing_telemetry.v2'\n"
        )
        tree = ast.parse(bad_src)
        sv = next(
            i for i in register_shipped_invariants()
            if i.invariant_name
            == "firing_telemetry_schema_version_pinned"
        )
        violations = sv.validate(tree, bad_src)
        assert any("firing_telemetry.v2" in v for v in violations)


# ---------------------------------------------------------------------------
# §J — register_flags
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

    def test_master_default_true(self):
        reg = _StubRegistry()
        register_flags(reg)
        master = next(
            s for s in reg.specs
            if s.name == "JARVIS_FIRING_TELEMETRY_ENABLED"
        )
        assert master.default is True
        assert master.type is FlagType.BOOL
        assert master.category is Category.OBSERVABILITY

    def test_all_flag_names(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = {s.name for s in reg.specs}
        assert names == {
            "JARVIS_FIRING_TELEMETRY_ENABLED",
            "JARVIS_FIRING_TELEMETRY_CAPACITY",
            "JARVIS_FIRING_TELEMETRY_KEY_MAX_CHARS",
            "JARVIS_FIRING_TELEMETRY_PER_KEY_VALUE_CAP",
            "JARVIS_FIRING_TELEMETRY_SNAPSHOT_MAX_KEYS",
        }

    def test_all_specs_documented(self):
        reg = _StubRegistry()
        register_flags(reg)
        for spec in reg.specs:
            assert spec.description.strip()
            assert spec.source_file.endswith(".py")
            assert spec.since.startswith("FiringTelemetry Slice 1")


# ---------------------------------------------------------------------------
# §K — GET route (aiohttp)
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
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FIRING_TELEMETRY_ENABLED", "true",
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
        assert "/observability/firing-telemetry" in paths

    @pytest.mark.asyncio
    async def test_get_200_with_master_on(self, router):
        # Pre-populate counters so the response has content.
        reset_singleton_for_tests()
        incr_fire_counter("test.alpha")
        incr_fire_counter("test.alpha")
        incr_fire_counter("test.beta")
        resp = await router._handle_firing_telemetry(
            _make_request("/observability/firing-telemetry"),
        )
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["enabled"] is True
        assert (
            body["schema_version"]
            == FIRING_TELEMETRY_SCHEMA_VERSION
        )
        keys = {c["key"] for c in body["counters"]}
        assert "test.alpha" in keys
        assert "test.beta" in keys
        # alpha=2 sorted before beta=1.
        assert body["counters"][0]["key"] == "test.alpha"
        assert body["counters"][0]["count"] == 2

    @pytest.mark.asyncio
    async def test_get_403_when_master_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_FIRING_TELEMETRY_ENABLED", "false",
        )
        resp = await router._handle_firing_telemetry(
            _make_request("/observability/firing-telemetry"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert (
            body["reason_code"]
            == "ide_observability.firing_telemetry_disabled"
        )

    @pytest.mark.asyncio
    async def test_get_403_when_umbrella_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "false",
        )
        resp = await router._handle_firing_telemetry(
            _make_request("/observability/firing-telemetry"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.disabled"

    @pytest.mark.asyncio
    async def test_get_400_on_malformed_limit(self, router):
        resp = await router._handle_firing_telemetry(
            _make_request(
                "/observability/firing-telemetry?limit=garbage",
            ),
        )
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert (
            body["reason_code"]
            == "ide_observability.malformed_limit"
        )

    @pytest.mark.asyncio
    async def test_key_prefix_filter(self, router):
        reset_singleton_for_tests()
        incr_fire_counter("observer.gradient.tick")
        incr_fire_counter("observer.coherence.tick")
        incr_fire_counter("admission_gate.SHED")
        resp = await router._handle_firing_telemetry(
            _make_request(
                "/observability/firing-telemetry"
                "?key_prefix=observer.",
            ),
        )
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        keys = {c["key"] for c in body["counters"]}
        assert "observer.gradient.tick" in keys
        assert "observer.coherence.tick" in keys
        assert "admission_gate.SHED" not in keys
        assert body.get("filter", {}).get("key_prefix") == "observer."

    @pytest.mark.asyncio
    async def test_too_long_key_prefix_400(self, router):
        prefix = "x" * 200
        resp = await router._handle_firing_telemetry(
            _make_request(
                "/observability/firing-telemetry"
                f"?key_prefix={prefix}",
            ),
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# §L — Singleton + module-level convenience
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_default_registry_returns_singleton(self):
        a = get_default_registry()
        b = get_default_registry()
        assert a is b

    def test_module_level_incr_uses_default_singleton(self):
        reset_singleton_for_tests()
        incr_fire_counter("mod-level")
        assert get_default_registry().get_count("mod-level") == 1
