# [Ouroboros] Modified by Ouroboros (op=op-01a109df-) at 2026-10-05 02:24 UTC
# Reason: `backend/autonomy/creative_problem_solving.py` has no corresponding test module. CREATE `tests/test_creative_problem_sol

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.autonomy.creative_problem_solving import (
    ProblemType,
    SolutionApproach,
    Problem,
    CreativeSolution,
    IdeaNode,
    CreativeProblemSolver
)

async def test_import_smoke():
    """Test that module imports correctly and all classes are accessible"""
    assert ProblemType is not None
    assert SolutionApproach is not None
    assert Problem is not None
    assert CreativeSolution is not None
    assert IdeaNode is not None
    assert CreativeProblemSolver is not None
    print("✅ Import smoke test passed")
    return True

async def test_problem_to_prompt_context():
    """Test that problem converts to prompt context correctly"""
    problem = Problem(
        problem_id="test-123",
        description="Test problem",
        problem_type=ProblemType.WORKFLOW_OPTIMIZATION,
        constraints=["Time limit 2 days"],
        objectives=["Improve efficiency"],
        context={"priority": "high"},
        priority=0.8
    )
    
    prompt = problem.to_prompt_context()
    assert "Test problem" in prompt
    assert "workflow_optimization" in prompt
    print("✅ Problem to prompt context test passed")
    return True

async def test_creative_solution_get_overall_score():
    """Test that creative solution calculates overall score correctly"""
    solution = CreativeSolution(
        solution_id="sol-123",
        problem_id="prob-123",
        approach=SolutionApproach.LATERAL_THINKING,
        description="Test solution",
        implementation_steps=[],
        innovation_score=0.9,
        feasibility_score=0.7,
        impact_score=0.8,
        resources_required=["time", "money"],
        estimated_time="2 weeks",
        risks=[],
        alternatives=[],
        synergies=[]
    )
    
    score = solution.get_overall_score()
    expected = 0.9 * 0.3 + 0.7 * 0.4 + 0.8 * 0.3
    assert abs(score - expected) < 0.01
    print("✅ Creative solution overall score test passed")
    return True

async def test_creative_problem_solver_init():
    """Test that creative problem solver initializes correctly"""
    with patch('anthropic.Anthropic') as mock_claude:
        mock_instance = MagicMock()
        mock_claude.return_value = mock_instance
        
        solver = CreativeProblemSolver("test-key")
        assert solver.claude == mock_instance
        assert isinstance(solver.active_problems, dict)
        assert isinstance(solver.solution_history, list)
        assert isinstance(solver.idea_graph, dict)
        print("✅ Creative problem solver init test passed")
        return True

async def test_solve_problem_returns_top_3():
    """Test that solve_problem returns at most 3 solutions"""
    with patch('anthropic.Anthropic') as mock_claude:
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"solution": "test", "innovation_score": 0.9, "feasibility_score": 0.8, "impact_score": 0.7}')]
        mock_claude.return_value.messages.create = AsyncMock(return_value=mock_response)
        
        solver = CreativeProblemSolver("test-key")
        problem = Problem(
            problem_id="test-123",
            description="Test problem",
            problem_type=ProblemType.WORKFLOW_OPTIMIZATION,
            constraints=[],
            objectives=["Improve efficiency"],
            context={},
            priority=0.8
        )
        
        solutions = await solver.solve_problem(problem)
        assert len(solutions) <= 3
        print("✅ Solve problem returns top 3 test passed")
        return True

async def test_solution_score_threshold():
    """Test that only solutions with feasibility > 0.4 are accepted"""
    with patch('anthropic.Anthropic') as mock_claude:
        # Mock response that returns low feasibility score
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"solution": "test", "innovation_score": 0.3, "feasibility_score": 0.3, "impact_score": 0.2}')]
        mock_claude.return_value.messages.create = AsyncMock(return_value=mock_response)
        
        solver = CreativeProblemSolver("test-key")
        problem = Problem(
            problem_id="test-123",
            description="Test problem",
            problem_type=ProblemType.WORKFLOW_OPTIMIZATION,
            constraints=[],
            objectives=["Improve efficiency"],
            context={},
            priority=0.8
        )
        
        solutions = await solver.solve_problem(problem)
        # Should be empty because feasibility < 0.4
        assert len(solutions) == 0
        print("✅ Solution score threshold test passed")
        return True

async def run_all_tests():
    """Run all tests for creative problem solving module"""
    tests = [
        test_import_smoke,
        test_problem_to_prompt_context,
        test_creative_solution_get_overall_score,
        test_creative_problem_solver_init,
        test_solve_problem_returns_top_3,
        test_solution_score_threshold
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
    print(" CREATIVE PROBLEM SOLVING - TEST SUMMARY")
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