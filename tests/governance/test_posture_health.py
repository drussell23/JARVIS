"""Tier 1 #2 — PostureObserver task-death detection regression spine.

Coverage tracks:

  * Env knobs — defaults, overrides, floors
  * Health classifier — full PostureHealthStatus 4-value taxonomy
    decision tree + master-off short-circuit + defensive shapes
  * Heartbeat tracking on PostureObserver itself
  * Safe-read wrappers (safe_load_posture / safe_load_posture_value)
  * SSE debounce + degraded event publish
  * invariant_drift_observer integration
  * Authority invariants — AST-pinned
"""
from __future__ import annotations

import ast
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from backend.core.ouroboros.governance import posture_health as ph
from backend.core.ouroboros.governance.posture_health import (
    EVENT_TYPE_POSTURE_OBSERVER_DEGRADED,
    POSTURE_HEALTH_SCHEMA_VERSION,
    PostureHealthStatus,
    PostureHealthVerdict,
    degraded_threshold_multiplier,
    detection_enabled,
    evaluate_observer_health,
    failure_streak_threshold,
    reset_publish_debounce_for_tests,
    safe_load_posture,
    safe_load_posture_value,
    sse_debounce_s,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _master_flag_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "true",
    )
    yield


@pytest.fixture(autouse=True)
def _reset_debounce():
    reset_publish_debounce_for_tests()
    yield
    reset_publish_debounce_for_tests()


def _healthy_snapshot(now: Optional[float] = None) -> Dict[str, Any]:
    """Build a healthy heartbeat snapshot. Defaults to wall-clock-now
    so tests using safe_load_posture (which reads real time.time())
    classify HEALTHY without explicit ``now=`` injection."""
    base = now if now is not None else time.time()
    return {
        "is_running": True,
        "task_started": True,
        "task_done": False,
        "last_cycle_ok_at_unix": base - 30,
        "last_cycle_attempt_at_unix": base - 30,
        "consecutive_cycle_failures": 0,
        "cycles_ok": 5,
        "cycles_failed": 0,
    }


# ---------------------------------------------------------------------------
# 1. Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_detection_default_false_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", raising=False,
        )
        assert detection_enabled() is False

    @pytest.mark.parametrize(
        "value,expected",
        [("", False), ("0", False), ("false", False), ("no", False),
         ("garbage", False), ("1", True), ("true", True),
         ("YES", True), ("on", True)],
    )
    def test_detection_env_matrix(
        self, monkeypatch, value, expected,
    ):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", value,
        )
        assert detection_enabled() is expected

    def test_degraded_multiplier_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_POSTURE_HEALTH_DEGRADED_MULTIPLIER",
            raising=False,
        )
        assert degraded_threshold_multiplier() == 3.0

    def test_degraded_multiplier_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_DEGRADED_MULTIPLIER", "0.5",
        )
        assert degraded_threshold_multiplier() == 1.5  # floor

    def test_degraded_multiplier_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_DEGRADED_MULTIPLIER", "garbage",
        )
        assert degraded_threshold_multiplier() == 3.0

    def test_failure_streak_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_POSTURE_HEALTH_FAILURE_STREAK_THRESHOLD",
            raising=False,
        )
        assert failure_streak_threshold() == 3

    def test_failure_streak_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_FAILURE_STREAK_THRESHOLD", "0",
        )
        assert failure_streak_threshold() == 1  # floor

    def test_sse_debounce_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_POSTURE_HEALTH_SSE_DEBOUNCE_S", raising=False,
        )
        assert sse_debounce_s() == 60.0

    def test_sse_debounce_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_SSE_DEBOUNCE_S", "1",
        )
        assert sse_debounce_s() == 5.0  # floor


# ---------------------------------------------------------------------------
# 2. Health classifier — full PostureHealthStatus taxonomy
# ---------------------------------------------------------------------------


