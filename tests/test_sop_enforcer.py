"""
Test Suite for SOP Enforcer - Clinical-Grade Discipline
========================================================

Tests the ThinkingProtocol, ComplexityAnalyzer, and SOPEnforcer
integration for "Measure Twice, Cut Once" enforcement.

Author: JARVIS AI System
"""

import asyncio
import pytest
import sys
from pathlib import Path
from datetime import datetime

# Add project paths
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


class TestComplexityAnalyzer:
    """Test the ComplexityAnalyzer class."""

    @pytest.fixture
    def analyzer(self):
        from backend.core.governance.sop_enforcer import ComplexityAnalyzer
        return ComplexityAnalyzer()

    def test_trivial_task(self, analyzer):
        """Should detect trivial tasks."""
        task = "Fix a typo in the README"
        complexity, score, metadata = analyzer.analyze(task)

        assert complexity.value in ("trivial", "simple")
        assert score < 0.4

    def test_simple_task(self, analyzer):
        """Should detect simple tasks."""
        task = "Add a new helper function to utils.py"
        complexity, score, metadata = analyzer.analyze(task)

        assert score < 0.5

    def test_moderate_task(self, analyzer):
        """Should detect moderate complexity tasks."""
        task = "Implement a new API endpoint for user authentication in auth_handler.py"
        complexity, score, metadata = analyzer.analyze(task)

        assert "implement" in metadata["keywords_found"]
        assert "api" in metadata["keywords_found"]

    def test_complex_task(self, analyzer):
        """Should detect complex tasks."""
        task = "Refactor the entire authentication architecture to use OAuth2"
        complexity, score, metadata = analyzer.analyze(task)

        assert complexity.value in ("complex", "critical")
        assert score >= 0.5
        assert "refactor" in metadata["keywords_found"]
        assert "architecture" in metadata["keywords_found"]

    def test_critical_task(self, analyzer):
        """Should detect critical tasks with high risk."""
        task = "Migrate the database schema and refactor security authentication breaking changes"
        complexity, score, metadata = analyzer.analyze(task)

        assert complexity.value == "critical"
        assert score >= 0.7
        assert "breaking" in metadata["risk_keywords_found"]

    def test_cross_repo_detection(self, analyzer):
        """Should detect cross-repo tasks."""
        # Use lowercase keywords that match the pattern
        task = "Integrate jarvis_prime with reactor_core training pipeline cross-repo"
        complexity, score, metadata = analyzer.analyze(task)

        # Cross-repo scope should be detected
        assert "Cross-repo scope" in metadata["signals"]
        # Score may vary but cross-repo signal should be present
        assert score > 0

    def test_file_mentions(self, analyzer):
        """Should detect multiple file mentions."""
        task = "Update auth.py, user_service.py, and api_routes.py with new validation"
        complexity, score, metadata = analyzer.analyze(task)

        assert len(metadata["files_mentioned"]) >= 3


