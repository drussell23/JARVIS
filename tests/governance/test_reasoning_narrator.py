# [Ouroboros] Modified by Ouroboros (op=op-01a07d63-) at 2026-09-07 19:42 UTC
# Reason: First-order proof #4: author a real unit test for the untested reasoning_narrator (spec v2)  AUTHOR a new pytest test fi

import asyncio
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.governance import reasoning_narrator

def test_reasoning_trace_add_phase():
    trace = reasoning_narrator.ReasoningTrace(op_id='op-1', phases=[])
    trace.add_phase('CLASSIFY', 'because x')
    assert trace.phases == [{'phase': 'CLASSIFY', 'reasoning': 'because x'}]

def test_reasoning_trace_format_for_voice_empty():
    trace = reasoning_narrator.ReasoningTrace(op_id='op-1', phases=[])
    assert trace.format_for_voice() == ''

def test_reasoning_trace_format_for_voice_three_phases():
    trace = reasoning_narrator.ReasoningTrace(op_id='op-1', phases=[])
    for i in range(3):
        trace.add_phase(f'PHASE{i}', f'reasoning{i}')
    expected = '. '.join([f'PHASE{i}: reasoning{i}' for i in range(3)])
    assert trace.format_for_voice() == expected

def test_reasoning_trace_format_for_voice_four_phases():
    trace = reasoning_narrator.ReasoningTrace(op_id='op-1', phases=[])
    for i in range(4):
        trace.add_phase(f'PHASE{i}', f'reasoning{i}')
    # Should only include last 3 phases
    expected = '. '.join([f'PHASE{i}: reasoning{i}' for i in range(1, 4)])
    assert trace.format_for_voice() == expected

def test_reasoning_trace_format_for_log():
    trace = reasoning_narrator.ReasoningTrace(op_id='op-1', phases=[])
    trace.add_phase('CLASSIFY', 'because x')
    trace.add_phase('ROUTE', 'because y')
    expected = '\n'.join([
        f'Reasoning Trace for op-1:',
        '  [CLASSIFY] because x',
        '  [ROUTE] because y'
    ])
    assert trace.format_for_log() == expected

def test_reasoning_narrator_start_trace():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    assert trace.op_id == 'op-1'
    assert trace.phases == []

def test_reasoning_narrator_record_classify():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_classify('op-1', 'HIGH', 'risk factors')
    assert trace.phases == [{'phase': 'CLASSIFY', 'reasoning': 'Risk=HIGH because risk factors'}]

def test_reasoning_narrator_record_route():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_route('op-1', 'Doubleword', 'high complexity')
    assert trace.phases == [{'phase': 'ROUTE', 'reasoning': 'Selected Doubleword because high complexity'}]

def test_reasoning_narrator_record_generate():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_generate('op-1', 'Claude', 5, 2.3)
    assert trace.phases == [{'phase': 'GENERATE', 'reasoning': 'Claude produced 5 candidates in 2.3s'}]

def test_reasoning_narrator_record_validate_pass():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_validate('op-1', True)
    assert trace.phases == [{'phase': 'VALIDATE', 'reasoning': 'Passed on first attempt'}]

def test_reasoning_narrator_record_validate_fail():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_validate('op-1', False, 'timeout')
    assert trace.phases == [{'phase': 'VALIDATE', 'reasoning': 'Failed (timeout), entering repair'}]

def test_reasoning_narrator_record_entropy():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_entropy('op-1', 0.75, 'quadrant_a')
    assert trace.phases == [{'phase': 'ENTROPY', 'reasoning': 'Systemic=0.750, quadrant=quadrant_a'}]

def test_reasoning_narrator_record_outcome_success():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_outcome('op-1', True, 'completed successfully')
    assert trace.phases == [{'phase': 'COMPLETE', 'reasoning': 'completed successfully'}]

def test_reasoning_narrator_record_outcome_failure():
    narrator = reasoning_narrator.ReasoningNarrator()
    trace = narrator.start_trace('op-1')
    narrator.record_outcome('op-1', False, 'failed due to error')
    assert trace.phases == [{'phase': 'POSTMORTEM', 'reasoning': 'failed due to error'}]

def test_reasoning_narrator_record_calls_with_unstarted_op_id():
    narrator = reasoning_narrator.ReasoningNarrator()
    # These calls should not raise exceptions and should not modify anything
    narrator.record_classify('op-1', 'HIGH', 'risk factors')
    narrator.record_route('op-1', 'Doubleword', 'high complexity')
    narrator.record_generate('op-1', 'Claude', 5, 2.3)
    narrator.record_validate('op-1', True)
    narrator.record_entropy('op-1', 0.75, 'quadrant_a')
    narrator.record_outcome('op-1', True, 'completed successfully')

async def test_reasoning_narrator_narrate_completion():
    say_fn = AsyncMock()
    narrator = reasoning_narrator.ReasoningNarrator(say_fn=say_fn)
    trace = narrator.start_trace('op-1')
    narrator.record_classify('op-1', 'HIGH', 'risk factors')
    narrator.record_route('op-1', 'Doubleword', 'high complexity')
    
    # Test narration
    result = await narrator.narrate_completion('op-1')
    expected = 'CLASSIFY: Risk=HIGH because risk factors. ROUTE: Selected Doubleword because high complexity'
    assert result == expected
    say_fn.assert_awaited_once_with(expected)
    
    # Second call should return None
    result2 = await narrator.narrate_completion('op-1')
    assert result2 is None

async def test_reasoning_narrator_narrate_completion_unknown_op_id():
    narrator = reasoning_narrator.ReasoningNarrator()
    result = await narrator.narrate_completion('op-unknown')
    assert result is None