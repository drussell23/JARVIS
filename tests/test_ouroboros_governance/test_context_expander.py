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


def _make_ready_oracle() -> MagicMock:
    """Return a MagicMock oracle that reports is_ready()=True."""
    oracle = MagicMock()
    oracle.is_ready.return_value = True
    oracle.get_status.return_value = {"running": True}
    oracle.get_fused_neighborhood = AsyncMock(side_effect=Exception("no fused neighborhood"))
    oracle.get_file_neighborhood.side_effect = Exception("no structural neighborhood")
    return oracle


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
        expander = ContextExpander(generator=gen, repo_root=tmp_path, oracle=_make_ready_oracle())
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

        expander = ContextExpander(generator=gen, repo_root=tmp_path, oracle=_make_ready_oracle())
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files.count("shared.py") == 1


class TestContextExpanderOracleManifest:
    """Oracle manifest injection into planning prompt."""

    async def test_oracle_manifest_injected_when_ready(self, tmp_path):
        """When oracle is ready, planning prompt includes structural neighborhood section."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from backend.core.ouroboros.oracle import FileNeighborhood
        from datetime import datetime, timezone, timedelta

        neighborhood = FileNeighborhood(
            target_files=["jarvis:foo.py"],
            imports=["jarvis:bar.py"],
            importers=[],
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )
        oracle = MagicMock()
        oracle.get_status = MagicMock(return_value={"running": True})
        oracle.get_file_neighborhood = MagicMock(return_value=neighborhood)

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
        assert "Structural file neighborhood" in captured_prompts[0]
        assert "jarvis:bar.py" in captured_prompts[0]

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
        # Must not raise; oracle.get_file_neighborhood must NOT have been called when not running
        oracle.get_file_neighborhood.assert_not_called()

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


class TestContextExpanderNeighborhoodManifest:
    """Tests for the get_file_neighborhood integration in ContextExpander."""

    def _make_ctx(self, description="fix the service", target_files=("backend/core/service.py",)):
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
        ctx = OperationContext.create(
            op_id="test-op-1",
            description=description,
            target_files=tuple(target_files),
        )
        ctx = ctx.advance(OperationPhase.ROUTE).advance(OperationPhase.CONTEXT_EXPANSION)
        return ctx

    def _make_oracle(self, neighborhood=None):
        from backend.core.ouroboros.oracle import FileNeighborhood
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        default_nh = neighborhood or FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[],
            callers=["jarvis:backend/core/main.py"],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=["jarvis:tests/test_service.py"],
        )
        oracle.get_file_neighborhood.return_value = default_nh
        return oracle

    async def test_neighborhood_section_appears_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        oracle = self._make_oracle()
        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)

        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], oracle=oracle)

        # Neighborhood section must reference known file
        assert "jarvis:backend/core/base.py" in prompt

    async def test_neighborhood_section_truncates_at_10(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        # 15 callers — should be truncated to 10 with indicator
        callers = [f"jarvis:backend/core/caller_{i}.py" for i in range(15)]
        neighborhood = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=[],
            importers=[],
            callers=callers,
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )
        oracle = self._make_oracle(neighborhood=neighborhood)

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], oracle=oracle)

        # Exactly 10 callers shown, plus the "and N more" indicator
        shown = sum(1 for line in prompt.splitlines() if "caller_" in line and line.strip().startswith("- "))
        assert shown == 10
        assert "and 5 more" in prompt

    async def test_no_neighborhood_section_when_oracle_not_running(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        oracle = MagicMock()
        oracle.get_status.return_value = {"running": False}

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], oracle=oracle)

        # get_file_neighborhood should NOT be called when not running
        oracle.get_file_neighborhood.assert_not_called()
        # No neighborhood paths in prompt
        assert "jarvis:" not in prompt


class TestContextExpanderOracleGuard:
    """Oracle readiness guard at expand() entry — single location, no drift."""

    async def test_oracle_none_returns_ctx_unchanged(self, tmp_path):
        """When oracle is None, expand() returns ctx unchanged immediately."""
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from unittest.mock import AsyncMock, MagicMock

        gen = MagicMock()
        gen.plan = AsyncMock(return_value='{"schema_version":"expansion.1","additional_files_needed":[],"reasoning":"x"}')

        expander = ContextExpander(generator=gen, repo_root=tmp_path, oracle=None)
        ctx = _make_ctx()
        deadline = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result is ctx

    async def test_oracle_not_ready_returns_ctx_unchanged(self, tmp_path):
        """When oracle.is_ready() returns False, expand() returns ctx unchanged."""
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from unittest.mock import AsyncMock, MagicMock

        oracle = MagicMock()
        oracle.is_ready.return_value = False

        gen = MagicMock()
        gen.plan = AsyncMock(return_value='{"schema_version":"expansion.1","additional_files_needed":[],"reasoning":"x"}')

        expander = ContextExpander(generator=gen, repo_root=tmp_path, oracle=oracle)
        ctx = _make_ctx()
        deadline = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result is ctx

    async def test_oracle_not_ready_logs_info(self, tmp_path, caplog):
        """When oracle not ready, logs INFO with the blind baseline message."""
        import logging
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from unittest.mock import AsyncMock, MagicMock

        oracle = MagicMock()
        oracle.is_ready.return_value = False

        gen = MagicMock()
        gen.plan = AsyncMock(return_value='{"schema_version":"expansion.1","additional_files_needed":[],"reasoning":"x"}')

        expander = ContextExpander(generator=gen, repo_root=tmp_path, oracle=oracle)
        ctx = _make_ctx()
        deadline = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(seconds=30)

        with caplog.at_level(logging.INFO, logger="Ouroboros.ContextExpander"):
            await expander.expand(ctx, deadline)

        assert "[ContextExpander] Oracle not ready \u2014 using blind baseline" in caplog.text

    async def test_oracle_ready_proceeds_to_expand(self, tmp_path):
        """When oracle.is_ready() returns True, expand() proceeds normally (not short-circuited)."""
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from unittest.mock import AsyncMock, MagicMock

        oracle = MagicMock()
        oracle.is_ready.return_value = True
        oracle.get_fused_neighborhood = AsyncMock(side_effect=AttributeError("not present"))

        (tmp_path / "extra.py").write_text("# extra\n")

        gen = MagicMock()
        gen.plan = AsyncMock(return_value='{"schema_version":"expansion.1","additional_files_needed":["extra.py"],"reasoning":"need it"}')

        expander = ContextExpander(generator=gen, repo_root=tmp_path, oracle=oracle)
        ctx = _make_ctx()
        deadline = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        # generator.plan must have been called — proving we did not short-circuit
        gen.plan.assert_called()