class TestHealthClassifier:
    def test_healthy_recent_ok_cycle(self):
        verdict = evaluate_observer_health(
            _healthy_snapshot(now=1000.0),
            interval_s=300,
            now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.HEALTHY
        assert verdict.is_degraded() is False

    def test_degraded_hung_stale_ok(self):
        snap = _healthy_snapshot(now=1000.0)
        snap["last_cycle_ok_at_unix"] = 1000.0 - 1500  # 5x interval
        snap["last_cycle_attempt_at_unix"] = 1000.0 - 100
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.DEGRADED_HUNG
        assert verdict.is_degraded() is True
        assert verdict.seconds_since_last_ok == 1500.0

    def test_degraded_failing_consecutive_failures(self):
        snap = _healthy_snapshot(now=1000.0)
        snap["consecutive_cycle_failures"] = 5
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert (
            verdict.status is PostureHealthStatus.DEGRADED_FAILING
        )
        assert verdict.consecutive_failures == 5

    def test_failing_takes_priority_over_hung(self):
        # Both stale-OK AND failure streak — failure streak first
        snap = _healthy_snapshot(now=1000.0)
        snap["last_cycle_ok_at_unix"] = 1000.0 - 1500
        snap["consecutive_cycle_failures"] = 5
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        # task_started + task_done=False checks happen earlier, but
        # for this snapshot both apply. Per decision sequence,
        # failure streak (step 5) fires before stale-OK (step 7).
        assert (
            verdict.status is PostureHealthStatus.DEGRADED_FAILING
        )

    def test_task_dead_never_started(self):
        snap = _healthy_snapshot(now=1000.0)
        snap["is_running"] = False
        snap["task_started"] = False
        snap["task_done"] = False
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.TASK_DEAD
        assert "never started" in verdict.detail

    def test_task_dead_done_crashed(self):
        snap = _healthy_snapshot(now=1000.0)
        snap["is_running"] = False
        snap["task_started"] = True
        snap["task_done"] = True
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.TASK_DEAD
        assert "done" in verdict.detail.lower()

    def test_cold_start_hung_attempted_never_completed(self):
        snap = _healthy_snapshot(now=1000.0)
        snap["last_cycle_ok_at_unix"] = None
        snap["last_cycle_attempt_at_unix"] = 1000.0 - 1500
        snap["consecutive_cycle_failures"] = 0  # not in failure streak
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.DEGRADED_HUNG
        assert "never completed" in verdict.detail

    def test_within_threshold_is_healthy(self):
        snap = _healthy_snapshot(now=1000.0)
        # Last OK at 2.5x interval — under 3x threshold
        snap["last_cycle_ok_at_unix"] = 1000.0 - 750
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.HEALTHY

    def test_master_off_returns_healthy(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "false",
        )
        # Snapshot shows TASK_DEAD but master off → HEALTHY
        snap = _healthy_snapshot(now=1000.0)
        snap["is_running"] = False
        snap["task_started"] = False
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.HEALTHY
        assert "master flag off" in verdict.detail

    def test_malformed_snapshot_returns_healthy(self):
        verdict = evaluate_observer_health(
            "not a dict",  # type: ignore[arg-type]
            interval_s=300,
            now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.HEALTHY

    def test_partial_snapshot_handled_defensively(self):
        # Missing keys default — observer never started
        verdict = evaluate_observer_health(
            {}, interval_s=300, now=1000.0,
        )
        # task_started defaults False → TASK_DEAD
        assert verdict.status is PostureHealthStatus.TASK_DEAD

    def test_threshold_scales_with_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_DEGRADED_MULTIPLIER", "10.0",
        )
        snap = _healthy_snapshot(now=1000.0)
        snap["last_cycle_ok_at_unix"] = 1000.0 - 1500  # 5x interval
        # With 10x multiplier, 5x interval is healthy
        verdict = evaluate_observer_health(
            snap, interval_s=300, now=1000.0,
        )
        assert verdict.status is PostureHealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# 3. PostureHealthVerdict shape
# ---------------------------------------------------------------------------


class TestVerdictShape:
    def test_verdict_is_frozen(self):
        v = evaluate_observer_health(
            _healthy_snapshot(), interval_s=300,
        )
        with pytest.raises((AttributeError, Exception)):
            v.detail = "x"  # type: ignore[misc]

    def test_to_dict_serializes_all_fields(self):
        v = evaluate_observer_health(
            _healthy_snapshot(now=1000.0),
            interval_s=300, now=1000.0,
        )
        d = v.to_dict()
        assert d["status"] == "healthy"
        assert d["schema_version"] == POSTURE_HEALTH_SCHEMA_VERSION
        for k in (
            "detail", "seconds_since_last_ok",
            "consecutive_failures", "interval_s",
            "threshold_multiplier",
        ):
            assert k in d

    def test_schema_version_pinned(self):
        assert POSTURE_HEALTH_SCHEMA_VERSION == "posture_health.1"


# ---------------------------------------------------------------------------
# 4. PostureHealthStatus closed-taxonomy pin
# ---------------------------------------------------------------------------


class TestStatusTaxonomy:
    def test_taxonomy_pinned(self):
        expected = {
            "healthy", "degraded_hung",
            "degraded_failing", "task_dead",
        }
        assert {s.value for s in PostureHealthStatus} == expected


# ---------------------------------------------------------------------------
# 5. Safe-read wrappers
# ---------------------------------------------------------------------------


class _FakeStore:
    """Mock store with controllable load_current."""

    def __init__(self, reading: Optional[Any] = None) -> None:
        self._reading = reading
        self.load_calls = 0

    def load_current(self) -> Optional[Any]:
        self.load_calls += 1
        return self._reading


class _FakeObserver:
    """Mock observer with controllable health snapshot."""

    def __init__(
        self, snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._snapshot = snapshot or _healthy_snapshot()
        self.snapshot_calls = 0

    def task_health_snapshot(self) -> Dict[str, Any]:
        self.snapshot_calls += 1
        return dict(self._snapshot)


def _reading(value: str = "EXPLORE") -> Any:
    return SimpleNamespace(
        posture=SimpleNamespace(value=value),
        confidence=0.9,
    )


class TestSafeLoadPosture:
    def test_healthy_returns_store_reading(self):
        store = _FakeStore(reading=_reading("EXPLORE"))
        observer = _FakeObserver(snapshot=_healthy_snapshot())
        result = safe_load_posture(
            observer=observer, store=store, interval_s=300,
        )
        assert result is not None
        assert result.posture.value == "EXPLORE"
        assert store.load_calls == 1

    def test_degraded_returns_none(self):
        store = _FakeStore(reading=_reading("EXPLORE"))
        # Observer reports task dead
        bad_snap = _healthy_snapshot()
        bad_snap["is_running"] = False
        bad_snap["task_started"] = False
        observer = _FakeObserver(snapshot=bad_snap)
        result = safe_load_posture(
            observer=observer, store=store, interval_s=300,
        )
        assert result is None
        # Store NOT consulted since observer is degraded
        assert store.load_calls == 0

    def test_no_observer_treated_as_dead(self):
        store = _FakeStore(reading=_reading("EXPLORE"))
        result = safe_load_posture(
            observer=None, store=store, interval_s=300,
        )
        assert result is None
        # Store NOT consulted
        assert store.load_calls == 0

    def test_no_store_returns_none(self):
        observer = _FakeObserver()
        result = safe_load_posture(
            observer=observer, store=None, interval_s=300,
        )
        assert result is None

    def test_master_off_passes_through_to_store(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "false",
        )
        store = _FakeStore(reading=_reading("EXPLORE"))
        # Even with task_dead observer, master-off bypasses health
        bad_snap = _healthy_snapshot()
        bad_snap["task_started"] = False
        observer = _FakeObserver(snapshot=bad_snap)
        result = safe_load_posture(
            observer=observer, store=store, interval_s=300,
        )
        assert result is not None  # store consulted directly
        assert store.load_calls == 1

    def test_classifier_exception_falls_through_to_store(self):
        store = _FakeStore(reading=_reading("EXPLORE"))

        class _BoomObserver:
            def task_health_snapshot(self):
                raise RuntimeError("snapshot blew up")

        result = safe_load_posture(
            observer=_BoomObserver(),
            store=store, interval_s=300,
        )
        # Per docstring: "On any error in the health check itself,
        # fall through to store.load_current()"
        assert result is not None
        assert store.load_calls == 1

    def test_store_exception_returns_none(self):
        class _BoomStore:
            def load_current(self):
                raise OSError("disk error")
        observer = _FakeObserver(snapshot=_healthy_snapshot())
        result = safe_load_posture(
            observer=observer,
            store=_BoomStore(), interval_s=300,
        )
        assert result is None

    def test_safe_load_posture_value_returns_string(self):
        store = _FakeStore(reading=_reading("HARDEN"))
        observer = _FakeObserver(snapshot=_healthy_snapshot())
        value = safe_load_posture_value(
            observer=observer, store=store, interval_s=300,
        )
        assert value == "HARDEN"

    def test_safe_load_posture_value_returns_none_on_degraded(self):
        store = _FakeStore(reading=_reading("HARDEN"))
        bad_snap = _healthy_snapshot()
        bad_snap["task_started"] = False
        observer = _FakeObserver(snapshot=bad_snap)
        value = safe_load_posture_value(
            observer=observer, store=store, interval_s=300,
        )
        assert value is None


# ---------------------------------------------------------------------------
# 6. SSE debounce + degraded event
# ---------------------------------------------------------------------------


class TestSSEDebounce:
    def test_event_constant_pinned(self):
        assert EVENT_TYPE_POSTURE_OBSERVER_DEGRADED == \
            "posture_observer_degraded"

    def test_master_off_no_publish(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "false",
        )
        verdict = PostureHealthVerdict(
            status=PostureHealthStatus.DEGRADED_HUNG,
            detail="x",
            seconds_since_last_ok=1500.0,
            consecutive_failures=0,
            interval_s=300,
            threshold_multiplier=3.0,
        )
        result = ph._maybe_publish_degraded_event(verdict)
        assert result is None

    def test_healthy_verdict_not_published(self):
        verdict = PostureHealthVerdict(
            status=PostureHealthStatus.HEALTHY,
            detail="x", seconds_since_last_ok=10.0,
            consecutive_failures=0, interval_s=300,
            threshold_multiplier=3.0,
        )
        result = ph._maybe_publish_degraded_event(verdict)
        assert result is None

    def test_debounce_suppresses_rapid_publishes(
        self, monkeypatch,
    ):
        # Patch broker to capture publishes
        published = []

        class _FakeBroker:
            def publish(self, **kw):
                published.append(kw)
                return f"frame-{len(published)}"

        def _fake_get_broker():
            return _FakeBroker()

        # Patch the lazy import target
        import sys
        import types
        fake_mod = types.ModuleType(
            "backend.core.ouroboros.governance.ide_observability_stream",
        )
        fake_mod.get_default_broker = _fake_get_broker
        monkeypatch.setitem(
            sys.modules,
            "backend.core.ouroboros.governance.ide_observability_stream",  # noqa: E501
            fake_mod,
        )

        verdict = PostureHealthVerdict(
            status=PostureHealthStatus.DEGRADED_HUNG,
            detail="x",
            seconds_since_last_ok=1500.0,
            consecutive_failures=0,
            interval_s=300,
            threshold_multiplier=3.0,
        )
        # First fire publishes
        r1 = ph._maybe_publish_degraded_event(verdict)
        assert r1 == "frame-1"
        # Immediate second fire suppressed
        r2 = ph._maybe_publish_degraded_event(verdict)
        assert r2 is None
        # Only 1 publish landed
        assert len(published) == 1


# ---------------------------------------------------------------------------
# 7. PostureObserver heartbeat tracking integration
# ---------------------------------------------------------------------------


class TestPostureObserverHeartbeat:
    def test_observer_exposes_task_health_snapshot(self):
        # Ensure the new method exists on PostureObserver class.
        from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
            PostureObserver,
        )
        assert hasattr(
            PostureObserver, "task_health_snapshot",
        )

    def test_initial_snapshot_indicates_not_started(self, tmp_path):
        from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
            PostureObserver,
        )
        from backend.core.ouroboros.governance.posture_store import (
            PostureStore,
        )
        store = PostureStore(tmp_path)
        observer = PostureObserver(
            project_root=tmp_path, store=store,
        )
        snap = observer.task_health_snapshot()
        assert snap["task_started"] is False
        assert snap["last_cycle_ok_at_unix"] is None
        assert snap["last_cycle_attempt_at_unix"] is None
        assert snap["consecutive_cycle_failures"] == 0
        assert snap["cycles_ok"] == 0


