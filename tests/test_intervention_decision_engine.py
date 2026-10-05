# [Ouroboros] Modified by Ouroboros (op=op-01a1097d-) at 2026-10-05 00:43 UTC
# Reason: `backend/autonomy/intervention_decision_engine.py` has no corresponding test module. CREATE `tests/test_intervention_dec

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from backend.autonomy.intervention_decision_engine import (
    UserState,
    SituationType,
    InterventionLevel,
    UserStateSignal,
    SituationAssessment,
    InterventionDecision,
    UserStateEvaluator,
    SituationAnalyzer,
    InterventionTiming,
    EffectivenessLearner,
    InterventionDecisionEngine,
    get_intervention_engine,
    test_intervention_engine
)

# Use datetime.timedelta instead of asyncio.timedelta
from datetime import timedelta


class TestUserStateEvaluator:
    def test_evaluate_user_state(self):
        evaluator = UserStateEvaluator()
        signals = [
            UserStateSignal(
                signal_type="vision",
                strength=0.8,
                confidence=0.9,
                source="camera"
            )
        ]
        context = {}
        state, confidence = evaluator.evaluate_user_state(signals, context)
        assert isinstance(state, UserState)
        assert 0.0 <= confidence <= 1.0

    def test_save_patterns(self):
        evaluator = UserStateEvaluator()
        # Test that save_patterns doesn't raise an exception
        try:
            evaluator.save_patterns()
        except Exception as e:
            pytest.fail(f"save_patterns raised an exception: {e}")

class TestSituationAnalyzer:
    def test_assess_situation(self):
        analyzer = SituationAnalyzer()
        context = {
            "error_detected": True
        }
        user_state = UserState.FRUSTRATED
        assessment = analyzer.assess_situation(context, user_state)
        assert assessment is not None
        assert assessment.situation_type == SituationType.CRITICAL_ERROR

    def test_assess_situation_no_situation(self):
        analyzer = SituationAnalyzer()
        context = {
            "normal_context": True
        }
        user_state = UserState.IDLE
        assessment = analyzer.assess_situation(context, user_state)
        assert assessment is None

class TestInterventionTiming:
    def test_calculate_optimal_timing(self):
        timing = InterventionTiming()
        decision = InterventionDecision(
            intervention_level=InterventionLevel.SILENT_MONITORING,
            timing_delay=timedelta(seconds=0),  # Set to a valid timedelta
            intervention_content={},
            reasoning="",
            confidence=0.8,
            expected_effectiveness=0.7,
            user_state=UserState.FRUSTRATED,
            situation=SituationAssessment(
                situation_type=SituationType.CRITICAL_ERROR,
                severity=0.9,
                time_criticality=0.8,
                solution_availability=0.6,
                context={},
                confidence=0.8
            )
        )
        context = {"current_task": "test"}
        delay = timing.calculate_optimal_timing(decision, UserState.FRUSTRATED, context)
        assert isinstance(delay, timedelta)

    def test_record_timing_outcome(self):
        timing = InterventionTiming()
        decision = InterventionDecision(
            intervention_level=InterventionLevel.SILENT_MONITORING,
            timing_delay=timedelta(seconds=0),  # Set to a valid timedelta
            intervention_content={},
            reasoning="",
            confidence=0.8,
            expected_effectiveness=0.7,
            user_state=UserState.FRUSTRATED,
            situation=SituationAssessment(
                situation_type=SituationType.CRITICAL_ERROR,
                severity=0.9,
                time_criticality=0.8,
                solution_availability=0.6,
                context={},
                confidence=0.8
            )
        )
        timing.record_timing_outcome(decision, timedelta(seconds=10), 0.8)
        # Should not raise an exception


class TestEffectivenessLearner:
    def test_record_intervention_outcome(self):
        learner = EffectivenessLearner()
        decision = InterventionDecision(
            intervention_level=InterventionLevel.SILENT_MONITORING,
            timing_delay=timedelta(seconds=0),  # Set to a valid timedelta
            intervention_content={},
            reasoning="",
            confidence=0.8,
            expected_effectiveness=0.7,
            user_state=UserState.FRUSTRATED,
            situation=SituationAssessment(
                situation_type=SituationType.CRITICAL_ERROR,
                severity=0.9,
                time_criticality=0.8,
                solution_availability=0.6,
                context={},
                confidence=0.8
            )
        )
        learner.record_intervention_outcome(decision, "positive", True, 0.8)
        # Should not raise an exception

    def test_predict_effectiveness(self):
        learner = EffectivenessLearner()
        decision = InterventionDecision(
            intervention_level=InterventionLevel.SILENT_MONITORING,
            timing_delay=timedelta(seconds=0),  # Set to a valid timedelta
            intervention_content={},
            reasoning="",
            confidence=0.8,
            expected_effectiveness=0.7,
            user_state=UserState.FRUSTRATED,
            situation=SituationAssessment(
                situation_type=SituationType.CRITICAL_ERROR,
                severity=0.9,
                time_criticality=0.8,
                solution_availability=0.6,
                context={},
                confidence=0.8
            )
        )
        effectiveness = learner.predict_effectiveness(decision)
        assert 0.0 <= effectiveness <= 1.0

