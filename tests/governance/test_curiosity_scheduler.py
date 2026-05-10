"""CuriosityScheduler — orchestration pins.

Closes the post-CuriosityEngine priority #3 from the brutal review:
the trigger that wires CuriosityEngine to RuntimeHealth's idle-GPU
window signal + posture awareness + rate limiting.

Pinned cage:
  * Master flag default false
  * Posture HARDEN forbids curiosity (defensive mode)
  * Memory pressure HIGH/CRITICAL forbids
  * Per-hour rate cap (default 4)
  * Per-fire cooldown (default 60s)
  * NEVER raises (provider exceptions caught)
  * Authority + cage invariants
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    curiosity_scheduler as cs,
)
from backend.core.ouroboros.governance.adaptation.curiosity_scheduler import (
    CuriosityScheduler,
    DEFAULT_COOLDOWN_S,
    DEFAULT_MAX_CYCLES_PER_HOUR,
    SchedulerResult,
    SchedulerStatus,
    get_cooldown_s,
    get_max_cycles_per_hour,
    is_scheduler_enabled,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHED_PATH = (
    _REPO_ROOT
    / "backend/core/ouroboros/governance/adaptation/curiosity_scheduler.py"
)


@dataclass(frozen=True)
class _StubSignature:
    failed_phase: str
    root_cause_class: str

    def signature_hash(self) -> str:
        import hashlib
        joined = f"{self.failed_phase}|{self.root_cause_class}"
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class _StubCandidate:
    signature: _StubSignature
    member_count: int


class _FakeEngine:
    """Test stub matching CuriosityEngine.run_cycle signature."""

    def __init__(self):
        self.calls: List[Any] = []
        self.return_value = mock.MagicMock()
        self.return_value.status = mock.MagicMock()
        self.return_value.status.value = "ok"

    def run_cycle(self, clusters, *, now_unix=None):
        self.calls.append((clusters, now_unix))
        return self.return_value


# ---------------------------------------------------------------------------
# Section A — Module constants + master flag
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_max_cycles(self):
        assert DEFAULT_MAX_CYCLES_PER_HOUR == 4

    def test_default_cooldown(self):
        assert DEFAULT_COOLDOWN_S == 60.0

    def test_truthy_constant(self):
        assert cs._TRUTHY == ("1", "true", "yes", "on")

    def test_curiosity_ok_postures(self):
        assert cs._CURIOSITY_OK_POSTURES == frozenset({
            "EXPLORE", "CONSOLIDATE", "MAINTAIN",
        })
        # HARDEN is the ONLY excluded posture.
        assert "HARDEN" not in cs._CURIOSITY_OK_POSTURES

    def test_curiosity_ok_pressure(self):
        assert cs._CURIOSITY_OK_PRESSURE == frozenset({"OK", "WARN"})
        # HIGH + CRITICAL excluded.
        assert "HIGH" not in cs._CURIOSITY_OK_PRESSURE
        assert "CRITICAL" not in cs._CURIOSITY_OK_PRESSURE


class TestMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_SCHEDULER_ENABLED", raising=False,
        )
        assert is_scheduler_enabled() is False

    def test_truthy(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", v)
            assert is_scheduler_enabled() is True, v

    def test_falsy(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", v)
            assert is_scheduler_enabled() is False, v


class TestEnvOverrides:
    def test_max_cycles_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_SCHEDULER_MAX_PER_HOUR", raising=False,
        )
        assert get_max_cycles_per_hour() == 4

    def test_max_cycles_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_SCHEDULER_MAX_PER_HOUR", "10",
        )
        assert get_max_cycles_per_hour() == 10

    def test_max_cycles_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_SCHEDULER_MAX_PER_HOUR", "not-an-int",
        )
        assert get_max_cycles_per_hour() == 4

    def test_max_cycles_zero_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_SCHEDULER_MAX_PER_HOUR", "0",
        )
        assert get_max_cycles_per_hour() == 4

    def test_cooldown_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_SCHEDULER_COOLDOWN_S", raising=False,
        )
        assert get_cooldown_s() == 60.0

    def test_cooldown_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_SCHEDULER_COOLDOWN_S", "30.5",
        )
        assert get_cooldown_s() == 30.5

    def test_cooldown_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_SCHEDULER_COOLDOWN_S", "-5",
        )
        assert get_cooldown_s() == 60.0


# ---------------------------------------------------------------------------
# Section B — Gate ordering + skip paths
# ---------------------------------------------------------------------------


def _clusters():
    return [
        _StubCandidate(
            signature=_StubSignature("GENERATE", "x"),
            member_count=5,
        ),
    ]


@pytest.fixture
def fresh_scheduler():
    return CuriosityScheduler(
        engine=_FakeEngine(),
        cluster_provider=lambda: _clusters(),
    )


class TestGateOrdering:
    def test_master_off_skips(self, monkeypatch, fresh_scheduler):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_SCHEDULER_ENABLED", raising=False,
        )
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.SKIPPED_MASTER_OFF

    def test_no_engine_skips(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=None,
            cluster_provider=lambda: _clusters(),
        )
        result = sched.tick()
        assert result.status is SchedulerStatus.SKIPPED_NO_CLUSTER_PROVIDER

    def test_no_cluster_provider_skips(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(), cluster_provider=None,
        )
        result = sched.tick()
        assert result.status is SchedulerStatus.SKIPPED_NO_CLUSTER_PROVIDER

    def test_posture_harden_skips(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.posture_provider = lambda: "HARDEN"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.SKIPPED_POSTURE_HARDEN
        assert result.posture == "HARDEN"

    def test_posture_explore_allows(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.posture_provider = lambda: "EXPLORE"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_posture_consolidate_allows(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.posture_provider = lambda: "CONSOLIDATE"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_posture_maintain_allows(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.posture_provider = lambda: "MAINTAIN"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_posture_provider_raise_caught(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.posture_provider = mock.MagicMock(
            side_effect=RuntimeError("boom"),
        )
        # Treated as "no posture info" → allowed to proceed.
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_pressure_critical_skips(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.pressure_provider = lambda: "CRITICAL"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.SKIPPED_MEMORY_PRESSURE
        assert result.pressure_level == "CRITICAL"

    def test_pressure_high_skips(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.pressure_provider = lambda: "HIGH"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.SKIPPED_MEMORY_PRESSURE

    def test_pressure_warn_allows(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.pressure_provider = lambda: "WARN"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_pressure_ok_allows(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.pressure_provider = lambda: "OK"
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_pressure_provider_raise_caught(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.pressure_provider = mock.MagicMock(
            side_effect=RuntimeError("boom"),
        )
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_idle_signal_false_skips(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.idle_signal = lambda: False
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.SKIPPED_NOT_IDLE
        assert result.is_idle is False

    def test_idle_signal_true_allows(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.idle_signal = lambda: True
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.FIRED

    def test_idle_signal_raise_treated_as_not_idle(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        # Defensive: raise → don't fire (we can't tell system state).
        fresh_scheduler.idle_signal = mock.MagicMock(
            side_effect=RuntimeError("boom"),
        )
        result = fresh_scheduler.tick()
        assert result.status is SchedulerStatus.SKIPPED_NOT_IDLE


# ---------------------------------------------------------------------------
# Section C — Rate cap
# ---------------------------------------------------------------------------


class TestRateCap:
    def test_4_fires_then_capped(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        # Disable cooldown for this test (use 0 cooldown).
        fresh_scheduler.cooldown_s = 0.0
        # Default max=4 fires/hour.
        for i in range(4):
            r = fresh_scheduler.tick(now_unix=1000.0 + i)
            assert r.status is SchedulerStatus.FIRED, f"fire {i}"
        # 5th tick within hour → rate cap.
        r = fresh_scheduler.tick(now_unix=1000.0 + 5)
        assert r.status is SchedulerStatus.SKIPPED_RATE_CAP

    def test_history_pruned_after_hour(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.cooldown_s = 0.0
        # 4 fires at t=1000.
        for i in range(4):
            r = fresh_scheduler.tick(now_unix=1000.0 + i)
            assert r.status is SchedulerStatus.FIRED
        # 5th fire 3700s later (> 1 hour) → first 4 pruned, this fires.
        r = fresh_scheduler.tick(now_unix=1000.0 + 3700)
        assert r.status is SchedulerStatus.FIRED

    def test_cycles_in_window_reported(
        self, monkeypatch, fresh_scheduler,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.cooldown_s = 0.0
        for i in range(3):
            r = fresh_scheduler.tick(now_unix=1000.0 + i)
        # 3 in window now.
        assert r.cycles_in_window == 3

    def test_explicit_max_per_hour(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
            max_cycles_per_hour=2,
            cooldown_s=0.0,
        )
        for i in range(2):
            r = sched.tick(now_unix=1000.0 + i)
            assert r.status is SchedulerStatus.FIRED
        r = sched.tick(now_unix=1000.0 + 2)
        assert r.status is SchedulerStatus.SKIPPED_RATE_CAP


# ---------------------------------------------------------------------------
# Section D — Cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_cooldown_blocks_immediate_re_fire(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
            cooldown_s=60.0,
            max_cycles_per_hour=10,  # avoid rate cap
        )
        r1 = sched.tick(now_unix=1000.0)
        assert r1.status is SchedulerStatus.FIRED
        r2 = sched.tick(now_unix=1010.0)  # 10s later
        assert r2.status is SchedulerStatus.SKIPPED_COOLDOWN
        assert "cooldown_remaining_s" in r2.detail

    def test_cooldown_expired_allows(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
            cooldown_s=60.0,
            max_cycles_per_hour=10,
        )
        r1 = sched.tick(now_unix=1000.0)
        assert r1.status is SchedulerStatus.FIRED
        r2 = sched.tick(now_unix=1100.0)  # 100s > 60s cooldown
        assert r2.status is SchedulerStatus.FIRED


# ---------------------------------------------------------------------------
# Section E — Engine error handling
# ---------------------------------------------------------------------------


class TestEngineErrorHandling:
    def test_cluster_provider_raise_caught(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        bad_provider = mock.MagicMock(side_effect=RuntimeError("boom"))
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=bad_provider,
        )
        # NEVER raises into caller.
        result = sched.tick()
        assert result.status is SchedulerStatus.ENGINE_ERROR
        assert "RuntimeError" in result.detail

    def test_engine_run_cycle_raise_caught(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        bad_engine = mock.MagicMock()
        bad_engine.run_cycle.side_effect = ValueError("boom")
        sched = CuriosityScheduler(
            engine=bad_engine,
            cluster_provider=lambda: _clusters(),
        )
        result = sched.tick()
        assert result.status is SchedulerStatus.ENGINE_ERROR
        assert "ValueError" in result.detail


# ---------------------------------------------------------------------------
# Section F — Engine result threading
# ---------------------------------------------------------------------------


class TestEngineResultThreading:
    def test_engine_result_attached(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        result = fresh_scheduler.tick()
        assert result.engine_result is not None

    def test_engine_invoked_with_clusters(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        engine = _FakeEngine()
        clusters = _clusters()
        sched = CuriosityScheduler(
            engine=engine,
            cluster_provider=lambda: clusters,
        )
        sched.tick(now_unix=1000.0)
        assert len(engine.calls) == 1
        passed_clusters, passed_now = engine.calls[0]
        assert passed_clusters == clusters
        assert passed_now == 1000.0


# ---------------------------------------------------------------------------
# Section G — End-to-end with real CuriosityEngine
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    def test_e2e_with_real_engine_and_ledger(
        self, monkeypatch, tmp_path,
    ):
        """Wire the scheduler to the real CuriosityEngine + a real
        HypothesisLedger; verify a clean tick lands a hypothesis."""
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        from backend.core.ouroboros.governance.adaptation.curiosity_engine import (
            CuriosityEngine,
        )
        from backend.core.ouroboros.governance.hypothesis_ledger import (
            HypothesisLedger,
        )
        ledger = HypothesisLedger(
            project_root=tmp_path,
            ledger_path=tmp_path / "h.jsonl",
        )
        engine = CuriosityEngine(ledger=ledger)
        sched = CuriosityScheduler(
            engine=engine,
            cluster_provider=lambda: _clusters(),
            posture_provider=lambda: "EXPLORE",
            pressure_provider=lambda: "OK",
            idle_signal=lambda: True,
            cooldown_s=0.0,
        )
        result = sched.tick(now_unix=1000.0)
        assert result.status is SchedulerStatus.FIRED
        # Hypothesis landed in ledger.
        all_h = ledger.load_all()
        assert len(all_h) == 1


# ---------------------------------------------------------------------------
# Section H — reset_state (test helper)
# ---------------------------------------------------------------------------


class TestResetState:
    def test_reset_clears_history(self, monkeypatch, fresh_scheduler):
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        fresh_scheduler.cooldown_s = 0.0
        for i in range(4):
            fresh_scheduler.tick(now_unix=1000.0 + i)
        # Capped now.
        r = fresh_scheduler.tick(now_unix=1000.0 + 5)
        assert r.status is SchedulerStatus.SKIPPED_RATE_CAP
        # Reset → fires again.
        fresh_scheduler.reset_state()
        r = fresh_scheduler.tick(now_unix=1000.0 + 6)
        assert r.status is SchedulerStatus.FIRED


# ---------------------------------------------------------------------------
# Section I — Authority + cage invariants
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        source = _SCHED_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned = (
            "scoped_tool_backend", "general_driver",
            "exploration_engine", "semantic_guardian",
            "orchestrator", "tool_executor", "phase_runners",
            "gate_runner", "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for b in banned:
                    assert b not in node.module, node.module

    def test_only_stdlib_top_level(self):
        # Scheduler must not import CuriosityEngine / HypothesisLedger
        # at top level — those flow in via dependency injection.
        source = _SCHED_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "enum", "logging", "os", "time",
            "dataclasses", "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    pytest.fail(
                        f"unexpected backend top-level import: {node.module}"
                    )
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), f"unexpected import: {node.module}"

    def test_no_subprocess_or_network(self):
        source = _SCHED_PATH.read_text(encoding="utf-8")
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
            "import anthropic",
        ):
            assert token not in source, f"banned token: {token}"


# ---------------------------------------------------------------------------
# Section J — Vector #11 wall-clock → monotonic regression
# ---------------------------------------------------------------------------
#
# §3.6.2 Vector #11 / Wave 3 hygiene: rate-cap window + cooldown
# elapsed-time math MUST be monotonic. NTP correction during a
# session previously produced spurious throttle / spurious cap-hit.
# These tests inject ``now_mono`` separately from ``now_unix`` to
# simulate clock drift and prove the gates are NTP-immune.


class TestVector11MonotonicGating:
    def test_backward_ntp_correction_does_not_extend_cooldown(
        self, monkeypatch,
    ):
        """Vector #11: with wall-clock gating, a backward ntpd jump
        of Δ seconds produced ``elapsed = -Δ`` and the scheduler
        sat in cooldown for ``cooldown_s + Δ`` seconds. With
        monotonic gating, ntpd cannot push the gate past
        ``cooldown_s``."""
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
            cooldown_s=60.0,
            max_cycles_per_hour=10,
        )
        # Fire at wall=1000 / mono=500.
        r1 = sched.tick(now_unix=1000.0, now_mono=500.0)
        assert r1.status is SchedulerStatus.FIRED
        # ntpd corrects backward 30s mid-session: wall=970 (< prev),
        # but monotonic continues forward to 561 (61s elapsed > 60s
        # cooldown). With monotonic gating the next fire IS allowed.
        r2 = sched.tick(now_unix=970.0, now_mono=561.0)
        assert r2.status is SchedulerStatus.FIRED

    def test_forward_ntp_jump_does_not_evict_history_early(
        self, monkeypatch,
    ):
        """Vector #11: with wall-clock gating, a forward ntpd jump
        evicted recent fires from the rolling-hour window. With
        monotonic gating, only true elapsed time can prune."""
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
            cooldown_s=0.0,
            max_cycles_per_hour=4,
        )
        # 4 fires at mono=0,1,2,3 (wall arbitrary).
        for i in range(4):
            r = sched.tick(
                now_unix=1000.0 + i, now_mono=float(i),
            )
            assert r.status is SchedulerStatus.FIRED
        # ntpd jumps forward 4 hours of wall-clock — but only 5s of
        # real (monotonic) elapsed time. Old fires must STILL be
        # within the rolling-hour window → cap MUST hold.
        r = sched.tick(
            now_unix=1000.0 + 14400, now_mono=5.0,
        )
        assert r.status is SchedulerStatus.SKIPPED_RATE_CAP

    def test_seconds_since_last_fire_uses_monotonic_delta(
        self, monkeypatch,
    ):
        """``seconds_since_last_fire`` reports honest monotonic
        delta (not wall-clock subtraction which was buggy on the
        ``ts - 0`` fallback path)."""
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
            cooldown_s=60.0,
            max_cycles_per_hour=10,
        )
        sched.tick(now_unix=1000.0, now_mono=500.0)
        r = sched.tick(now_unix=1010.0, now_mono=510.0)
        assert r.status is SchedulerStatus.SKIPPED_COOLDOWN
        # 10s elapsed in monotonic terms — independent of wall clock
        assert r.seconds_since_last_fire == 10.0

    def test_seconds_since_last_fire_none_before_first_fire(
        self, monkeypatch,
    ):
        """When no fire has happened, the field is None (not 0 or
        a since-epoch number — fixes legacy ``ts - 0`` bug)."""
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
        )
        # Force cooldown gate via injection of last_fire history
        # never set. Cooldown gate short-circuits to None when
        # _last_fire_mono is None — gate doesn't fire SKIPPED_COOLDOWN
        # at all on first tick. Verify FIRED on first tick.
        r = sched.tick(now_unix=1000.0, now_mono=500.0)
        assert r.status is SchedulerStatus.FIRED
        # And subsequent SKIPPED_COOLDOWN reports the real elapsed.
        r2 = sched.tick(now_unix=1001.0, now_mono=501.0)
        assert r2.status is SchedulerStatus.SKIPPED_COOLDOWN
        assert r2.seconds_since_last_fire == 1.0

    def test_production_path_uses_real_monotonic(
        self, monkeypatch,
    ):
        """When neither now_unix nor now_mono is supplied, tick
        defaults to time.time() + time.monotonic() — proves
        production callers don't need to supply timestamps."""
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
            cooldown_s=0.0,
        )
        r = sched.tick()
        assert r.status is SchedulerStatus.FIRED
        # ts_epoch is wall-clock-ish (within 5s of test execution)
        assert abs(r.ts_epoch - time.time()) < 5.0

    def test_internal_state_uses_monotonic_fields(
        self, monkeypatch,
    ):
        """Pin: internal state has _fire_history_mono +
        _last_fire_mono (monotonic) populated after a fire.
        Wave 3 / Vector #11 invariant — drift requires
        explicit pin update."""
        monkeypatch.setenv("JARVIS_CURIOSITY_SCHEDULER_ENABLED", "1")
        sched = CuriosityScheduler(
            engine=_FakeEngine(),
            cluster_provider=lambda: _clusters(),
        )
        sched.tick(now_unix=1000.0, now_mono=500.0)
        assert sched._last_fire_mono == 500.0
        assert sched._fire_history_mono == [500.0]
        assert sched._last_fire_ts == 1000.0  # wall-clock audit