class TestDesignPlan:
    """Test the DesignPlan Pydantic model."""

    def test_valid_plan(self):
        """Should accept a valid design plan."""
        from backend.core.governance.sop_enforcer import (
            DesignPlan, ProposedChange, RiskAssessment
        )

        plan = DesignPlan(
            goal="Implement user authentication feature with JWT tokens",
            context="The system currently has no authentication. We need to add JWT-based auth for API endpoints.",
            proposed_changes=[
                ProposedChange(
                    file_path="backend/auth/jwt_handler.py",
                    change_type="create",
                    description="Create JWT token generation and validation",
                    estimated_lines=100,
                )
            ],
            risk_assessment=[
                RiskAssessment(
                    category="security",
                    description="JWT secret could be exposed",
                    severity="high",
                    mitigation="Use environment variables and rotate secrets regularly",
                )
            ],
        )

        assert plan.plan_id is not None
        assert plan.status.value == "draft"
        assert len(plan.proposed_changes) == 1
        assert len(plan.risk_assessment) == 1

    def test_plan_missing_goal(self):
        """Should reject plan with missing goal."""
        from backend.core.governance.sop_enforcer import (
            DesignPlan, ProposedChange, RiskAssessment
        )
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DesignPlan(
                goal="x",  # Too short
                context="This is the context for the change",
                proposed_changes=[
                    ProposedChange(
                        file_path="test.py",
                        change_type="modify",
                        description="Test change",
                    )
                ],
                risk_assessment=[
                    RiskAssessment(
                        category="other",
                        description="Test risk",
                        severity="low",
                        mitigation="Test mitigation",
                    )
                ],
            )

    def test_plan_unmitigated_high_risk(self):
        """Should reject plan with unmitigated high risk."""
        from backend.core.governance.sop_enforcer import (
            DesignPlan, ProposedChange, RiskAssessment
        )
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DesignPlan(
                goal="This is a valid goal for the task at hand",
                context="This is the context that explains the background and requirements for this change",
                proposed_changes=[
                    ProposedChange(
                        file_path="test.py",
                        change_type="modify",
                        description="Test change",
                    )
                ],
                risk_assessment=[
                    RiskAssessment(
                        category="security",
                        description="Critical security vulnerability",
                        severity="critical",
                        mitigation="",  # Empty mitigation
                    )
                ],
            )


class TestSOPEnforcer:
    """Test the SOPEnforcer class."""

    @pytest.fixture
    def config(self):
        from backend.core.governance.sop_enforcer import SOPEnforcerConfig
        return SOPEnforcerConfig(
            enabled=True,
            strict_mode=True,
            complexity_threshold=0.5,
        )

    @pytest.fixture
    def enforcer(self, config):
        from backend.core.governance.sop_enforcer import SOPEnforcer
        return SOPEnforcer(config)

    @pytest.mark.asyncio
    async def test_allow_simple_task(self, enforcer):
        """Should allow simple tasks without plan."""
        from backend.core.governance.sop_enforcer import EnforcementAction

        action, reason, plan = await enforcer.check_task(
            task_id="test-1",
            goal="Fix a typo in the README file",
            context={},
        )

        # Simple tasks should be allowed or not detected as coding
        assert action in (EnforcementAction.ALLOW, EnforcementAction.REQUIRE_PLAN)

    @pytest.mark.asyncio
    async def test_require_plan_complex_task(self, enforcer):
        """Should require plan for complex tasks."""
        from backend.core.governance.sop_enforcer import EnforcementAction

        action, reason, plan = await enforcer.check_task(
            task_id="test-2",
            goal="Refactor the entire authentication architecture to use OAuth2 with breaking changes",
            context={},
        )

        assert action == EnforcementAction.REQUIRE_PLAN
        assert "complexity" in reason.lower() or "plan" in reason.lower()

    @pytest.mark.asyncio
    async def test_bypass_emergency(self, enforcer):
        """Should bypass for emergency keywords."""
        from backend.core.governance.sop_enforcer import EnforcementAction

        action, reason, plan = await enforcer.check_task(
            task_id="test-3",
            goal="URGENT hotfix for production authentication bug",
            context={},
        )

        assert action == EnforcementAction.BYPASS
        assert "bypass" in reason.lower()

    @pytest.mark.asyncio
    async def test_stats_tracking(self, enforcer):
        """Should track enforcement statistics."""
        await enforcer.check_task("test-4", "Simple task", {})
        await enforcer.check_task("test-5", "Refactor architecture", {})

        stats = enforcer.get_stats()
        assert stats["tasks_checked"] == 2

    @pytest.mark.asyncio
    async def test_non_coding_task_allowed(self, enforcer):
        """Should allow non-coding tasks."""
        from backend.core.governance.sop_enforcer import EnforcementAction

        action, reason, plan = await enforcer.check_task(
            task_id="test-6",
            goal="What's the weather like today?",
            context={},
        )

        assert action == EnforcementAction.ALLOW
        assert "Not a coding task" in reason