class TestInterventionDecisionEngine:
    def test_init(self):
        engine = InterventionDecisionEngine()
        assert isinstance(engine, InterventionDecisionEngine)

    @pytest.mark.asyncio
    async def test_evaluate_intervention_need_no_situation(self):
        engine = InterventionDecisionEngine()
        # Mock the situation analyzer to return None
        with patch.object(engine.situation_analyzer, 'assess_situation', return_value=None):
            context = {"normal_context": True}
            result = await engine.evaluate_intervention_need(context)
            assert result is None

    @pytest.mark.asyncio
    async def test_evaluate_intervention_need_silent_monitoring(self):
        engine = InterventionDecisionEngine()
        # Mock the situation analyzer to return a situation
        assessment = SituationAssessment(
            situation_type=SituationType.CRITICAL_ERROR,
            severity=0.5,
            time_criticality=0.5,
            solution_availability=0.5,
            context={},
            confidence=0.8
        )
        with patch.object(engine.situation_analyzer, 'assess_situation', return_value=assessment):
            with patch.object(engine, '_decide_intervention_level', return_value=InterventionLevel.SILENT_MONITORING):
                context = {"normal_context": True}
                result = await engine.evaluate_intervention_need(context)
                assert result is None  # Should return None for silent monitoring

    @pytest.mark.asyncio
    async def test_evaluate_intervention_need_success(self):
        engine = InterventionDecisionEngine()
        assessment = SituationAssessment(
            situation_type=SituationType.CRITICAL_ERROR,
            severity=0.9,
            time_criticality=0.8,
            solution_availability=0.6,
            context={},
            confidence=0.8
        )
        with patch.object(engine.situation_analyzer, 'assess_situation', return_value=assessment):
            with patch.object(engine, '_decide_intervention_level', return_value=InterventionLevel.GENTLE_SUGGESTION):
                context = {"normal_context": True}
                result = await engine.evaluate_intervention_need(context)
                assert isinstance(result, InterventionDecision)

    @pytest.mark.asyncio
    async def test_execute_intervention(self):
        engine = InterventionDecisionEngine()
        decision = InterventionDecision(
            intervention_level=InterventionLevel.GENTLE_SUGGESTION,
            timing_delay=timedelta(seconds=0),  # Set to a valid timedelta
            intervention_content={"message": "test"},
            reasoning="",
            confidence=0.8,
            expected_effectiveness=0.7,
            user_state=UserState.FRUSTRATED,
            situation=SituationAssessment(
                situation_type=SituationType.CRITICAL_ERROR,
                severity=0.9,
                time_criticality=0.8,
                solution_availability=0.6,
                context={},
                confidence=0.8
            )
        )
        result = await engine.execute_intervention(decision)
        assert isinstance(result, dict)

    def test_get_performance_metrics(self):
        engine = InterventionDecisionEngine()
        metrics = engine.get_performance_metrics()
        assert isinstance(metrics, dict)

    @pytest.mark.asyncio
    async def test_generate_goal_no_situation(self):
        engine = InterventionDecisionEngine()
        # Mock the situation analyzer to return None
        with patch.object(engine.situation_analyzer, 'assess_situation', return_value=None):
            result = await engine.generate_goal({})
            assert result is None

    @pytest.mark.asyncio
    async def test_generate_goal_success(self):
        engine = InterventionDecisionEngine()
        assessment = SituationAssessment(
            situation_type=SituationType.CRITICAL_ERROR,
            severity=0.9,
            time_criticality=0.8,
            solution_availability=0.6,
            context={},
            confidence=0.8
        )
        with patch.object(engine.situation_analyzer, 'assess_situation', return_value=assessment):
            result = await engine.generate_goal({})
            assert isinstance(result, dict)
            assert "description" in result
            assert "priority" in result
            assert "source" in result

    def test_save_learned_data(self):
        engine = InterventionDecisionEngine()
        # Test that save_learned_data doesn't raise an exception
        try:
            engine.save_learned_data()
        except Exception as e:
            pytest.fail(f"save_learned_data raised an exception: {e}")

def test_get_intervention_engine():
    engine = get_intervention_engine()
    assert isinstance(engine, InterventionDecisionEngine)

@pytest.mark.asyncio
async def test_test_intervention_engine():
    # This is a smoke test - just ensure it doesn't crash
    try:
        await test_intervention_engine()
    except Exception as e:
        pytest.fail(f"test_intervention_engine raised an exception: {e}")
