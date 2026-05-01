"""Move 4 Slice 3 — InvariantDriftObserver regression spine.

Coverage tracks the four operational behaviors:

  * Lifecycle — start/stop/is_running, master+observer flag gates
  * Cadence — base interval, posture multiplier, vigilance shrink,
    failure backoff, all composing correctly
  * Tick semantics — full ObserverTickResult decision tree, drift
    de-duplication, history append, emitter wiring
  * Authority invariants — AST-pinned (no orchestrator/iron_gate/etc)
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import (
    invariant_drift_observer as obs_mod,
)
from backend.core.ouroboros.governance.invariant_drift_auditor import (
    ExplorationFloorPin,
    InvariantDriftRecord,
    InvariantSnapshot,
)
from backend.core.ouroboros.governance.invariant_drift_observer import (
    InvariantDriftObserver,
    InvariantDriftSignalEmitter,
    ObserverTickResult,
    backoff_ceiling_s,
    base_interval_s,
    dedup_window,
    get_default_observer,
    get_signal_emitter,
    observer_enabled,
    posture_multipliers,
    register_signal_emitter,
    reset_default_observer,
    reset_signal_emitter,
    vigilance_factor,
    vigilance_ticks,
)
from backend.core.ouroboros.governance.invariant_drift_store import (
    InvariantDriftStore,
    install_boot_snapshot,
    reset_default_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _master_flag_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "true",
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_state():
    reset_default_store()
    reset_default_observer()
    reset_signal_emitter()
    yield
    reset_default_store()
    reset_default_observer()
    reset_signal_emitter()


@pytest.fixture
def store(tmp_path) -> InvariantDriftStore:
    return InvariantDriftStore(tmp_path)


def _make_snapshot(
    snapshot_id: str = "stub",
    captured_at_utc: float = 1000.0,
    *,
    shipped_invariant_names=("alpha", "beta"),
    posture_value: Optional[str] = None,
) -> InvariantSnapshot:
    return InvariantSnapshot(
        snapshot_id=snapshot_id,
        captured_at_utc=captured_at_utc,
        shipped_invariant_names=tuple(shipped_invariant_names),
        shipped_violation_signature="sig",
        shipped_violation_count=0,
        flag_registry_hash="flag_v1",
        flag_count=42,
        exploration_floor_pins=(
            ExplorationFloorPin(
                complexity="moderate", min_score=8.0,
                min_categories=3, required_categories=(),
            ),
        ),
        posture_value=posture_value,
        posture_confidence=None,
    )


class _CapturingEmitter(InvariantDriftSignalEmitter):
    """Captures every emit call for inspection."""

    def __init__(self) -> None:
        self.calls: List[
            Tuple[InvariantSnapshot, Tuple[InvariantDriftRecord, ...]]
        ] = []

    def emit(self, snapshot, drift_records):
        self.calls.append((snapshot, drift_records))


class _RaisingEmitter(InvariantDriftSignalEmitter):
    def emit(self, snapshot, drift_records):
        raise RuntimeError("emitter blew up")


# ---------------------------------------------------------------------------
# 1. Env knobs — defaults + overrides + floors
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_observer_enabled_default_true_post_graduation(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            raising=False,
        )
        # Slice 5 graduation flipped this default.
        assert observer_enabled() is True

    @pytest.mark.parametrize(
        "value,expected",
        [("1", True), ("true", True), ("YES", True), ("on", True),
         ("0", False), ("false", False), ("no", False),
         # Empty = unset = post-graduation default true
         ("", True),
         # Garbage falls to revert
         ("garbage", False)],
    )
    def test_observer_enabled_env(self, monkeypatch, value, expected):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", value,
        )
        assert observer_enabled() is expected

    def test_base_interval_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S",
            raising=False,
        )
        assert base_interval_s() == 600.0

    def test_base_interval_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S", "5",
        )
        # Floor is 30s
        assert base_interval_s() == 30.0

    def test_base_interval_garbage_falls_to_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S", "huh",
        )
        assert base_interval_s() == 600.0

    def test_vigilance_ticks_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_TICKS", "0",
        )
        assert vigilance_ticks() == 1

    def test_vigilance_factor_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_FACTOR",
            "5.0",
        )
        # Ceiling at 1.0
        assert vigilance_factor() == 1.0

    def test_vigilance_factor_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_FACTOR",
            "0.0001",
        )
        # Floor 0.05
        assert vigilance_factor() == 0.05

    def test_dedup_window_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_DEDUP_WINDOW", "0",
        )
        assert dedup_window() == 1

    def test_backoff_ceiling_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_BACKOFF_CEILING_S",
            "10",
        )
        assert backoff_ceiling_s() == 60.0

    def test_posture_multipliers_defaults(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
            raising=False,
        )
        m = posture_multipliers()
        # Sensible defaults: HARDEN tightens, EXPLORE loosens
        assert m["HARDEN"] < 1.0
        assert m["EXPLORE"] > 1.0
        assert m["CONSOLIDATE"] == 1.0

    def test_posture_multipliers_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
            json.dumps({"HARDEN": 0.1, "EXPLORE": 3.0}),
        )
        m = posture_multipliers()
        assert m["HARDEN"] == 0.1
        assert m["EXPLORE"] == 3.0
        # Defaults preserved for keys not overridden
        assert m["CONSOLIDATE"] == 1.0

    def test_posture_multipliers_invalid_json_uses_defaults(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
            "not valid json",
        )
        m = posture_multipliers()
        assert m["HARDEN"] == 0.5  # default

    def test_posture_multipliers_non_dict_uses_defaults(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
            "[1, 2, 3]",
        )
        m = posture_multipliers()
        assert m["HARDEN"] == 0.5

    def test_posture_multipliers_garbage_value_skipped(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
            json.dumps({"HARDEN": "not_a_float"}),
        )
        m = posture_multipliers()
        # Garbage value ignored; default kept
        assert m["HARDEN"] == 0.5


# ---------------------------------------------------------------------------
# 2. Pluggable signal emitter — register/get/reset
# ---------------------------------------------------------------------------


class TestEmitterRegistry:
    def test_default_emitter_is_noop(self):
        emitter = get_signal_emitter()
        # Default emits don't raise and produce no observable side
        # effect
        emitter.emit(_make_snapshot(), ())

    def test_register_replaces_default(self):
        cap = _CapturingEmitter()
        register_signal_emitter(cap)
        assert get_signal_emitter() is cap

    def test_reset_restores_noop(self):
        cap = _CapturingEmitter()
        register_signal_emitter(cap)
        reset_signal_emitter()
        # After reset, emitting must not reach the capturer
        get_signal_emitter().emit(_make_snapshot(), ())
        assert cap.calls == []

    def test_register_rejects_non_emitter(self):
        register_signal_emitter("not an emitter")  # type: ignore[arg-type]
        # Default still in place
        emitter = get_signal_emitter()
        emitter.emit(_make_snapshot(), ())  # does not raise


# ---------------------------------------------------------------------------
# 3. Cadence computation — base × posture × vigilance × backoff
# ---------------------------------------------------------------------------


class TestCadence:
    def test_base_only_when_no_posture_no_vigilance(
        self, store, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S",
            raising=False,
        )
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: None,
        )
        # No posture → base interval (600s)
        assert observer.compute_interval_s() == 600.0

    def test_posture_multiplier_applied(self, store):
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: "HARDEN",
        )
        # HARDEN = 0.5× → 300s
        assert observer.compute_interval_s() == 300.0

    def test_explore_loosens_cadence(self, store):
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: "EXPLORE",
        )
        assert observer.compute_interval_s() == 900.0

    def test_unknown_posture_uses_baseline(self, store):
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: "BOGUS_POSTURE",
        )
        assert observer.compute_interval_s() == 600.0

    def test_vigilance_shrinks_cadence(self, store):
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: None,
        )
        observer._vigilance_ticks_remaining = 3
        # 600s × 0.5 = 300s
        assert observer.compute_interval_s() == 300.0

    def test_backoff_extends_cadence(self, store):
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: None,
        )
        observer._consecutive_failures = 2
        # 600s × (1+2) = 1800s, which equals the default ceiling
        assert observer.compute_interval_s() == 1800.0

    def test_backoff_capped_at_ceiling(self, store, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_BACKOFF_CEILING_S",
            "1200",
        )
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: None,
        )
        observer._consecutive_failures = 100
        # Should clamp to 1200s
        assert observer.compute_interval_s() == 1200.0

    def test_interval_floor_enforced(self, store, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
            json.dumps({"HARDEN": 0.001}),  # near-zero multiplier
        )
        observer = InvariantDriftObserver(
            store, posture_reader=lambda: "HARDEN",
        )
        # Interval floor is 30s
        assert observer.compute_interval_s() == 30.0


# ---------------------------------------------------------------------------
# 4. run_one_cycle — full ObserverTickResult decision tree
# ---------------------------------------------------------------------------


class TestRunOneCycle:
    @pytest.mark.asyncio
    async def test_no_baseline_succeeds_with_history_append(
        self, store,
    ):
        snap = _make_snapshot(snapshot_id="first")
        observer = InvariantDriftObserver(
            store, capture=lambda: snap,
            posture_reader=lambda: None,
        )
        result = await observer.run_one_cycle()
        assert isinstance(result, ObserverTickResult)
        assert result.captured == snap
        assert result.drift_records == ()
        assert result.failure_reason is None
        # History was appended
        assert len(store.load_history()) == 1

    @pytest.mark.asyncio
    async def test_baseline_match_no_drift_no_emit(self, store):
        snap = _make_snapshot()
        install_boot_snapshot(store=store, snapshot=snap)
        cap = _CapturingEmitter()
        observer = InvariantDriftObserver(
            store, emitter=cap, capture=lambda: snap,
            posture_reader=lambda: None,
        )
        result = await observer.run_one_cycle()
        assert result.drift_records == ()
        assert result.emitted is False
        assert cap.calls == []

    @pytest.mark.asyncio
    async def test_drift_detected_emits_signal(self, store):
        baseline = _make_snapshot(
            snapshot_id="baseline",
            shipped_invariant_names=("alpha", "beta"),
        )
        install_boot_snapshot(store=store, snapshot=baseline)
        drifted = _make_snapshot(
            snapshot_id="drifted",
            shipped_invariant_names=("alpha",),  # beta removed
        )
        cap = _CapturingEmitter()
        observer = InvariantDriftObserver(
            store, emitter=cap, capture=lambda: drifted,
            posture_reader=lambda: None,
        )
        result = await observer.run_one_cycle()
        assert len(result.drift_records) >= 1
        assert result.emitted is True
        assert result.deduped is False
        assert len(cap.calls) == 1

    @pytest.mark.asyncio
    async def test_repeated_drift_deduped(self, store):
        baseline = _make_snapshot(
            snapshot_id="baseline",
            shipped_invariant_names=("alpha", "beta"),
        )
        install_boot_snapshot(store=store, snapshot=baseline)
        drifted = _make_snapshot(
            snapshot_id="drifted",
            shipped_invariant_names=("alpha",),
        )
        cap = _CapturingEmitter()
        observer = InvariantDriftObserver(
            store, emitter=cap, capture=lambda: drifted,
            posture_reader=lambda: None,
        )
        # First cycle — emits
        await observer.run_one_cycle()
        # Second cycle — same drift, deduped
        result_2 = await observer.run_one_cycle()
        # Third cycle — still same, still deduped
        result_3 = await observer.run_one_cycle()
        assert result_2.deduped is True
        assert result_2.emitted is False
        assert result_3.deduped is True
        # Only one emission total
        assert len(cap.calls) == 1
        # Stats reflect dedup count
        stats = observer.stats()
        assert stats["signals_emitted"] == 1
        assert stats["signals_deduped"] == 2

    @pytest.mark.asyncio
    async def test_novel_drift_emits_again_after_dedup(self, store):
        baseline = _make_snapshot(
            snapshot_id="baseline",
            shipped_invariant_names=("alpha", "beta", "gamma"),
        )
        install_boot_snapshot(store=store, snapshot=baseline)
        # Sequence of captures: dropping beta → dropping gamma →
        # dropping beta+gamma. Each is a DIFFERENT drift signature.
        captures = iter([
            _make_snapshot(
                snapshot_id="c1",
                shipped_invariant_names=("alpha", "gamma"),
            ),
            _make_snapshot(
                snapshot_id="c2",
                shipped_invariant_names=("alpha", "beta"),
            ),
            _make_snapshot(
                snapshot_id="c3",
                shipped_invariant_names=("alpha",),
            ),
        ])
        cap = _CapturingEmitter()
        observer = InvariantDriftObserver(
            store, emitter=cap,
            capture=lambda: next(captures),
            posture_reader=lambda: None,
        )
        for _ in range(3):
            await observer.run_one_cycle()
        # Three NOVEL signatures → three emissions
        assert len(cap.calls) == 3

    @pytest.mark.asyncio
    async def test_capture_failure_records_backoff(self, store):
        def boom():
            raise RuntimeError("simulated capture failure")

        observer = InvariantDriftObserver(
            store, capture=boom, posture_reader=lambda: None,
        )
        result = await observer.run_one_cycle()
        assert result.captured is None
        assert result.failure_reason is not None
        # consecutive_failures incremented
        stats = observer.stats()
        assert stats["consecutive_failures"] == 1
        assert stats["cycles_failed"] == 1
        # Subsequent successful capture resets
        snap = _make_snapshot()
        observer._capture = lambda: snap
        await observer.run_one_cycle()
        assert observer.stats()["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_emitter_exception_swallowed(self, store):
        baseline = _make_snapshot(
            snapshot_id="baseline",
            shipped_invariant_names=("alpha", "beta"),
        )
        install_boot_snapshot(store=store, snapshot=baseline)
        drifted = _make_snapshot(
            snapshot_id="drifted",
            shipped_invariant_names=("alpha",),
        )
        observer = InvariantDriftObserver(
            store, emitter=_RaisingEmitter(),
            capture=lambda: drifted,
            posture_reader=lambda: None,
        )
        # Must NOT propagate the emitter's exception
        result = await observer.run_one_cycle()
        # Drift was detected; we attempted to emit (raised+swallowed)
        assert len(result.drift_records) >= 1

    @pytest.mark.asyncio
    async def test_drift_escalates_vigilance(self, store):
        baseline = _make_snapshot(
            snapshot_id="baseline",
            shipped_invariant_names=("alpha", "beta"),
        )
        install_boot_snapshot(store=store, snapshot=baseline)
        drifted = _make_snapshot(
            snapshot_id="drifted",
            shipped_invariant_names=("alpha",),
        )
        observer = InvariantDriftObserver(
            store, capture=lambda: drifted,
            posture_reader=lambda: None,
        )
        await observer.run_one_cycle()
        # vigilance_ticks should now be set
        assert observer.stats()["vigilance_ticks_remaining"] == \
            vigilance_ticks()

    @pytest.mark.asyncio
    async def test_no_drift_decays_vigilance(self, store):
        snap = _make_snapshot()
        install_boot_snapshot(store=store, snapshot=snap)
        observer = InvariantDriftObserver(
            store, capture=lambda: snap, posture_reader=lambda: None,
        )
        observer._vigilance_ticks_remaining = 3
        await observer.run_one_cycle()
        assert observer.stats()["vigilance_ticks_remaining"] == 2
        await observer.run_one_cycle()
        assert observer.stats()["vigilance_ticks_remaining"] == 1
        await observer.run_one_cycle()
        assert observer.stats()["vigilance_ticks_remaining"] == 0

    @pytest.mark.asyncio
    async def test_history_append_failure_does_not_block_cycle(
        self, store, monkeypatch,
    ):
        snap = _make_snapshot()

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(store, "append_history", boom)
        observer = InvariantDriftObserver(
            store, capture=lambda: snap, posture_reader=lambda: None,
        )
        result = await observer.run_one_cycle()
        # Cycle still completes (no baseline → empty drift)
        assert result.captured == snap


# ---------------------------------------------------------------------------
# 5. Lifecycle — start/stop/is_running
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_disabled_when_master_off(
        self, store, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "true",
        )
        observer = InvariantDriftObserver(store)
        observer.start()
        assert observer.is_running() is False

    @pytest.mark.asyncio
    async def test_start_disabled_when_observer_flag_off(
        self, store, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "false",
        )
        observer = InvariantDriftObserver(store)
        observer.start()
        assert observer.is_running() is False

    @pytest.mark.asyncio
    async def test_start_runs_when_both_flags_on(
        self, store, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "true",
        )
        # Tighter cadence so the test isn't slow
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S", "30",
        )
        observer = InvariantDriftObserver(
            store, capture=lambda: _make_snapshot(),
            posture_reader=lambda: None,
        )
        observer.start()
        assert observer.is_running() is True
        # Allow one cycle to land
        await asyncio.sleep(0.05)
        await observer.stop()
        assert observer.is_running() is False

    @pytest.mark.asyncio
    async def test_stop_cancels_quickly(self, store, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "true",
        )
        # Long interval — test that stop() doesn't wait it out
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S", "3600",
        )
        observer = InvariantDriftObserver(
            store, capture=lambda: _make_snapshot(),
            posture_reader=lambda: None,
        )
        observer.start()
        # Stop must complete in well under the cadence
        await asyncio.wait_for(observer.stop(), timeout=2.0)

    @pytest.mark.asyncio
    async def test_double_start_idempotent(self, store, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S", "3600",
        )
        observer = InvariantDriftObserver(
            store, capture=lambda: _make_snapshot(),
            posture_reader=lambda: None,
        )
        observer.start()
        first_task = observer._task
        observer.start()  # second call should be no-op
        assert observer._task is first_task
        await observer.stop()


# ---------------------------------------------------------------------------
# 6. Default observer singleton
# ---------------------------------------------------------------------------


class TestDefaultObserver:
    def test_get_default_observer_singleton(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BASE_DIR", str(tmp_path),
        )
        a = get_default_observer()
        b = get_default_observer()
        assert a is b

    def test_reset_replaces_singleton(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BASE_DIR", str(tmp_path),
        )
        a = get_default_observer()
        reset_default_observer()
        b = get_default_observer()
        assert a is not b


# ---------------------------------------------------------------------------
# 7. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
)


_ALLOWED_GOVERNANCE_SUBSTRINGS = (
    "invariant_drift_auditor",
    "invariant_drift_store",
    "posture_observer",  # for cadence multiplier
    "posture_health",  # Tier 1 #2 safe-read wrapper
    "ide_observability_stream",  # Slice 5 SSE event publish
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "invariant_drift_observer.py"
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    def test_no_forbidden_authority_imports(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in mod:
                        offenders.append(mod)
        assert offenders == [], (
            f"invariant_drift_observer.py imports forbidden "
            f"authority modules: {offenders}"
        )

    def test_governance_imports_in_allowlist(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                ok = any(
                    sub in mod
                    for sub in _ALLOWED_GOVERNANCE_SUBSTRINGS
                )
                assert ok, (
                    f"invariant_drift_observer imports unexpected "
                    f"governance module: {mod}"
                )

    def test_public_api_exported(self):
        # Slice 5 added EVENT_TYPE_INVARIANT_DRIFT_DETECTED +
        # publish_invariant_drift_detected for the observability
        # SSE event surface.
        expected_exports = {
            "EVENT_TYPE_INVARIANT_DRIFT_DETECTED",
            "InvariantDriftObserver",
            "InvariantDriftSignalEmitter",
            "ObserverTickResult",
            "backoff_ceiling_s",
            "base_interval_s",
            "dedup_window",
            "get_default_observer",
            "get_signal_emitter",
            "observer_enabled",
            "posture_multipliers",
            "publish_invariant_drift_detected",
            "register_signal_emitter",
            "reset_default_observer",
            "reset_signal_emitter",
            "vigilance_factor",
            "vigilance_ticks",
        }
        assert set(obs_mod.__all__) == expected_exports

    def test_observer_tick_result_is_frozen(self):
        result = ObserverTickResult(
            captured=None, drift_records=(),
            emitted=False, deduped=False, failure_reason=None,
        )
        with pytest.raises((AttributeError, Exception)):
            result.emitted = True  # type: ignore[misc]
