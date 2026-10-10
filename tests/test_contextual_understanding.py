# [Ouroboros] Modified by Ouroboros (op=op-01a12162-) at 2026-10-09 15:59 UTC
# Reason: `backend/autonomy/contextual_understanding.py` has no corresponding test module. CREATE `tests/test_contextual_understan

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.autonomy.contextual_understanding import (
    EmotionalState,
    CognitiveLoad,
    WorkContext,
    UserBehaviorSignal,
    EmotionalProfile,
    ContextualInsight,
    ContextualUnderstandingEngine
)

async def run_all_tests():
    """Run all tests for contextual understanding module"""
    tests = [
        test_import_smoke,
        test_contextual_understanding_engine_init,
        test_extract_behavioral_signals,
        test_detect_emotional_state,
        test_assess_cognitive_load,
        test_understand_work_context,
        test_generate_contextual_insights,
        test_update_understanding,
        test_calculate_confidence,
        test_create_signal_vector,
        test_rule_based_emotion_detection,
        test_adapt_personality,
        test_generate_empathetic_response,
        test_get_understanding_stats
    ]
    
    results = []
    for test_func in tests:
        try:
            result = await test_func()
            results.append((test_func.__name__, result))
        except Exception as e:
            print(f"❌ Test '{test_func.__name__}' FAILED with exception: {e}")
            import traceback
            traceback.print_exc()
            results.append((test_func.__name__, False))
    
    # Summary
    print("\n" + "="*80)
    print(" CONTEXTUAL UNDERSTANDING - TEST SUMMARY")
    print("="*80 + "\n")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{status}: {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED!\n")
        return True
    else:
        print(f"\n⚠️  {total - passed} test(s) failed\n")
        return False

if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)


async def test_import_smoke():
    """Test that module imports correctly and all classes are accessible"""
    assert EmotionalState is not None
    assert CognitiveLoad is not None
    assert WorkContext is not None
    assert UserBehaviorSignal is not None
    assert EmotionalProfile is not None
    assert ContextualInsight is not None
    assert ContextualUnderstandingEngine is not None
    print("✅ Import smoke test passed")
    return True




async def test_extract_behavioral_signals():
    """Test behavioral signal extraction with various inputs"""
    with patch('anthropic.Anthropic') as mock_claude:
        mock_instance = MagicMock()
        mock_claude.return_value = mock_instance
        
        engine = ContextualUnderstandingEngine("test-key")
        
        # Test with empty data
        workspace_state = {}
        activity_data = {}
        signals = await engine._extract_behavioral_signals(workspace_state, activity_data)
        assert isinstance(signals, list)
        
        # Test with full data
        workspace_state = {
            'windows': [{'app_name': 'app1'}, {'app_name': 'app2'}],
            'notifications': {'badges': ['badge1', 'badge2']}
        }
        activity_data = {
            'window_switches': 5,
            'typing_speed': 80,
            'typing_variance': 10,
            'click_rate': 30,
            'click_accuracy': 0.95,
            'idle_time': 120,
            'error_corrections': 3,
            'total_actions': 100
        }
        signals = await engine._extract_behavioral_signals(workspace_state, activity_data)
        assert len(signals) == 6
        print("✅ Behavioral signal extraction test passed")
        return True






async def test_understand_work_context():
    """Test work context understanding with various window configurations"""
    with patch('anthropic.Anthropic') as mock_claude:
        mock_instance = MagicMock()
        mock_claude.return_value = mock_instance
        
        engine = ContextualUnderstandingEngine("test-key")
        
        # Test with no windows (break context)
        workspace_state = {}
        context = await engine._understand_work_context(workspace_state)
        assert context == WorkContext.BREAK
        
        # Test with development windows
        workspace_state = {
            'windows': [
                {'app_name': 'VSCode'},
                {'app_name': 'Chrome'}
            ]
        }
        context = await engine._understand_work_context(workspace_state)
        assert context in [WorkContext.DEVELOPMENT, WorkContext.ANALYTICAL_WORK]
        
        print("✅ Work context understanding test passed")
        return True


async def test_generate_contextual_insights():
    """Test contextual insight generation"""
    with patch('anthropic.Anthropic') as mock_claude:
        mock_instance = MagicMock()
        mock_claude.return_value = mock_instance
        
        engine = ContextualUnderstandingEngine("test-key")
        
        insights = await engine._generate_contextual_insights(
            EmotionalState.FOCUSED,
            CognitiveLoad.LOW,
            WorkContext.DEVELOPMENT,
            []
        )
        assert isinstance(insights, list)
        print("✅ Contextual insight generation test passed")
        return True










async def test_adapt_personality():
    """Test personality adaptation functionality"""
    with patch('anthropic.Anthropic') as mock_claude:
        mock_instance = MagicMock()
        mock_claude.return_value = mock_instance
        
        engine = ContextualUnderstandingEngine("test-key")
        
        adaptation = await engine._adapt_personality(EmotionalState.FOCUSED, WorkContext.DEVELOPMENT)
        assert isinstance(adaptation, dict)
        print("✅ Personality adaptation test passed")
        return True


async def test_generate_empathetic_response():
    """Test empathetic response generation"""
    with patch('anthropic.Anthropic') as mock_claude:
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='test response')]
        mock_claude.return_value.messages.create = AsyncMock(return_value=mock_response)
        
        engine = ContextualUnderstandingEngine("test-key")
        
        response = await engine.generate_empathetic_response('user input', {'emotional_state': EmotionalState.FOCUSED})
        assert isinstance(response, str)
        print("✅ Empathetic response generation test passed")
        return True