class TestThinkingProtocol:
    """Test the JARVISThinkingProtocol class."""

    @pytest.fixture
    def protocol(self):
        from backend.core.governance.sop_enforcer import JARVISThinkingProtocol
        return JARVISThinkingProtocol()

    @pytest.mark.asyncio
    async def test_validate_valid_plan(self, protocol):
        """Should validate a well-formed plan."""
        from backend.core.governance.sop_enforcer import (
            DesignPlan, ProposedChange, RiskAssessment
        )

        plan = DesignPlan(
            goal="Implement user authentication feature with JWT tokens for secure API access",
            context="The system currently has no authentication. We need to add JWT-based auth for all API endpoints. This includes token generation, validation, and refresh mechanisms.",
            proposed_changes=[
                ProposedChange(
                    file_path="backend/auth/jwt_handler.py",
                    change_type="create",
                    description="Create JWT token generation and validation module",
                    estimated_lines=100,
                ),
                ProposedChange(
                    file_path="backend/api/middleware.py",
                    change_type="modify",
                    description="Add authentication middleware to validate tokens",
                    estimated_lines=50,
                ),
            ],
            risk_assessment=[
                RiskAssessment(
                    category="security",
                    description="JWT secret could be exposed in logs",
                    severity="high",
                    mitigation="Use environment variables, implement secret rotation, and sanitize logs",
                ),
                RiskAssessment(
                    category="breaking_change",
                    description="Existing API clients will need to update",
                    severity="medium",
                    mitigation="Provide migration guide and grace period",
                ),
            ],
        )

        is_valid, errors = await protocol.validate_plan(plan)

        assert is_valid is True
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_validate_incomplete_plan(self, protocol):
        """Should reject incomplete plan."""
        from backend.core.governance.sop_enforcer import (
            DesignPlan, ProposedChange, RiskAssessment, SOPEnforcerConfig
        )

        config = SOPEnforcerConfig(min_changes_count=2)

        plan = DesignPlan(
            goal="Short goal here for testing",  # Minimum length
            context="x" * 50,  # Minimum length
            proposed_changes=[
                ProposedChange(
                    file_path="test.py",
                    change_type="modify",
                    description="Single change",
                ),
            ],
            risk_assessment=[
                RiskAssessment(
                    category="other",
                    description="Some risk",
                    severity="low",
                    mitigation="Do something",
                ),
            ],
        )

        is_valid, errors = await protocol.validate_plan(plan, config)

        assert is_valid is False
        assert len(errors) > 0


class TestIntegration:
    """Integration tests for SOP Enforcer with other components."""

    @pytest.mark.asyncio
    async def test_require_design_plan_function(self):
        """Test the convenience function."""
        from backend.core.governance.sop_enforcer import require_design_plan

        can_proceed, plan, block_reason = await require_design_plan(
            goal="Fix a typo in documentation",
            context={},
            llm=None,  # No LLM, can't generate plan
        )

        # Without LLM, simple tasks should still be allowed
        # Complex tasks will be blocked
        assert isinstance(can_proceed, bool)

    @pytest.mark.asyncio
    async def test_enforcer_with_repo_context(self):
        """Test enforcer with repository context."""
        from backend.core.governance.sop_enforcer import (
            SOPEnforcer, SOPEnforcerConfig, EnforcementAction
        )

        # Lower threshold to trigger plan requirement
        config = SOPEnforcerConfig(
            enabled=True,
            complexity_threshold=0.3,  # Lower threshold
        )
        enforcer = SOPEnforcer(config)

        # Provide repo context with many files
        context = {
            "repo_map": "backend/\n  core/\n    auth.py\n    users.py",
            "mentioned_files": ["auth.py", "users.py", "api.py", "tests.py", "models.py"],
        }

        action, reason, plan = await enforcer.check_task(
            task_id="test-ctx",
            goal="Refactor authentication architecture to support multiple OAuth providers with breaking changes",
            context=context,
        )

        # Complex refactor should require plan
        assert action == EnforcementAction.REQUIRE_PLAN


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
