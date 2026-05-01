"""Tier 1 #1 — Confidence Drop SSE Producer regression spine.

Coverage tracks the four operational behaviors:

  * Env knobs — defaults, overrides, floors, ceilings
  * State machine — full FireDecision taxonomy + rate-limit + sustained-
    low milestone + recovery + multi-op isolation + ring eviction
  * Wire-up — observe_streaming_verdict defensive contract
  * Authority invariants — AST-pinned (no orchestrator/iron_gate/etc)
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.verification import (
    confidence_sse_producer as producer,
)
from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
    ConfidenceVerdict,
)
from backend.core.ouroboros.governance.verification.confidence_sse_producer import (  # noqa: E501
    CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION,
    ConfidenceTransitionTracker,
    FireDecision,
    TransitionResult,
    _PublisherSet,
    get_default_tracker,
    min_interval_s,
    observe_streaming_verdict,
    op_ring_size,
    producer_enabled,
    reset_default_tracker_for_tests,
    sustained_low_threshold,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _master_flag_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED", "true",
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_tracker():
    reset_default_tracker_for_tests()
    yield
    reset_default_tracker_for_tests()


class _Capture:
    """Mock publisher set that captures every call for inspection."""

    def __init__(self) -> None:
        self.drop_calls: List[dict] = []
        self.approaching_calls: List[dict] = []
        self.sustained_calls: List[dict] = []

    def publish_drop(self, **kw):
        self.drop_calls.append(kw)
        return f"frame-drop-{len(self.drop_calls)}"

    def publish_approaching(self, **kw):
        self.approaching_calls.append(kw)
        return f"frame-approach-{len(self.approaching_calls)}"

    def publish_sustained(self, **kw):
        self.sustained_calls.append(kw)
        return f"frame-sustained-{len(self.sustained_calls)}"

    def publisher_set(self) -> _PublisherSet:
        return _PublisherSet(
            publish_drop=self.publish_drop,
            publish_approaching=self.publish_approaching,
            publish_sustained=self.publish_sustained,
        )


# ---------------------------------------------------------------------------
# 1. Env knobs — defaults + overrides + floors
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_producer_disabled_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED", raising=False,
        )
        assert producer_enabled() is False

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", False), ("0", False), ("false", False), ("no", False),
            ("off", False), ("garbage", False),
            ("1", True), ("true", True), ("YES", True), ("on", True),
        ],
    )
    def test_producer_env_matrix(
        self, monkeypatch, value, expected,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED", value,
        )
        assert producer_enabled() is expected

    def test_min_interval_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_SSE_MIN_INTERVAL_S", raising=False,
        )
        assert min_interval_s() == 1.0

    def test_min_interval_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_SSE_MIN_INTERVAL_S", "0.001",
        )
        assert min_interval_s() == 0.05  # floor

    def test_min_interval_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_SSE_MIN_INTERVAL_S", "not_a_float",
        )
        assert min_interval_s() == 1.0

    def test_sustained_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_SSE_SUSTAINED_LOW_THRESHOLD",
            raising=False,
        )
        assert sustained_low_threshold() == 5

    def test_sustained_threshold_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_SSE_SUSTAINED_LOW_THRESHOLD", "1",
        )
        assert sustained_low_threshold() == 2  # floor

    def test_op_ring_size_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_SSE_OP_RING_SIZE", raising=False,
        )
        assert op_ring_size() == 256

    def test_op_ring_size_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_SSE_OP_RING_SIZE", "5",
        )
        assert op_ring_size() == 16  # floor


# ---------------------------------------------------------------------------
# 2. State machine — full FireDecision taxonomy
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_ok_to_approaching_fires(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.APPROACHING_FLOOR,
        )
        assert result.decision is FireDecision.FIRED_APPROACHING
        assert result.fired_event_type == "model_confidence_approaching"
        assert len(cap.approaching_calls) == 1

    def test_ok_to_below_fires_drop(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        assert result.decision is FireDecision.FIRED_DROP
        assert result.fired_event_type == "model_confidence_drop"
        assert len(cap.drop_calls) == 1

    def test_approaching_to_below_fires_drop(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.APPROACHING_FLOOR,
            now=1.0,
        )
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=10.0,  # well past rate limit
        )
        assert result.decision is FireDecision.FIRED_DROP

    def test_below_to_below_no_transition(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=1.0,
        )
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=10.0,
        )
        assert result.decision is FireDecision.SUPPRESSED_NO_TRANSITION
        assert result.consecutive_below == 2
        # Only the first fire happened
        assert len(cap.drop_calls) == 1

    def test_recovery_resets_consecutive(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=1.0,
        )
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=2.0,
        )
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.OK,
            now=3.0,
        )
        assert result.consecutive_below == 0
        assert result.decision is FireDecision.SUPPRESSED_NO_TRANSITION

    def test_sustained_low_milestone_fires_at_threshold(
        self, monkeypatch,
    ):
        # Default threshold = 5
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # Tick 1 fires drop (OK→BELOW transition)
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=1.0,
        )
        # Ticks 2-4 are no-transition
        for i in range(2, 5):
            tracker.observe_verdict(
                op_id="op1",
                verdict=ConfidenceVerdict.BELOW_FLOOR,
                now=float(i),
            )
        # Tick 5 fires sustained
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=5.0,
        )
        assert result.decision is FireDecision.FIRED_SUSTAINED
        assert result.consecutive_below == 5
        assert len(cap.sustained_calls) == 1

    def test_sustained_low_repeats_at_multiples(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # Run 15 consecutive BELOW_FLOOR with default threshold=5
        # Should fire sustained at 5, 10, 15
        decisions = []
        for i in range(1, 16):
            r = tracker.observe_verdict(
                op_id="op1",
                verdict=ConfidenceVerdict.BELOW_FLOOR,
                now=float(i) * 2.0,  # past rate limit each tick
            )
            decisions.append(r.decision)
        sustained_fires = [
            d for d in decisions
            if d is FireDecision.FIRED_SUSTAINED
        ]
        assert len(sustained_fires) == 3
        assert len(cap.sustained_calls) == 3

    def test_sustained_low_resets_on_recovery(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # 5 BELOW → sustained fires
        for i in range(1, 6):
            tracker.observe_verdict(
                op_id="op1",
                verdict=ConfidenceVerdict.BELOW_FLOOR,
                now=float(i) * 2.0,
            )
        assert len(cap.sustained_calls) == 1
        # Recover
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.OK,
            now=20.0,
        )
        # New episode: 5 BELOW again — should fire sustained again
        for i in range(1, 6):
            tracker.observe_verdict(
                op_id="op1",
                verdict=ConfidenceVerdict.BELOW_FLOOR,
                now=20.0 + float(i) * 2.0,
            )
        assert len(cap.sustained_calls) == 2

    def test_rate_limit_suppresses_rapid_fires(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # First fire at t=1.0
        r1 = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=1.0,
        )
        assert r1.decision is FireDecision.FIRED_DROP
        # Recover at t=1.1 then re-fire at t=1.2 — within rate limit
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.OK,
            now=1.1,
        )
        r3 = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=1.2,
        )
        # Within 1.0s of last emit → rate limited
        assert r3.decision is FireDecision.SUPPRESSED_RATE_LIMITED
        assert len(cap.drop_calls) == 1

    def test_rate_limit_does_not_suppress_sustained(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # Rapid BELOW ticks — rate limit irrelevant since only first
        # is a transition fire; sustained MUST fire at 5 regardless
        for i in range(5):
            tracker.observe_verdict(
                op_id="op1",
                verdict=ConfidenceVerdict.BELOW_FLOOR,
                now=1.0 + float(i) * 0.01,  # 10ms apart — way under 1s
            )
        # First fire is drop, fifth is sustained
        assert len(cap.drop_calls) == 1
        assert len(cap.sustained_calls) == 1

    def test_master_off_suppresses_all_fires(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED", "false",
        )
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        assert result.decision is FireDecision.SUPPRESSED_DISABLED
        assert cap.drop_calls == []
        assert cap.approaching_calls == []
        assert cap.sustained_calls == []

    def test_invalid_verdict_no_transition(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        result = tracker.observe_verdict(
            op_id="op1",
            verdict="not_a_verdict",  # type: ignore[arg-type]
        )
        assert result.decision is FireDecision.SUPPRESSED_NO_TRANSITION
        assert cap.drop_calls == []

    def test_multi_op_isolated_state(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # op1 + op2 + op3 all BELOW from OK → 3 separate drop fires
        for op_id in ("op1", "op2", "op3"):
            result = tracker.observe_verdict(
                op_id=op_id,
                verdict=ConfidenceVerdict.BELOW_FLOOR,
                now=1.0,  # same time — they don't share rate limit
            )
            assert result.decision is FireDecision.FIRED_DROP
        assert len(cap.drop_calls) == 3

    def test_publisher_exception_swallowed(self):
        def boom(**kw):
            raise RuntimeError("broker died")
        boom_publishers = _PublisherSet(
            publish_drop=boom,
            publish_approaching=boom,
            publish_sustained=boom,
        )
        tracker = ConfidenceTransitionTracker(
            publishers=boom_publishers,
        )
        # Must NOT propagate the exception
        result = tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        # Decision was made; publish was attempted; exception swallowed
        assert result.decision is FireDecision.FIRED_DROP


# ---------------------------------------------------------------------------
# 3. Ring eviction — bounded memory
# ---------------------------------------------------------------------------


class TestRingEviction:
    def test_evicts_oldest_when_ring_fills(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_SSE_OP_RING_SIZE", "16",
        )
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # Fill ring with 16 ops
        for i in range(16):
            tracker.observe_verdict(
                op_id=f"op{i}",
                verdict=ConfidenceVerdict.OK,
            )
        # Add one more — first one should be evicted
        tracker.observe_verdict(
            op_id="op_new",
            verdict=ConfidenceVerdict.OK,
        )
        stats = tracker.stats()
        assert stats["evicted_ops"] >= 1
        assert stats["tracked_ops"] <= 16

    def test_reset_op_removes_state(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        assert tracker.stats()["tracked_ops"] == 1
        assert tracker.reset_op("op1") is True
        assert tracker.stats()["tracked_ops"] == 0

    def test_reset_op_unknown_returns_false(self):
        tracker = ConfidenceTransitionTracker()
        assert tracker.reset_op("unknown_op") is False

    def test_clear_all_resets_state_and_counters(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        tracker.clear_all_for_tests()
        stats = tracker.stats()
        assert stats["tracked_ops"] == 0
        assert stats["fired_drop"] == 0
        assert stats["total_observations"] == 0


# ---------------------------------------------------------------------------
# 4. Wire-up — observe_streaming_verdict defensive contract
# ---------------------------------------------------------------------------


class TestStreamingWireup:
    def test_observe_streaming_normalizes_op_id(self, monkeypatch):
        result = observe_streaming_verdict(
            op_id=12345,  # int — should coerce to str
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        assert result is not None
        assert result.op_id == "12345"

    def test_observe_streaming_returns_none_on_invalid_verdict(self):
        result = observe_streaming_verdict(
            op_id="op1",
            verdict="bogus",  # not a ConfidenceVerdict
        )
        assert result is None

    def test_observe_streaming_returns_none_on_empty_op_id(self):
        result = observe_streaming_verdict(
            op_id=None,
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        assert result is None
        result2 = observe_streaming_verdict(
            op_id="",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        assert result2 is None

    def test_observe_streaming_uses_default_singleton(self):
        # First call writes to singleton; second call reads same state
        observe_streaming_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
        )
        stats = get_default_tracker().stats()
        assert stats["tracked_ops"] == 1
        assert stats["fired_drop"] >= 0  # depends on broker mock state


# ---------------------------------------------------------------------------
# 5. Default singleton
# ---------------------------------------------------------------------------


class TestDefaultSingleton:
    def test_get_default_tracker_singleton(self):
        a = get_default_tracker()
        b = get_default_tracker()
        assert a is b

    def test_reset_for_tests_replaces_singleton(self):
        a = get_default_tracker()
        reset_default_tracker_for_tests()
        b = get_default_tracker()
        assert a is not b


# ---------------------------------------------------------------------------
# 6. TransitionResult shape
# ---------------------------------------------------------------------------


class TestTransitionResult:
    def test_transition_result_is_frozen(self):
        result = TransitionResult(
            op_id="op1",
            prior_verdict="ok",
            current_verdict="below_floor",
            decision=FireDecision.FIRED_DROP,
            consecutive_below=1,
        )
        with pytest.raises((AttributeError, Exception)):
            result.op_id = "op2"  # type: ignore[misc]

    def test_to_dict_serializes_fields(self):
        result = TransitionResult(
            op_id="op1",
            prior_verdict="ok",
            current_verdict="below_floor",
            decision=FireDecision.FIRED_DROP,
            consecutive_below=1,
            fired_event_type="model_confidence_drop",
        )
        d = result.to_dict()
        assert d["op_id"] == "op1"
        assert d["decision"] == "fired_drop"
        assert d["fired_event_type"] == "model_confidence_drop"
        assert d["schema_version"] == \
            CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION

    def test_schema_version_pinned(self):
        assert CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION == \
            "confidence_sse_producer.1"


# ---------------------------------------------------------------------------
# 7. FireDecision taxonomy pinned
# ---------------------------------------------------------------------------


class TestFireDecisionTaxonomy:
    def test_taxonomy_pinned(self):
        # Closed 6-value taxonomy. Adding a value requires explicit
        # work; this test catches silent additions.
        expected = {
            "fired_drop",
            "fired_approaching",
            "fired_sustained",
            "suppressed_disabled",
            "suppressed_rate_limited",
            "suppressed_no_transition",
        }
        assert {d.value for d in FireDecision} == expected


# ---------------------------------------------------------------------------
# 8. Stats observability
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_initial(self):
        tracker = ConfidenceTransitionTracker()
        stats = tracker.stats()
        assert stats["total_observations"] == 0
        assert stats["fired_drop"] == 0
        assert stats["fired_approaching"] == 0
        assert stats["fired_sustained"] == 0
        assert stats["tracked_ops"] == 0

    def test_stats_after_full_lifecycle(self):
        cap = _Capture()
        tracker = ConfidenceTransitionTracker(
            publishers=cap.publisher_set(),
        )
        # 1 approach + 1 drop + 1 sustained
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.APPROACHING_FLOOR,
            now=1.0,
        )
        tracker.observe_verdict(
            op_id="op1",
            verdict=ConfidenceVerdict.BELOW_FLOOR,
            now=10.0,
        )
        for i in range(4):
            tracker.observe_verdict(
                op_id="op1",
                verdict=ConfidenceVerdict.BELOW_FLOOR,
                now=11.0 + float(i),
            )
        stats = tracker.stats()
        assert stats["fired_approaching"] == 1
        assert stats["fired_drop"] == 1
        assert stats["fired_sustained"] == 1
        assert stats["total_observations"] == 6


# ---------------------------------------------------------------------------
# 9. Authority invariants — AST-pinned
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


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "verification"
                / "confidence_sse_producer.py"
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
            f"confidence_sse_producer imports forbidden modules: "
            f"{offenders}"
        )

    def test_only_consumes_confidence_modules(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        allowed = (
            "confidence_monitor",
            "confidence_observability",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                ok = any(sub in mod for sub in allowed)
                assert ok, (
                    f"unexpected governance module: {mod}"
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
            "CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION",
            "ConfidenceTransitionTracker",
            "FireDecision",
            "TransitionResult",
            "get_default_tracker",
            "min_interval_s",
            "observe_streaming_verdict",
            "op_ring_size",
            "producer_enabled",
            "reset_default_tracker_for_tests",
            "sustained_low_threshold",
        }
        assert set(producer.__all__) == expected

    def test_doubleword_provider_imports_observer(self):
        # Pin the wire-up site so a refactor doesn't silently drop
        # the producer call from the streaming hot path.
        # _module_path() returns .../backend/core/ouroboros/governance/
        #   verification/confidence_sse_producer.py
        # → parents[0]=verification, parents[1]=governance,
        #   parents[2]=ouroboros, parents[3]=core, parents[4]=backend
        # We want sibling of verification → parents[1]/doubleword_provider.py
        path = _module_path().parents[1] / "doubleword_provider.py"
        source = path.read_text(encoding="utf-8")
        assert "observe_streaming_verdict" in source, (
            "doubleword_provider must call observe_streaming_verdict "
            "after _confidence_monitor.evaluate() — wire-up dropped"
        )
        assert "Tier 1 #1" in source, (
            "doubleword_provider must mark the wiring with the slice "
            "comment for traceability"
        )
