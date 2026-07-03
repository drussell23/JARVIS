"""Slice 2 regression spine — PostureStore + posture_prompt + PostureObserver
+ StrategicDirection posture-section integration.

Authority invariants re-asserted in Slice 4 graduation.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
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
    recent_summaries_max,
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

    def test_master_off_returns_empty_string(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")
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
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")
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

    def test_cost_burn_bounded_memoized_no_reparse(self, tmp_path: Path, monkeypatch):
        """Fix 1 — the 300s cadence must NOT re-parse an unchanged
        cost_state.json every cycle. A growing ledger is parsed AT MOST
        ONCE (memoized on stat identity), regardless of how many reads.
        RED against the old path (json.loads on every call → O(cycles*N))."""
        import backend.core.ouroboros.governance.posture_observer as po

        cost_path = tmp_path / ".jarvis" / "cost_state.json"
        cost_path.parent.mkdir(parents=True)
        # Simulate an unbounded growing cost ledger — only the two daily
        # scalars are load-bearing; ``history`` stands in for the growth
        # that made a full json.loads pathologically slow.
        big_history = [{"op": i, "usd": 0.0001 * i} for i in range(50_000)]
        cost_path.write_text(json.dumps({
            "daily_spent_usd": 0.4, "daily_cap_usd": 1.0, "history": big_history,
        }))

        calls = {"n": 0}
        _real_loads = po.json.loads

        def _counting_loads(*a, **k):
            calls["n"] += 1
            return _real_loads(*a, **k)

        monkeypatch.setattr(po.json, "loads", _counting_loads)

        collector = SignalCollector(tmp_path)
        vals = [collector.cost_burn_normalized() for _ in range(20)]

        assert all(v == pytest.approx(0.4) for v in vals)
        # Bounded: exactly one parse across 20 reads of the unchanged file.
        assert calls["n"] == 1

    def test_cost_burn_reparse_on_file_change(self, tmp_path: Path):
        """Fix 1 — the memoized read invalidates on a real write
        (stat identity change), so a fresh daily value is picked up."""
        import os as _os

        cost_path = tmp_path / ".jarvis" / "cost_state.json"
        cost_path.parent.mkdir(parents=True)
        cost_path.write_text(json.dumps({
            "daily_spent_usd": 0.2, "daily_cap_usd": 1.0,
        }))
        collector = SignalCollector(tmp_path)
        assert collector.cost_burn_normalized() == pytest.approx(0.2)

        # Rewrite with a new value + force a distinct mtime.
        new_mtime = cost_path.stat().st_mtime + 10.0
        cost_path.write_text(json.dumps({
            "daily_spent_usd": 0.8, "daily_cap_usd": 1.0,
        }))
        _os.utime(cost_path, (new_mtime, new_mtime))
        assert collector.cost_burn_normalized() == pytest.approx(0.8)

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
    async def test_collector_exception_is_fail_soft(
        self, tmp_store: PostureStore,
    ):
        # Fix 2 — the wholesale offload substrate is fail-soft: a raising
        # collector NEVER propagates into the cadence loop. run_one_cycle
        # returns None and bumps cycles_failed (superseding the pre-fix
        # "raise propagates to outer loop" contract).
        observer = PostureObserver(
            Path("."), tmp_store, collector=_RaisingCollector(),
        )
        result = await observer.run_one_cycle()
        assert result is None
        assert observer.stats()["cycles_failed"] == 1

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
    async def test_start_noop_when_master_flag_off(self, tmp_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")
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
# Fix 2 — wholesale run_one_cycle off-loop via cooperative_fs_io.offload()
# ---------------------------------------------------------------------------


class _DualCollector:
    """Implements BOTH the sync (offloaded) and legacy async collect
    surfaces so a single fixture can prove which routing path fired."""

    def __init__(self, bundle: SignalBundle) -> None:
        self.bundle = bundle
        self.sync_calls = 0
        self.async_calls = 0

    def build_bundle(self) -> SignalBundle:
        self.sync_calls += 1
        return self.bundle

    async def build_bundle_async(self) -> SignalBundle:
        self.async_calls += 1
        return self.bundle


class TestPostureObserverWholesaleOffload:

    @pytest.mark.asyncio
    async def test_default_on_routes_through_sync_offloaded_cycle(
        self, tmp_store: PostureStore,
    ):
        dual = _DualCollector(_explore_bundle())
        observer = PostureObserver(Path("."), tmp_store, collector=dual)
        reading = await observer.run_one_cycle()
        assert reading is not None
        # Wholesale offload uses the SYNC build_bundle in a worker thread.
        assert dual.sync_calls == 1
        assert dual.async_calls == 0

    @pytest.mark.asyncio
    async def test_master_off_falls_back_to_legacy_async_path(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED", "false")
        dual = _DualCollector(_explore_bundle())
        observer = PostureObserver(Path("."), tmp_store, collector=dual)
        reading = await observer.run_one_cycle()
        assert reading is not None
        # Rollback: the legacy per-signal async collect path fires instead.
        assert dual.async_calls == 1
        assert dual.sync_calls == 0

    @pytest.mark.asyncio
    async def test_loop_stays_responsive_during_offloaded_cycle(
        self, tmp_store: PostureStore,
    ):
        """A heartbeat coroutine keeps ticking while a slow cycle runs —
        proving the whole cadence tick is off the primary loop. Were the
        0.3s sync collect running ON the loop, the heartbeat would freeze
        (near-zero ticks)."""
        ticks = {"n": 0}
        stop = asyncio.Event()

        async def _heartbeat():
            while not stop.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        hb = asyncio.create_task(_heartbeat())
        observer = PostureObserver(
            Path("."), tmp_store,
            collector=_SlowCollector(delay=0.3, bundle=_explore_bundle()),
        )
        reading = await observer.run_one_cycle()
        stop.set()
        await hb

        assert reading is not None  # cycle completed off-loop
        # 0.3s / 5ms ≈ 60 potential ticks; require a robust floor.
        assert ticks["n"] >= 15

    @pytest.mark.asyncio
    async def test_concurrent_reader_never_sees_partial_bundle(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        """Race: while an offloaded cycle atomically flips ``current``,
        a concurrent reader hammering load_current always observes a
        fully-valid prior-or-complete reading, never a torn write."""
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.0")
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        await observer.run_one_cycle()  # seed EXPLORE

        observer._collector = _SlowCollector(  # type: ignore[attr-defined]
            delay=0.2, bundle=_harden_bundle(),
        )
        seen = []
        stop = asyncio.Event()

        async def _reader():
            while not stop.is_set():
                seen.append(tmp_store.load_current())
                await asyncio.sleep(0.002)

        r = asyncio.create_task(_reader())
        await observer.run_one_cycle()
        stop.set()
        await r

        valid = {
            Posture.EXPLORE, Posture.HARDEN,
            Posture.CONSOLIDATE, Posture.MAINTAIN,
        }
        assert len(seen) > 0
        for cur in seen:
            # Prior bundle is always present + structurally complete.
            assert cur is not None
            assert cur.posture in valid
            assert isinstance(cur.confidence, float)

    @pytest.mark.asyncio
    async def test_offload_failure_keeps_prior_bundle_fail_soft(
        self, tmp_store: PostureStore,
    ):
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        await observer.run_one_cycle()
        prior = tmp_store.load_current()
        assert prior is not None and prior.posture is Posture.EXPLORE
        failed_before = observer.stats()["cycles_failed"]

        # Swap in a raising collector — the offloaded cycle fails.
        observer._collector = _RaisingCollector()  # type: ignore[attr-defined]
        result = await observer.run_one_cycle()

        # Fail-soft: no raise, prior bundle preserved (no partial mutation).
        assert result is None
        still = tmp_store.load_current()
        assert still is not None and still.posture is Posture.EXPLORE
        assert observer.stats()["cycles_failed"] == failed_before + 1


# ---------------------------------------------------------------------------
# CRITICAL fix — loop-affine on_change MUST marshal back to the loop thread
# (it was invoked inside the offload worker thread = UB: in production it
# flows into StreamEventBroker.publish -> asyncio.Queue.put_nowait ->
# loop.call_soon, which is not thread-safe from a foreign thread).
# ---------------------------------------------------------------------------


class _ThreadRecordingCollector:
    """Records which thread ``build_bundle`` ran on so a test can prove the
    cadence body genuinely offloaded to a worker (not the loop thread)."""

    def __init__(self, bundle: SignalBundle) -> None:
        self.bundle = bundle
        self.build_thread: object = None

    def build_bundle(self) -> SignalBundle:
        self.build_thread = threading.current_thread()
        return self.bundle


class TestOnChangeMarshalledToLoopThread:

    @pytest.mark.asyncio
    async def test_on_change_runs_on_loop_thread_not_worker(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        """Under the DEFAULT wholesale-offload path, on a real posture
        transition, ``on_change`` MUST fire on the loop/main thread — never
        the offload worker thread. RED against the pre-fix code that called
        ``self._on_change`` inside ``_process_bundle`` while it ran in the
        offload worker."""
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.0")
        seen_threads: list = []

        def hook(new, prev):
            seen_threads.append(threading.current_thread())

        collector = _ThreadRecordingCollector(_explore_bundle())
        observer = PostureObserver(
            Path("."), tmp_store, collector=collector, on_change=hook,
        )
        # Cold-start transition (prev=None) fires on_change.
        await observer.run_one_cycle()
        # The cycle body genuinely offloaded to a worker thread...
        assert collector.build_thread is not threading.main_thread()

        # Real transition EXPLORE -> HARDEN also fires on_change.
        observer._collector = _ThreadRecordingCollector(_harden_bundle())  # type: ignore[attr-defined]
        await observer.run_one_cycle()

        # ...yet EVERY on_change invocation landed on the loop/main thread.
        assert seen_threads, "on_change never fired on a real transition"
        for t in seen_threads:
            assert t is threading.main_thread(), (
                f"on_change ran on worker thread {getattr(t, 'name', t)!r} — "
                "loop-affine callback was NOT marshalled back to the loop"
            )

    @pytest.mark.asyncio
    async def test_legacy_async_path_still_fires_on_change_on_loop(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        """Rollback path (wholesale offload master OFF) preserves the
        on-loop on_change firing byte-behavior-identically."""
        monkeypatch.setenv("JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED", "false")
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.0")
        seen_threads: list = []

        def hook(new, prev):
            seen_threads.append(threading.current_thread())

        observer = PostureObserver(
            Path("."), tmp_store,
            collector=_DualCollector(_explore_bundle()), on_change=hook,
        )
        await observer.run_one_cycle()
        assert seen_threads
        for t in seen_threads:
            assert t is threading.main_thread()


# ---------------------------------------------------------------------------
# IMPORTANT 1 — memoization (NOT the offload) is what prevents a C-level
# GIL-hold (real multi-MB json.loads) from freezing a heartbeat. The
# existing _SlowCollector uses time.sleep which RELEASES the GIL, so it
# can't detect this — a genuine parse is needed.
# ---------------------------------------------------------------------------


class TestGilHoldMemoKeepsLoopResponsive:

    @pytest.mark.asyncio
    async def test_memo_prevents_reparse_gil_freeze(
        self, tmp_path: Path, monkeypatch,
    ):
        import backend.core.ouroboros.governance.posture_observer as po

        # Seed a genuinely large cost_state.json so json.loads is a real
        # C-level GIL hold (unlike time.sleep, which yields the GIL).
        cost_path = tmp_path / ".jarvis" / "cost_state.json"
        cost_path.parent.mkdir(parents=True)
        big_history = [
            {"op": i, "usd": 0.0001 * i, "pad": "x" * 80}
            for i in range(120_000)
        ]
        cost_path.write_text(json.dumps({
            "daily_spent_usd": 0.4, "daily_cap_usd": 1.0,
            "history": big_history,
        }))
        assert cost_path.stat().st_size > 5_000_000, "fixture not large enough"

        # Premise: a raw parse of this file is a real, measurable GIL hold —
        # NOT instantaneous (else the test would prove nothing).
        _t0 = time.perf_counter()
        json.loads(cost_path.read_text(encoding="utf-8"))
        parse_s = time.perf_counter() - _t0
        assert parse_s > 0.03, f"parse too fast to be a real GIL hold: {parse_s}s"

        # Count ONLY the multi-MB cost_state parses (isolate from any small
        # summary/arc-context parses).
        real_loads = po.json.loads
        parses = {"n": 0}

        def counting_loads(s, *a, **k):
            if isinstance(s, str) and len(s) > 1_000_000:
                parses["n"] += 1
            return real_loads(s, *a, **k)

        monkeypatch.setattr(po.json, "loads", counting_loads)
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.0")

        store = PostureStore(tmp_path / ".jarvis")
        collector = SignalCollector(tmp_path)
        observer = PostureObserver(tmp_path, store, collector=collector)

        # A "freeze" is an inter-tick gap far larger than the 5ms heartbeat
        # period — it can only happen while a C-level json.loads holds the
        # GIL (offload can't help: the GIL is process-wide). Counting freeze
        # EVENTS (not total ticks) is the robust signal: total ticks are
        # confounded by wall-time (more parses => longer run => more ticks).
        freeze_threshold = max(0.015, parse_s * 0.5)

        async def _cycles_count_freezes(n: int, reset_cache_each: bool) -> int:
            ts: list = []
            stop = asyncio.Event()

            async def _hb():
                while not stop.is_set():
                    ts.append(time.perf_counter())
                    await asyncio.sleep(0.005)

            hb = asyncio.create_task(_hb())
            for _ in range(n):
                if reset_cache_each:
                    # Defeat the memo => genuine re-parse every cycle.
                    collector._cost_burn_cache = None  # type: ignore[attr-defined]
                await observer.run_one_cycle()
            stop.set()
            await hb
            gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
            return sum(1 for g in gaps if g > freeze_threshold)

        # (a) WITHOUT the memo — reset the cache each cycle so the multi-MB
        #     file is re-parsed every cycle: the heartbeat freezes ONCE PER
        #     CYCLE (a GIL hold recurs on every tick).
        collector._cost_burn_cache = None  # type: ignore[attr-defined]
        parses["n"] = 0
        freezes_no_memo = await _cycles_count_freezes(3, reset_cache_each=True)
        parses_no_memo = parses["n"]

        # (b) WITH the memo (the fix) — parse happens exactly once (cycle 1);
        #     cycles 2+ stat-only, so the heartbeat stays responsive.
        collector._cost_burn_cache = None  # type: ignore[attr-defined]
        parses["n"] = 0
        freezes_memo = await _cycles_count_freezes(3, reset_cache_each=False)
        parses_memo = parses["n"]

        # Deterministic proof the memo bounds re-parse: 3 without, 1 with.
        assert parses_no_memo == 3
        assert parses_memo == 1
        # The freeze is real and recurring without the memo; the memo
        # confines it to the unavoidable single first parse. (offload is
        # identical in both runs — the delta is memoization alone.)
        assert freezes_no_memo >= 2, (
            f"expected recurring freezes without memo, got {freezes_no_memo}"
        )
        assert freezes_memo <= 1, (
            f"memoized path should freeze at most once, got {freezes_memo}"
        )
        assert freezes_memo < freezes_no_memo


# ---------------------------------------------------------------------------
# IMPORTANT 2 — recent_summaries: ONE shared, bounded scan per cycle across
# the four summary-derived raters (was 4x re-scan + re-parse, unbounded).
# ---------------------------------------------------------------------------


class TestSharedBoundedRecentSummaries:

    def _seed_sessions(self, tmp_path: Path, count: int) -> None:
        sessions = tmp_path / ".ouroboros" / "sessions"
        sessions.mkdir(parents=True)
        for i in range(count):
            d = sessions / f"sess-{i:03d}"
            d.mkdir()
            (d / "summary.json").write_text(json.dumps({
                "ops_digest": {"attempted": 10, "verified": 5},
                "event_counts": {
                    "generate_total": 4, "iron_gate_reject": 1,
                    "apply_total": 2, "l2_invoked": 1,
                },
                "session_lessons": [{"tag": "infra"}],
            }))

    def test_build_bundle_parses_at_most_max_and_once_per_cycle(
        self, tmp_path: Path, monkeypatch,
    ):
        """20 sessions, N=5: the four raters share ONE bounded scan, so a
        cycle parses exactly 5 summaries (newest-N) — NOT 20, and NOT 4x5.
        RED against the old path (each of 4 raters scanned all 20)."""
        import backend.core.ouroboros.governance.posture_observer as po

        monkeypatch.setenv("JARVIS_POSTURE_RECENT_SUMMARIES_MAX", "5")
        self._seed_sessions(tmp_path, 20)

        real_loads = po.json.loads
        parses = {"n": 0}

        def counting_loads(*a, **k):
            parses["n"] += 1
            return real_loads(*a, **k)

        monkeypatch.setattr(po.json, "loads", counting_loads)
        collector = SignalCollector(tmp_path)
        parses["n"] = 0
        bundle = collector.build_bundle()

        # Bounded to newest-5 AND scanned/parsed once (not 4x per rater).
        assert parses["n"] == 5
        # All four summary-derived ratings still computed + in range.
        assert 0.0 <= bundle.postmortem_failure_rate <= 1.0
        assert 0.0 <= bundle.iron_gate_reject_rate <= 1.0
        assert 0.0 <= bundle.l2_repair_rate <= 1.0
        assert 0.0 <= bundle.session_lessons_infra_ratio <= 1.0

    def test_shared_scan_matches_standalone_rater_values(
        self, tmp_path: Path,
    ):
        """The shared-scan path yields identical rater values to the legacy
        per-rater standalone scans (behavior preserved)."""
        self._seed_sessions(tmp_path, 4)
        collector = SignalCollector(tmp_path)
        shared = collector.scan_recent_summaries(
            collector._summaries_widest_window_h(), recent_summaries_max(),
        )
        assert collector.postmortem_failure_rate(shared) == (
            collector.postmortem_failure_rate()
        )
        assert collector.iron_gate_reject_rate(shared) == (
            collector.iron_gate_reject_rate()
        )
        assert collector.l2_repair_rate(shared) == collector.l2_repair_rate()
        assert collector.session_lessons_infra_ratio(shared) == (
            collector.session_lessons_infra_ratio()
        )

    def test_scan_bounds_parse_budget_before_parsing(
        self, tmp_path: Path, monkeypatch,
    ):
        """Even a huge session-count spike parses at most N (bound applied
        pre-parse, keyed on newest mtime)."""
        import backend.core.ouroboros.governance.posture_observer as po

        monkeypatch.setenv("JARVIS_POSTURE_RECENT_SUMMARIES_MAX", "3")
        self._seed_sessions(tmp_path, 40)
        real_loads = po.json.loads
        parses = {"n": 0}

        def counting_loads(*a, **k):
            parses["n"] += 1
            return real_loads(*a, **k)

        monkeypatch.setattr(po.json, "loads", counting_loads)
        collector = SignalCollector(tmp_path)
        parses["n"] = 0
        rows = collector.scan_recent_summaries(
            collector._summaries_widest_window_h(), recent_summaries_max(),
        )
        assert parses["n"] == 3
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# IMPORTANT 3 — epoch guard: a slow cycle N that completes AFTER cycle N+1
# must NOT overwrite N+1's newer reading (asyncio.wait_for timeout does not
# cancel the offload worker).
# ---------------------------------------------------------------------------


class TestStaleCycleEpochGuard:

    @pytest.mark.asyncio
    async def test_epoch_advances_each_offloaded_cycle(
        self, tmp_store: PostureStore,
    ):
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )
        assert observer._cycle_epoch == 0
        await observer.run_one_cycle()
        assert observer._cycle_epoch == 1
        await observer.run_one_cycle()
        assert observer._cycle_epoch == 2

    @pytest.mark.asyncio
    async def test_stale_cycle_does_not_clobber_newer_reading(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        """Cycle N is dispatched (epoch=1) but times out on the loop while
        its worker keeps running. Cycle N+1 (epoch=2) completes first and
        writes HARDEN. When N's worker finally reaches its write with the
        stale epoch=1, it MUST no-op — HARDEN stays current."""
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.0")
        observer = PostureObserver(
            Path("."), tmp_store, collector=_StubCollector(_explore_bundle()),
        )

        # Cycle N dispatched — epoch stamped 1 (mirrors the loop-thread stamp).
        observer._cycle_epoch += 1
        epoch_n = observer._cycle_epoch

        # Cycle N+1 runs to completion first, advancing the epoch to 2 and
        # writing HARDEN as the authoritative current.
        observer._cycle_epoch += 1
        epoch_n1 = observer._cycle_epoch
        observer._collector = _StubCollector(_harden_bundle())  # type: ignore[attr-defined]
        out_n1 = observer._run_one_cycle_sync(epoch_n1)
        assert out_n1 is not None
        observer._fire_on_change(out_n1.on_change_args)
        assert tmp_store.load_current().posture is Posture.HARDEN  # type: ignore[union-attr]

        # The STALE cycle N finally completes with an EXPLORE reading.
        observer._collector = _StubCollector(_explore_bundle())  # type: ignore[attr-defined]
        out_n = observer._run_one_cycle_sync(epoch_n)
        assert out_n is not None
        # Its write was suppressed (stale epoch) and no on_change marshalled.
        assert out_n.on_change_args is None
        # current must STILL be N+1's HARDEN — not clobbered by N's EXPLORE.
        assert tmp_store.load_current().posture is Posture.HARDEN  # type: ignore[union-attr]

        # Control: the SAME EXPLORE reading with the CURRENT epoch DOES write
        # — proving it was the stale-epoch guard (not hysteresis) that
        # suppressed the clobber above.
        out_ctrl = observer._run_one_cycle_sync(observer._cycle_epoch)
        assert out_ctrl is not None
        observer._fire_on_change(out_ctrl.on_change_args)
        assert tmp_store.load_current().posture is Posture.EXPLORE  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_legacy_inline_path_has_no_epoch_guard(
        self, tmp_store: PostureStore, monkeypatch,
    ):
        """Legacy (master-off) inline path passes epoch=None → the guard is
        inert (no overlap to race), byte-behavior-identical."""
        monkeypatch.setenv("JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED", "false")
        observer = PostureObserver(
            Path("."), tmp_store, collector=_DualCollector(_explore_bundle()),
        )
        reading = await observer.run_one_cycle()
        assert reading is not None
        assert tmp_store.load_current() is not None
        # Epoch never advanced by the legacy path.
        assert observer._cycle_epoch == 0


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
    async def test_format_for_prompt_without_posture_when_master_off(
        self, tmp_path: Path, monkeypatch,
    ):
        """Post-Wave-1-graduation: master defaults True, so must set
        =false explicitly to test the master-off path. Without this
        the live-fire-populated .jarvis/posture_current.json would
        get picked up by the default store and the section would
        render."""
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")
        from backend.core.ouroboros.governance.strategic_direction import (
            StrategicDirectionService,
        )
        svc = StrategicDirectionService(tmp_path)
        svc._digest = "test digest"  # type: ignore[attr-defined]
        svc._loaded = True  # type: ignore[attr-defined]
        out = await svc.format_for_prompt()
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
        out = await svc.format_for_prompt()
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
        out = await svc.format_for_prompt()
        assert "Current Strategic Posture" not in out

    @pytest.mark.asyncio
    async def test_format_for_prompt_no_crash_when_store_empty(
        self, tmp_path: Path, monkeypatch,
    ):
        """Store points to a tmp_path with no posture_current.json — the
        render_posture_section must gracefully produce no section rather
        than crash. Must reset the default store so the Arc-A-live-fire-
        written .jarvis/posture_current.json in the real repo doesn't
        leak in via the default singleton path."""
        from backend.core.ouroboros.governance.strategic_direction import (
            StrategicDirectionService,
        )
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_store,
        )
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        # Force the default store onto tmp_path so the real repo's
        # .jarvis/posture_current.json doesn't leak in.
        reset_default_store()
        get_default_store(tmp_path / ".jarvis")
        svc = StrategicDirectionService(tmp_path)
        svc._digest = "test digest"  # type: ignore[attr-defined]
        svc._loaded = True  # type: ignore[attr-defined]
        out = await svc.format_for_prompt()
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
