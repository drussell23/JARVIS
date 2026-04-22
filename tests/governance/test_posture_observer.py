"""Slice 2 regression spine — PostureStore + posture_prompt + PostureObserver
+ StrategicDirection posture-section integration.

Authority invariants re-asserted in Slice 4 graduation.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.direction_inferrer import (
    DirectionInferrer,
)
from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
    SignalBundle,
    baseline_bundle,
)
from backend.core.ouroboros.governance.posture_observer import (
    OverrideState,
    PostureObserver,
    SignalCollector,
    collector_timeout_s,
    hysteresis_window_s,
    observer_interval_s,
    override_max_h,
    reset_default_observer,
    reset_default_store,
)
from backend.core.ouroboros.governance.posture_prompt import (
    compose_posture_section,
    prompt_injection_enabled,
)
from backend.core.ouroboros.governance.posture_store import (
    POSTURE_STORE_SCHEMA,
    OverrideRecord,
    PostureStore,
    reading_from_json,
    reading_to_json,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JARVIS_DIRECTION_INFERRER") or key.startswith("JARVIS_POSTURE"):
            monkeypatch.delenv(key, raising=False)
    reset_default_store()
    reset_default_observer()
    yield
    reset_default_store()
    reset_default_observer()


@pytest.fixture
def tmp_store(tmp_path: Path) -> PostureStore:
    return PostureStore(tmp_path / ".jarvis")


def _explore_bundle() -> SignalBundle:
    return replace(baseline_bundle(), feat_ratio=0.80, test_docs_ratio=0.10)


def _harden_bundle() -> SignalBundle:
    return replace(
        baseline_bundle(),
        fix_ratio=0.75,
        postmortem_failure_rate=0.55,
        iron_gate_reject_rate=0.45,
        session_lessons_infra_ratio=0.80,
    )


def _explore_reading() -> PostureReading:
    return DirectionInferrer().infer(_explore_bundle())


def _harden_reading() -> PostureReading:
    return DirectionInferrer().infer(_harden_bundle())


# ---------------------------------------------------------------------------
# PostureStore — atomicity, schema, round-trip
# ---------------------------------------------------------------------------


class TestPostureStore:

    def test_write_then_load_current_roundtrips(self, tmp_store: PostureStore):
        reading = _explore_reading()
        tmp_store.write_current(reading)
        loaded = tmp_store.load_current()
        assert loaded is not None
        assert loaded.posture is reading.posture
        assert loaded.signal_bundle_hash == reading.signal_bundle_hash

    def test_load_current_missing_returns_none(self, tmp_store: PostureStore):
        assert tmp_store.load_current() is None

    def test_malformed_current_returns_none(self, tmp_store: PostureStore):
        tmp_store.current_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_store.current_path.write_text("{not: json", encoding="utf-8")
        assert tmp_store.load_current() is None

    def test_schema_mismatch_current_rejected(self, tmp_store: PostureStore):
        tmp_store.current_path.parent.mkdir(parents=True, exist_ok=True)
        payload = reading_to_json(_explore_reading())
        payload["schema_version"] = "2.0"
        tmp_store.current_path.write_text(json.dumps(payload), encoding="utf-8")
        assert tmp_store.load_current() is None

    def test_history_ring_buffer_trims_to_cap(self, tmp_path: Path):
        store = PostureStore(tmp_path / ".jarvis", history_size=16)
        for _ in range(25):
            store.append_history(_explore_reading())
        all_history = store.load_history()
        assert len(all_history) == 16

    def test_history_limit_returns_tail(self, tmp_store: PostureStore):
        for _ in range(10):
            tmp_store.append_history(_explore_reading())
        tail = tmp_store.load_history(limit=3)
        assert len(tail) == 3

    def test_history_missing_returns_empty(self, tmp_store: PostureStore):
        assert tmp_store.load_history() == []

    def test_audit_append_only(self, tmp_store: PostureStore):
        rec1 = OverrideRecord(
            event="set", posture=Posture.EXPLORE, who="user",
            at=time.time(), until=time.time() + 3600, reason="test",
        )
        rec2 = OverrideRecord(
            event="clear", posture=None, who="user",
            at=time.time(), until=None, reason="",
        )
        tmp_store.append_audit(rec1)
        tmp_store.append_audit(rec2)
        records = tmp_store.load_audit()
        assert len(records) == 2
        assert records[0].event == "set"
        assert records[1].event == "clear"

    def test_audit_never_truncated_large_count(self, tmp_store: PostureStore):
        for i in range(500):
            tmp_store.append_audit(OverrideRecord(
                event="set", posture=Posture.HARDEN, who="user",
                at=time.time(), until=time.time() + 3600, reason=f"r{i}",
            ))
        assert len(tmp_store.load_audit()) == 500

    def test_atomic_write_no_partial_state_after_exception(self, tmp_store: PostureStore):
        """Even if temp+rename fails, there shouldn't be a half-written
        current file. We simulate by writing twice and ensuring the file
        exists and is valid JSON."""
        reading = _explore_reading()
        tmp_store.write_current(reading)
        tmp_store.write_current(_harden_reading())
        # File must still be parseable
        loaded = tmp_store.load_current()
        assert loaded is not None

    def test_stats_reports_counts(self, tmp_store: PostureStore):
        for _ in range(3):
            tmp_store.append_history(_explore_reading())
        tmp_store.write_current(_explore_reading())
        tmp_store.append_audit(OverrideRecord(
            event="set", posture=Posture.EXPLORE, who="user",
            at=time.time(), until=None, reason="",
        ))
        stats = tmp_store.stats()
        assert stats["history_count"] == 3
        assert stats["audit_count"] == 1
        assert stats["has_current"] is True
        assert stats["schema_version"] == POSTURE_STORE_SCHEMA

    def test_clear_all_removes_triplet(self, tmp_store: PostureStore):
        tmp_store.write_current(_explore_reading())
        tmp_store.append_history(_explore_reading())
        tmp_store.append_audit(OverrideRecord(
            event="set", posture=Posture.EXPLORE, who="user",
            at=time.time(), until=None, reason="",
        ))
        tmp_store.clear_all()
        assert not tmp_store.current_path.exists()
        assert not tmp_store.history_path.exists()
        assert not tmp_store.audit_path.exists()

    def test_reading_to_json_inverse(self):
        reading = _explore_reading()
        payload = reading_to_json(reading)
        restored = reading_from_json(payload)
        assert restored is not None
        assert restored.posture is reading.posture
        assert restored.confidence == pytest.approx(reading.confidence)
        assert len(restored.evidence) == len(reading.evidence)


# ---------------------------------------------------------------------------
# Posture prompt renderer
# ---------------------------------------------------------------------------


class TestPosturePrompt:

    def test_none_reading_returns_empty_string(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        assert compose_posture_section(None) == ""

    def test_master_off_returns_empty_string(self):
        assert compose_posture_section(_explore_reading()) == ""

    def test_master_on_injection_off_returns_empty(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        monkeypatch.setenv("JARVIS_POSTURE_PROMPT_INJECTION_ENABLED", "false")
        assert compose_posture_section(_explore_reading()) == ""

    def test_both_on_renders_section(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        block = compose_posture_section(_explore_reading())
        assert "## Current Strategic Posture" in block
        assert "EXPLORE" in block
        assert "Advisory" in block

    def test_force_bypasses_env_gates(self):
        # Master flag off, injection default on, but force=True renders anyway
        block = compose_posture_section(_explore_reading(), force=True)
        assert "EXPLORE" in block

    def test_top_n_respected(self):
        block = compose_posture_section(_explore_reading(), force=True, top_n=1)
        lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
        assert len(lines) == 1

    def test_top_n_zero_coerces_to_one(self):
        block = compose_posture_section(_explore_reading(), force=True, top_n=0)
        lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
        assert len(lines) == 1

    def test_advisory_per_posture(self):
        harden = compose_posture_section(_harden_reading(), force=True)
        assert "stabilize" in harden.lower() or "tighten" in harden.lower()
        explore = compose_posture_section(_explore_reading(), force=True)
        assert "ship" in explore.lower() or "breadth" in explore.lower()

    def test_empty_evidence_fallback(self):
        # MAINTAIN-heavy reading with all-zero signals → empty meaningful evidence
        reading = DirectionInferrer().infer(baseline_bundle())
        block = compose_posture_section(reading, force=True)
        assert "baseline state" in block.lower() or "no strong signals" in block.lower()

    def test_block_under_600_chars_budget(self):
        block = compose_posture_section(_harden_reading(), force=True)
        assert len(block) < 600, f"posture block too large: {len(block)} chars"

    def test_prompt_injection_enabled_gated_by_master(self, monkeypatch):
        assert prompt_injection_enabled() is False
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        assert prompt_injection_enabled() is True
        monkeypatch.setenv("JARVIS_POSTURE_PROMPT_INJECTION_ENABLED", "false")
        assert prompt_injection_enabled() is False


# ---------------------------------------------------------------------------
# SignalCollector — real git log, real summary.json parsing
# ---------------------------------------------------------------------------


class TestSignalCollector:

    def test_commit_ratios_on_real_repo(self):
        """Run against the actual repo — feat_ratio should be > 0 for
        this codebase given its conventional-commit history."""
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        collector = SignalCollector(repo_root)
        ratios = collector.commit_ratios()
        # Must return all 4 keys, each in [0,1]
        for key in ("feat", "fix", "refactor", "test_docs"):
            assert key in ratios
            assert 0.0 <= ratios[key] <= 1.0

    def test_commit_ratios_empty_repo_yields_zero(self, tmp_path: Path):
        """Directory without git → all zeros, no crash."""
        collector = SignalCollector(tmp_path)
        ratios = collector.commit_ratios()
        assert ratios == {"feat": 0.0, "fix": 0.0, "refactor": 0.0, "test_docs": 0.0}

    def test_postmortem_rate_no_sessions_yields_zero(self, tmp_path: Path):
        collector = SignalCollector(tmp_path)
        assert collector.postmortem_failure_rate() == 0.0

    def test_postmortem_rate_from_fixture(self, tmp_path: Path):
        sessions = tmp_path / ".ouroboros" / "sessions" / "sess-1"
        sessions.mkdir(parents=True)
        summary = sessions / "summary.json"
        summary.write_text(json.dumps({
            "ops_digest": {"attempted": 10, "verified": 4},
        }))
        collector = SignalCollector(tmp_path)
        # 10 attempted, 4 verified → 6 failed / 10 = 0.6
        assert collector.postmortem_failure_rate() == pytest.approx(0.6)

    def test_open_ops_provider_honored(self, tmp_path: Path):
        collector = SignalCollector(tmp_path, open_ops_provider=lambda: 8)
        # 8 / 16 = 0.5
        assert collector.open_ops_normalized() == pytest.approx(0.5)

    def test_open_ops_provider_raising_yields_zero(self, tmp_path: Path):
        def boom():
            raise RuntimeError("boom")
        collector = SignalCollector(tmp_path, open_ops_provider=boom)
        assert collector.open_ops_normalized() == 0.0

    def test_cost_burn_from_cost_state(self, tmp_path: Path):
        cost_path = tmp_path / ".jarvis" / "cost_state.json"
        cost_path.parent.mkdir(parents=True)
        cost_path.write_text(json.dumps({
            "daily_spent_usd": 0.25, "daily_cap_usd": 1.0,
        }))
        collector = SignalCollector(tmp_path)
        assert collector.cost_burn_normalized() == pytest.approx(0.25)

    def test_cost_burn_missing_file_yields_zero(self, tmp_path: Path):
        collector = SignalCollector(tmp_path)
        assert collector.cost_burn_normalized() == 0.0

    def test_build_bundle_is_well_formed_schema(self, tmp_path: Path):
        collector = SignalCollector(tmp_path)
        bundle = collector.build_bundle()
        assert bundle.schema_version == "1.0"
        # All fields populated
        assert isinstance(bundle.feat_ratio, float)
        assert isinstance(bundle.worktree_orphan_count, int)


# ---------------------------------------------------------------------------
# OverrideState
# ---------------------------------------------------------------------------


class TestOverrideState:

    def test_cold_state_no_override(self):
        state = OverrideState()
        assert state.active_posture() is None

    def test_set_then_active(self):
        state = OverrideState()
        state.set(Posture.EXPLORE, duration_s=3600, reason="test")
        assert state.active_posture() is Posture.EXPLORE

    def test_clear_drops_override(self):
        state = OverrideState()
        state.set(Posture.EXPLORE, duration_s=3600, reason="test")
        state.clear()
        assert state.active_posture() is None

    def test_duration_clamped_to_max(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_OVERRIDE_MAX_H", "1")
        state = OverrideState()
        set_at, until = state.set(Posture.EXPLORE, duration_s=999999, reason="x")
        # Max 1h = 3600s
        assert until - set_at <= 3600.0 + 1e-3

    def test_expired_detection(self):
        state = OverrideState()
        # Set with 0-second duration → immediately expired
        state.set(Posture.EXPLORE, duration_s=0, reason="x")
        time.sleep(0.01)
        assert state.is_expired() is True
        assert state.active_posture() is None

    def test_snapshot_shape(self):
        state = OverrideState()
        state.set(Posture.HARDEN, duration_s=1800, reason="ship the fix")
        snap = state.snapshot()
        assert snap["posture"] == "HARDEN"
        assert snap["reason"] == "ship the fix"
        assert snap["until"] is not None


# ---------------------------------------------------------------------------
# PostureObserver — one-cycle, hysteresis, override, timeout
# ---------------------------------------------------------------------------


class _StubCollector:
    def __init__(self, bundle: SignalBundle) -> None:
        self.bundle = bundle
        self.calls = 0

    def build_bundle(self) -> SignalBundle:
        self.calls += 1
        return self.bundle


class _SlowCollector:
    def __init__(self, delay: float, bundle: SignalBundle) -> None:
        self.delay = delay
        self.bundle = bundle

    def build_bundle(self) -> SignalBundle:
        time.sleep(self.delay)
        return self.bundle


class _RaisingCollector:
    def build_bundle(self) -> SignalBundle:
        raise RuntimeError("collector blew up")


class TestPostureObserverCycle:

    @pytest.mark.asyncio
    async def test_cold_start_promotes_first_reading(self, tmp_store: PostureStore):
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        reading = await observer.run_one_cycle()
        assert reading is not None
        current = tmp_store.load_current()
        assert current is not None
        assert current.posture is Posture.EXPLORE

    @pytest.mark.asyncio
    async def test_history_appended_even_without_promotion(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        # Force window to prevent promotion on second differing reading
        monkeypatch.setenv("JARVIS_POSTURE_HYSTERESIS_WINDOW_S", "3600")
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "2.0")

        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        await observer.run_one_cycle()
        # Swap collector to different posture; hysteresis should keep current
        observer._collector = _StubCollector(_harden_bundle())  # type: ignore[attr-defined]
        await observer.run_one_cycle()

        current = tmp_store.load_current()
        assert current is not None
        # Despite HARDEN bundle on cycle 2, EXPLORE stays current
        assert current.posture is Posture.EXPLORE
        # But history has both
        history = tmp_store.load_history()
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_high_confidence_bypasses_hysteresis(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_POSTURE_HYSTERESIS_WINDOW_S", "3600")
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.1")

        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        await observer.run_one_cycle()
        observer._collector = _StubCollector(_harden_bundle())  # type: ignore[attr-defined]
        await observer.run_one_cycle()

        current = tmp_store.load_current()
        assert current is not None
        assert current.posture is Posture.HARDEN

    @pytest.mark.asyncio
    async def test_collector_timeout_doesnt_crash_loop(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_POSTURE_COLLECTOR_TIMEOUT_S", "0.1")
        observer = PostureObserver(
            Path("."), tmp_store,
            collector=_SlowCollector(delay=1.0, bundle=_explore_bundle()),
        )
        # Should return None (timed out) but NOT raise
        reading = await observer.run_one_cycle()
        assert reading is None
        assert observer.stats()["cycles_failed"] == 1

    @pytest.mark.asyncio
    async def test_collector_exception_captured_by_outer_loop(
        self, tmp_store: PostureStore,
    ):
        observer = PostureObserver(
            Path("."), tmp_store, collector=_RaisingCollector(),
        )
        with pytest.raises(RuntimeError):
            await observer.run_one_cycle()

    @pytest.mark.asyncio
    async def test_same_posture_refreshes_current(self, tmp_store: PostureStore):
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        await observer.run_one_cycle()
        first_at = tmp_store.load_current().inferred_at  # type: ignore[union-attr]
        # Mutate the stub to force a different hash
        observer._collector = _StubCollector(  # type: ignore[attr-defined]
            replace(_explore_bundle(), feat_ratio=0.79)
        )
        await asyncio.sleep(0.01)
        await observer.run_one_cycle()
        second = tmp_store.load_current()
        assert second is not None
        assert second.posture is Posture.EXPLORE
        assert second.inferred_at >= first_at

    @pytest.mark.asyncio
    async def test_override_masks_natural_posture(self, tmp_store: PostureStore):
        override = OverrideState()
        override.set(Posture.HARDEN, duration_s=3600, reason="ops test")
        observer = PostureObserver(
            Path("."), tmp_store,
            collector=_StubCollector(_explore_bundle()),
            override_state=override,
        )
        await observer.run_one_cycle()
        # Observer still records the natural reading (EXPLORE) as current
        # per Slice 2 semantics — override-masking at render time is a
        # Slice 3 concern. What we verify here is that both history +
        # current survive the override path without crashing.
        current = tmp_store.load_current()
        assert current is not None

    @pytest.mark.asyncio
    async def test_override_expiry_writes_audit_record(self, tmp_store: PostureStore):
        override = OverrideState()
        override.set(Posture.HARDEN, duration_s=0.01, reason="brief")
        time.sleep(0.02)
        observer = PostureObserver(
            Path("."), tmp_store,
            collector=_StubCollector(_explore_bundle()),
            override_state=override,
        )
        await observer.run_one_cycle()
        records = tmp_store.load_audit()
        assert any(r.event == "expired" for r in records)

    @pytest.mark.asyncio
    async def test_on_change_hook_called_on_posture_flip(self, tmp_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.0")
        calls = []
        def hook(new, prev):
            calls.append((new.posture, prev.posture if prev else None))

        observer = PostureObserver(
            Path("."), tmp_store,
            collector=_StubCollector(_explore_bundle()),
            on_change=hook,
        )
        await observer.run_one_cycle()
        observer._collector = _StubCollector(_harden_bundle())  # type: ignore[attr-defined]
        await observer.run_one_cycle()
        assert any(c[0] is Posture.HARDEN for c in calls)

    @pytest.mark.asyncio
    async def test_start_noop_when_master_flag_off(self, tmp_store: PostureStore):
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        observer.start()
        assert observer.is_running() is False

    @pytest.mark.asyncio
    async def test_stats_shape(self, tmp_store: PostureStore):
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        await observer.run_one_cycle()
        stats = observer.stats()
        assert "cycles_ok" in stats
        assert "cycles_failed" in stats
        assert "interval_s" in stats
        assert "hysteresis_window_s" in stats


# ---------------------------------------------------------------------------
# Env defaults
# ---------------------------------------------------------------------------


class TestEnvDefaults:

    def test_observer_interval_default_300(self):
        assert observer_interval_s() == 300.0

    def test_hysteresis_window_default_900(self):
        assert hysteresis_window_s() == 900.0

    def test_collector_timeout_default_30(self):
        assert collector_timeout_s() == 30.0

    def test_override_max_default_24(self):
        assert override_max_h() == 24


# ---------------------------------------------------------------------------
# StrategicDirection integration
# ---------------------------------------------------------------------------


class TestStrategicDirectionIntegration:

    @pytest.mark.asyncio
    async def test_format_for_prompt_without_posture_when_master_off(self, tmp_path: Path):
        from backend.core.ouroboros.governance.strategic_direction import (
            StrategicDirectionService,
        )
        svc = StrategicDirectionService(tmp_path)
        # Force a minimal digest so the method returns non-empty
        svc._digest = "test digest"  # type: ignore[attr-defined]
        svc._loaded = True  # type: ignore[attr-defined]
        out = svc.format_for_prompt()
        assert "Current Strategic Posture" not in out

    @pytest.mark.asyncio
    async def test_format_for_prompt_includes_posture_when_both_flags_on(
        self, tmp_path: Path, monkeypatch,
    ):
        from backend.core.ouroboros.governance.strategic_direction import (
            StrategicDirectionService,
        )
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_store,
        )
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        # Wire default store into tmp_path
        reset_default_store()
        store = get_default_store(tmp_path / ".jarvis")
        store.write_current(_harden_reading())

        svc = StrategicDirectionService(tmp_path)
        svc._digest = "test digest"  # type: ignore[attr-defined]
        svc._loaded = True  # type: ignore[attr-defined]
        out = svc.format_for_prompt()
        assert "Current Strategic Posture" in out
        assert "HARDEN" in out

    @pytest.mark.asyncio
    async def test_format_for_prompt_omits_posture_when_injection_off(
        self, tmp_path: Path, monkeypatch,
    ):
        from backend.core.ouroboros.governance.strategic_direction import (
            StrategicDirectionService,
        )
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_store,
        )
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        monkeypatch.setenv("JARVIS_POSTURE_PROMPT_INJECTION_ENABLED", "false")
        reset_default_store()
        store = get_default_store(tmp_path / ".jarvis")
        store.write_current(_harden_reading())

        svc = StrategicDirectionService(tmp_path)
        svc._digest = "test digest"  # type: ignore[attr-defined]
        svc._loaded = True  # type: ignore[attr-defined]
        out = svc.format_for_prompt()
        assert "Current Strategic Posture" not in out

    @pytest.mark.asyncio
    async def test_format_for_prompt_no_crash_when_store_empty(
        self, tmp_path: Path, monkeypatch,
    ):
        from backend.core.ouroboros.governance.strategic_direction import (
            StrategicDirectionService,
        )
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        # No write_current → store has nothing
        svc = StrategicDirectionService(tmp_path)
        svc._digest = "test digest"  # type: ignore[attr-defined]
        svc._loaded = True  # type: ignore[attr-defined]
        out = svc.format_for_prompt()
        # Section omitted (no reading) but doesn't crash
        assert "Current Strategic Posture" not in out
        assert "test digest" in out


# ---------------------------------------------------------------------------
# Authority invariant — grep-pin, re-asserted in Slice 4
# ---------------------------------------------------------------------------


_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator",
)


class TestAuthorityInvariantSlice2:

    @pytest.mark.parametrize("relpath", [
        "backend/core/ouroboros/governance/posture_store.py",
        "backend/core/ouroboros/governance/posture_prompt.py",
        "backend/core/ouroboros/governance/posture_observer.py",
    ])
    def test_zero_authority_imports(self, relpath: str):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (repo_root / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in _AUTHORITY_MODULES:
                    if f".{forbidden}" in line:
                        bad.append(line)
        assert not bad, f"{relpath} contains authority imports: {bad}"