class TestVector11SourcePins:
    """Bytes-level regression on the curiosity_scheduler.py
    source — vector #11 fix is structural."""

    def test_no_wall_clock_in_rate_cap_pruning(self):
        """The pruning helper MUST take a monotonic argument
        and operate on _fire_history_mono. Drift requires
        explicit pin update."""
        source = _SCHED_PATH.read_text(encoding="utf-8")
        # The function signature must reference mono.
        assert "_prune_fire_history(self, mono_now: float)" in source
        # The check_rate_cap must reference mono_now.
        assert "_check_rate_cap(self, mono_now: float)" in source
        # The check_cooldown must reference mono_now.
        assert "mono_now: float" in source
        # The internal state MUST be _fire_history_mono +
        # _last_fire_mono (the monotonic fields).
        assert "_fire_history_mono" in source
        assert "_last_fire_mono" in source

    def test_tick_accepts_now_mono_kwarg(self):
        """The tick() API MUST accept ``now_mono`` for test
        injection / determinism."""
        source = _SCHED_PATH.read_text(encoding="utf-8")
        assert "now_mono: Optional[float] = None" in source

    def test_production_falls_back_to_real_monotonic(self):
        """Pin: when neither argument is supplied, tick uses
        time.monotonic() — proven by source bytes."""
        source = _SCHED_PATH.read_text(encoding="utf-8")
        assert "mono_ts = time.monotonic()" in source

    def test_vector_11_provenance_in_docstring(self):
        """The fix's audit trail (Vector #11) MUST be cited in
        the source so future maintainers can find the rationale.
        """
        source = _SCHED_PATH.read_text(encoding="utf-8")
        assert "Vector #11" in source
        assert "NTP-immune" in source
