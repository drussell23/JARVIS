"""Phase B4 — FeedbackEngine SSE producer-bridge regression spine.

Coverage:
  * Env knob — default-false + asymmetric explicit-on/off semantics
  * 3-kind transition taxonomy frozen + closed
  * 3 public producer functions: master-off short-circuit + happy
    path + defensive isolation against publisher exceptions
  * Schema-version stable string
  * register_flags auto-discovery contract
  * register_shipped_invariants AST pins (default-false, frozen
    taxonomy, no authority imports) all hold against current
    source
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance import (
    feedback_engine_sse_producer as producer,
)
from backend.core.ouroboros.governance.feedback_engine_sse_producer import (
    FEEDBACK_ENGINE_SSE_PRODUCER_SCHEMA_VERSION,
    TRANSITION_CURRICULUM_BATCH_EMITTED,
    TRANSITION_MODEL_PROMOTED,
    TRANSITION_ROLLBACK_THRESHOLD_CROSSED,
    feed_curriculum_batch,
    feed_model_promoted,
    feed_rollback_threshold,
    producer_enabled,
    register_flags,
    register_shipped_invariants,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_publishes(monkeypatch) -> List[Dict[str, Any]]:
    """Replace the canonical SSE publish helper with an in-memory
    capture list. Every public producer entry routes through
    ``publish_feedback_engine_signal_event`` per design — patch
    that single point and we see every emission."""
    calls: List[Dict[str, Any]] = []

    def _fake_publish(*, transition_kind: str, payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
        calls.append({
            "transition_kind": transition_kind,
            "payload": dict(payload or {}),
        })
        return f"frame-{len(calls)}"

    import backend.core.ouroboros.governance.ide_observability_stream as stream
    monkeypatch.setattr(
        stream,
        "publish_feedback_engine_signal_event",
        _fake_publish,
    )
    return calls


@pytest.fixture(autouse=True)
def _master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED", "true",
    )
    yield


# ---------------------------------------------------------------------------
# 1. Env knob — default-false + explicit on/off
# ---------------------------------------------------------------------------


class TestEnvKnob:
    def test_default_false_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED",
            raising=False,
        )
        assert producer_enabled() is False

    def test_default_false_when_whitespace(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED",
            "   ",
        )
        assert producer_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_explicit_truthy(self, monkeypatch, value):
        monkeypatch.setenv(
            "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED",
            value,
        )
        assert producer_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "False"])
    def test_explicit_falsy(self, monkeypatch, value):
        monkeypatch.setenv(
            "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED",
            value,
        )
        assert producer_enabled() is False


# ---------------------------------------------------------------------------
# 2. Transition-kind taxonomy
# ---------------------------------------------------------------------------


class TestTransitionKindsFrozen:
    def test_three_string_constants_distinct(self):
        kinds = {
            TRANSITION_ROLLBACK_THRESHOLD_CROSSED,
            TRANSITION_MODEL_PROMOTED,
            TRANSITION_CURRICULUM_BATCH_EMITTED,
        }
        assert len(kinds) == 3
        assert TRANSITION_ROLLBACK_THRESHOLD_CROSSED == "rollback_threshold_crossed"
        assert TRANSITION_MODEL_PROMOTED == "model_promoted"
        assert TRANSITION_CURRICULUM_BATCH_EMITTED == "curriculum_batch_emitted"

    def test_valid_set_exactly_three(self):
        assert producer._VALID_TRANSITION_KINDS == frozenset({
            TRANSITION_ROLLBACK_THRESHOLD_CROSSED,
            TRANSITION_MODEL_PROMOTED,
            TRANSITION_CURRICULUM_BATCH_EMITTED,
        })

    def test_unknown_kind_dropped_silently(self, captured_publishes):
        # Bypass the public API to test the inner _publish guard.
        result = producer._publish(
            transition_kind="bogus_kind",
            payload={"x": 1},
        )
        assert result is None
        assert captured_publishes == []


# ---------------------------------------------------------------------------
# 3. Public producer entries — happy path + master-off short-circuit
# ---------------------------------------------------------------------------


class TestRollbackThreshold:
    def test_happy_path_publishes(self, captured_publishes):
        result = feed_rollback_threshold(
            brain_id="claude_haiku_4_5",
            rollback_count=10,
            threshold=5,
            weight_delta=-0.1,
        )
        assert result is True
        assert len(captured_publishes) == 1
        call = captured_publishes[0]
        assert call["transition_kind"] == TRANSITION_ROLLBACK_THRESHOLD_CROSSED
        assert call["payload"]["brain_id"] == "claude_haiku_4_5"
        assert call["payload"]["rollback_count"] == 10
        assert call["payload"]["threshold"] == 5
        assert call["payload"]["weight_delta"] == pytest.approx(-0.1)

    def test_empty_brain_id_silent(self, captured_publishes):
        result = feed_rollback_threshold(
            brain_id="",
            rollback_count=10,
            threshold=5,
        )
        assert result is False
        assert captured_publishes == []

    def test_master_off_silent(self, captured_publishes, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED",
            "false",
        )
        result = feed_rollback_threshold(
            brain_id="claude",
            rollback_count=10,
            threshold=5,
        )
        assert result is False
        assert captured_publishes == []

    def test_threshold_clamped_to_min_one(self, captured_publishes):
        result = feed_rollback_threshold(
            brain_id="claude",
            rollback_count=10,
            threshold=0,
        )
        assert result is True
        assert captured_publishes[0]["payload"]["threshold"] == 1


class TestModelPromoted:
    def test_happy_path_publishes(self, captured_publishes):
        result = feed_model_promoted(
            model_id="qwen3_5_397b",
            previous_model_id="qwen3_400b",
            source_event_file="reactor_2026_05_10_001.json",
            repo="jarvis",
        )
        assert result is True
        call = captured_publishes[0]
        assert call["transition_kind"] == TRANSITION_MODEL_PROMOTED
        assert call["payload"]["model_id"] == "qwen3_5_397b"
        assert call["payload"]["previous_model_id"] == "qwen3_400b"
        assert call["payload"]["repo"] == "jarvis"

    def test_empty_model_id_silent(self, captured_publishes):
        result = feed_model_promoted(
            model_id="   ",  # whitespace only
            source_event_file="r.json",
        )
        assert result is False
        assert captured_publishes == []

    def test_master_off_silent(self, monkeypatch, captured_publishes):
        monkeypatch.setenv(
            "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED",
            "0",
        )
        result = feed_model_promoted(
            model_id="qwen3_5_397b",
        )
        assert result is False
        assert captured_publishes == []


class TestCurriculumBatch:
    def test_happy_path_publishes(self, captured_publishes):
        result = feed_curriculum_batch(
            source_curriculum_id="curriculum_2026_05_10_topk.json",
            emitted_count=7,
            rejected_count=2,
        )
        assert result is True
        call = captured_publishes[0]
        assert call["transition_kind"] == TRANSITION_CURRICULUM_BATCH_EMITTED
        assert call["payload"]["emitted_count"] == 7
        assert call["payload"]["rejected_count"] == 2

    def test_empty_batch_chatter_suppressed(self, captured_publishes):
        result = feed_curriculum_batch(
            source_curriculum_id="curriculum_x.json",
            emitted_count=0,
        )
        assert result is False
        assert captured_publishes == []

    def test_negative_rejected_clamped_to_zero(self, captured_publishes):
        result = feed_curriculum_batch(
            source_curriculum_id="c.json",
            emitted_count=3,
            rejected_count=-99,
        )
        assert result is True
        assert captured_publishes[0]["payload"]["rejected_count"] == 0


# ---------------------------------------------------------------------------
# 4. Defensive contract — never raises even when publish blows up
# ---------------------------------------------------------------------------


class TestDefensiveContract:
    def test_publish_exception_is_silent(self, monkeypatch):
        def _exploding_publish(*, transition_kind, payload=None):
            raise RuntimeError("simulated SSE outage")

        import backend.core.ouroboros.governance.ide_observability_stream as stream
        monkeypatch.setattr(
            stream,
            "publish_feedback_engine_signal_event",
            _exploding_publish,
        )
        # All three entries return False, NEVER raise.
        assert feed_rollback_threshold(brain_id="b", rollback_count=10, threshold=5) is False
        assert feed_model_promoted(model_id="m") is False
        assert feed_curriculum_batch(source_curriculum_id="c", emitted_count=1) is False

    def test_publish_helper_unimport_silent(self, monkeypatch):
        import sys
        # Simulate the canonical helper not being importable.
        original = sys.modules.get(
            "backend.core.ouroboros.governance.ide_observability_stream",
        )
        try:
            sys.modules["backend.core.ouroboros.governance.ide_observability_stream"] = None  # type: ignore[assignment]
            # Public API still returns False without raising.
            assert feed_rollback_threshold(brain_id="b", rollback_count=10, threshold=5) is False
        finally:
            if original is not None:
                sys.modules["backend.core.ouroboros.governance.ide_observability_stream"] = original


# ---------------------------------------------------------------------------
# 5. Schema version + register_flags auto-discovery
# ---------------------------------------------------------------------------


class TestSchemaAndRegistration:
    def test_schema_version_stable(self):
        assert FEEDBACK_ENGINE_SSE_PRODUCER_SCHEMA_VERSION == (
            "feedback_engine_sse_producer.1"
        )

    def test_register_flags_returns_count(self):
        class _StubRegistry:
            def __init__(self):
                self.specs = []

            def register(self, spec, *, override=False):
                self.specs.append(spec)
                return True

        registry = _StubRegistry()
        installed = register_flags(registry)
        assert installed == 1
        assert len(registry.specs) == 1
        assert registry.specs[0].name == (
            "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED"
        )
        assert registry.specs[0].default is False

    def test_register_flags_swallow_import_failure(self, monkeypatch):
        import sys
        # Force the FlagRegistry import inside register_flags to fail.
        original = sys.modules.get(
            "backend.core.ouroboros.governance.flag_registry",
        )
        try:
            sys.modules["backend.core.ouroboros.governance.flag_registry"] = None  # type: ignore[assignment]

            class _Stub:
                def register(self, spec, *, override=False):
                    raise AssertionError("should not be reached")

            assert register_flags(_Stub()) == 0
        finally:
            if original is not None:
                sys.modules["backend.core.ouroboros.governance.flag_registry"] = original


# ---------------------------------------------------------------------------
# 6. AST invariants — pins hold against today's source
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_three_invariants_registered(self):
        invs = register_shipped_invariants()
        assert len(invs) == 3
        names = {inv.invariant_name for inv in invs}
        assert names == {
            "feedback_engine_sse_producer_default_false",
            "feedback_engine_sse_producer_transition_kinds_frozen",
            "feedback_engine_sse_producer_no_authority_imports",
        }

    def test_all_invariants_pass_against_current_source(self):
        import ast as _ast
        from pathlib import Path
        src_path = Path(
            "backend/core/ouroboros/governance/"
            "feedback_engine_sse_producer.py"
        )
        source = src_path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        invs = register_shipped_invariants()
        for inv in invs:
            violations = inv.validate(tree, source)
            assert violations == (), (
                f"{inv.invariant_name} violated: {violations}"
            )

    def test_no_authority_imports_truly_absent(self):
        # AST-walk so the validator's own forbidden-list constants
        # (which appear as string literals in the validator) don't
        # self-match.
        import ast as _ast
        from pathlib import Path
        src = Path(
            "backend/core/ouroboros/governance/"
            "feedback_engine_sse_producer.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        forbidden_modules = frozenset({
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.sensor_governor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.strategic_direction",
        })
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                assert mod not in forbidden_modules, (
                    f"authority asymmetry violated: imports {mod}"
                )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    mod = alias.name or ""
                    assert mod not in forbidden_modules, (
                        f"authority asymmetry violated: imports {mod}"
                    )


# ---------------------------------------------------------------------------
# 7. SSE event type registered in canonical valid set
# ---------------------------------------------------------------------------


class TestSSEIntegration:
    def test_event_type_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FEEDBACK_ENGINE_SIGNAL,
            _VALID_EVENT_TYPES,
        )
        assert EVENT_TYPE_FEEDBACK_ENGINE_SIGNAL == "feedback_engine_signal"
        assert EVENT_TYPE_FEEDBACK_ENGINE_SIGNAL in _VALID_EVENT_TYPES

    def test_publish_helper_exported(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_feedback_engine_signal_event,
        )
        assert callable(publish_feedback_engine_signal_event)
