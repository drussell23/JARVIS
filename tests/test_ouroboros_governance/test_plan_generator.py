"""Tests for plan-generation behavior in plan-review sessions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.plan_generator import PlanGenerator


def _deadline() -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=15)


def _make_context(description: str = "Fix typo") -> OperationContext:
    return OperationContext.create(
        target_files=("backend/core/utils.py",),
        description=description,
        op_id="op-plan-test",
    )


def _plan_payload(file_path: str = "backend/core/utils.py") -> str:
    return json.dumps(
        {
            "schema_version": "plan.1",
            "approach": "Make a small targeted update before generating code.",
            "complexity": "trivial",
            "ordered_changes": [
                {
                    "file_path": file_path,
                    "change_type": "modify",
                    "description": "Update the targeted utility in place.",
                    "dependencies": [],
                    "estimated_scope": "small",
                }
            ],
            "risk_factors": [],
            "test_strategy": "Run focused unit tests.",
            "architectural_notes": "",
        }
    )


class TestPlanGeneratorPlanReviewMode:
    @pytest.mark.asyncio
    async def test_trivial_ops_skip_by_default(self, tmp_path: Path) -> None:
        generator = MagicMock()
        generator.plan = AsyncMock(return_value=_plan_payload())
        planner = PlanGenerator(generator=generator, repo_root=tmp_path)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_SHOW_PLAN_BEFORE_EXECUTE", None)
            result = await planner.generate_plan(_make_context(), _deadline())

        assert result.skipped is True
        assert result.skip_reason.startswith("trivial_op")
        generator.plan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trivial_ops_still_plan_when_review_mode_enabled(self, tmp_path: Path) -> None:
        generator = MagicMock()
        generator.plan = AsyncMock(return_value=_plan_payload())
        planner = PlanGenerator(generator=generator, repo_root=tmp_path)

        with patch.dict(os.environ, {"JARVIS_SHOW_PLAN_BEFORE_EXECUTE": "1"}, clear=False):
            result = await planner.generate_plan(_make_context(), _deadline())

        assert result.skipped is False
        assert result.complexity == "trivial"
        generator.plan.assert_awaited_once()
