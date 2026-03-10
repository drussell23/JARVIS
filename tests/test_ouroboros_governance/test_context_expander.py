"""Tests for ContextExpander — bounded pre-generation context expansion."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase


def _make_ctx(
    target_files: tuple = ("foo.py",),
    description: str = "test op",
) -> OperationContext:
    ctx = OperationContext.create(target_files=target_files, description=description)
    ctx = ctx.advance(OperationPhase.ROUTE)
    ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
    return ctx


def _make_generator(response: str) -> MagicMock:
    gen = MagicMock()
    gen.plan = AsyncMock(return_value=response)
    return gen


class TestContextExpanderConstants:
    def test_max_rounds_is_2(self):
        from backend.core.ouroboros.governance.context_expander import MAX_ROUNDS
        assert MAX_ROUNDS == 2

    def test_max_files_per_round_is_5(self):
        from backend.core.ouroboros.governance.context_expander import MAX_FILES_PER_ROUND
        assert MAX_FILES_PER_ROUND == 5


class TestContextExpanderExpand:
    async def test_expand_returns_ctx_in_context_expansion_phase(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": [],
            "reasoning": "no extra files needed",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.phase is OperationPhase.CONTEXT_EXPANSION

    async def test_expand_empty_files_returns_empty_tuple(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": [],
            "reasoning": "sufficient context",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_adds_existing_files(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        (tmp_path / "helpers.py").write_text("# helper\n")

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": ["helpers.py"],
            "reasoning": "need helper context",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert "helpers.py" in result.expanded_context_files

    async def test_expand_skips_nonexistent_files(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": ["ghost.py", "phantom.py"],
            "reasoning": "want these",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_truncates_to_max_files_per_round(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander, MAX_FILES_PER_ROUND

        for i in range(8):
            (tmp_path / f"file{i}.py").write_text(f"# file {i}\n")

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": [f"file{i}.py" for i in range(8)],
            "reasoning": "want all",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert len(result.expanded_context_files) <= MAX_FILES_PER_ROUND

    async def test_expand_stops_after_max_rounds(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander, MAX_ROUNDS

        for i in range(10):
            (tmp_path / f"round{i}.py").write_text(f"# round {i}\n")

        call_count = 0

        async def counting_plan(prompt: str, deadline: datetime) -> str:
            nonlocal call_count
            call_count += 1
            return json.dumps({
                "schema_version": "expansion.1",
                "additional_files_needed": [f"round{call_count}.py"],
                "reasoning": "always want more",
            })

        gen = MagicMock()
        gen.plan = counting_plan

        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        await expander.expand(ctx, deadline)
        assert call_count <= MAX_ROUNDS

    async def test_expand_invalid_json_does_not_raise(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator("this is not json at all")
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_wrong_schema_version_returns_empty(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "wrong.version",
            "additional_files_needed": ["something.py"],
            "reasoning": "test",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_generator_exception_does_not_raise(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = MagicMock()
        gen.plan = AsyncMock(side_effect=RuntimeError("provider_down"))

        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_deduplicates_files_across_rounds(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        (tmp_path / "shared.py").write_text("# shared\n")

        call_count = 0

        async def dup_plan(prompt: str, deadline: datetime) -> str:
            nonlocal call_count
            call_count += 1
            return json.dumps({
                "schema_version": "expansion.1",
                "additional_files_needed": ["shared.py"],
                "reasoning": "same file both rounds",
            })

        gen = MagicMock()
        gen.plan = dup_plan

        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files.count("shared.py") == 1


class TestContextExpanderOracleManifest:
    """Oracle manifest injection into planning prompt."""

    async def test_oracle_manifest_injected_when_ready(self, tmp_path):
        """When oracle is ready, planning prompt includes available files list."""
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from datetime import datetime, timezone, timedelta

        oracle = MagicMock()
        oracle.get_status = MagicMock(return_value={"running": True})
        oracle.get_relevant_files_for_query = AsyncMock(
            return_value=[tmp_path / "foo.py", tmp_path / "bar.py"]
        )

        captured_prompts = []
        mock_gen = MagicMock()
        async def fake_plan(prompt, deadline):
            captured_prompts.append(prompt)
            return '{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        mock_gen.plan = fake_plan

        expander = ContextExpander(generator=mock_gen, repo_root=tmp_path, oracle=oracle)
        ctx = OperationContext.create(
            target_files=("foo.py",), description="fix async timeout"
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        await expander.expand(ctx, deadline)

        assert len(captured_prompts) > 0
        assert "Available files" in captured_prompts[0]
        assert "foo.py" in captured_prompts[0] or "bar.py" in captured_prompts[0]

    async def test_oracle_fallback_when_not_ready(self, tmp_path):
        """When oracle.get_status() returns running=False, expand() runs without raising."""
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from datetime import datetime, timezone, timedelta

        oracle = MagicMock()
        oracle.get_status = MagicMock(return_value={"running": False})
        oracle.get_relevant_files_for_query = AsyncMock(return_value=[])

        mock_gen = MagicMock()
        mock_gen.plan = AsyncMock(
            return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        )

        expander = ContextExpander(generator=mock_gen, repo_root=tmp_path, oracle=oracle)
        ctx = OperationContext.create(
            target_files=("foo.py",), description="fix async timeout"
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        result = await expander.expand(ctx, deadline)
        # Must not raise; oracle.get_relevant_files_for_query must NOT have been called
        oracle.get_relevant_files_for_query.assert_not_called()

    async def test_oracle_fallback_when_none(self, tmp_path):
        """When oracle=None, expand() runs exactly as before."""
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from datetime import datetime, timezone, timedelta

        mock_gen = MagicMock()
        mock_gen.plan = AsyncMock(
            return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        )

        expander = ContextExpander(generator=mock_gen, repo_root=tmp_path)  # no oracle
        ctx = OperationContext.create(
            target_files=("foo.py",), description="fix async timeout"
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        result = await expander.expand(ctx, deadline)  # must not raise