# ---------------------------------------------------------------------------
# 8. Authority invariants — AST-pinned
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
    "invariant_drift",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "posture_health.py"
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
            f"posture_health imports forbidden modules: {offenders}"
        )

    def test_module_does_not_perform_disk_writes(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        forbidden_tokens = (
            ".write_text(",
            ".write_bytes(",
            "os.replace(",
            "NamedTemporaryFile",
        )
        for tok in forbidden_tokens:
            assert tok not in source, (
                f"forbidden disk-write token: {tok!r}"
            )

    def test_public_api_exported(self):
        expected = {
            "EVENT_TYPE_POSTURE_OBSERVER_DEGRADED",
            "POSTURE_HEALTH_SCHEMA_VERSION",
            "PostureHealthStatus",
            "PostureHealthVerdict",
            "degraded_threshold_multiplier",
            "detection_enabled",
            "evaluate_observer_health",
            "failure_streak_threshold",
            "reset_publish_debounce_for_tests",
            "safe_load_posture",
            "safe_load_posture_value",
            "sse_debounce_s",
        }
        assert set(ph.__all__) == expected

    def test_invariant_drift_observer_uses_safe_wrapper(self):
        # Pin the wire-up site so a refactor doesn't silently drop
        # the safe-read call from invariant_drift_observer's
        # default posture reader.
        path = _module_path().parent / "invariant_drift_observer.py"
        source = path.read_text(encoding="utf-8")
        assert "safe_load_posture_value" in source, (
            "invariant_drift_observer must call safe_load_posture_"
            "value via posture_health — wire-up dropped"
        )
        assert "Tier 1 #2" in source, (
            "invariant_drift_observer must mark wiring with slice "
            "comment for traceability"
        )
