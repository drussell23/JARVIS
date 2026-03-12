"""Coordinator-path tests for PrimeProvider and ClaudeProvider (Task 7)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.op_context import GenerationResult, OperationContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(op_id: str = "op-coord-001") -> OperationContext:
    return OperationContext.create(
        target_files=("tests/test_utils.py",),
        description="Coordinator path test",
        op_id=op_id,
    )


def _patch_response(file_path: str = "tests/test_utils.py") -> str:
    """Return a minimal valid 2b.1 patch JSON string."""
    return json.dumps({
        "schema_version": "2b.1",
        "candidates": [
            {
                "candidate_id": "c1",
                "file_path": file_path,
                "full_content": "def test_stub():\n    assert True\n",
                "rationale": "coordinator path test",
            }
        ],
    })


class _StubCoordinator:
    """Minimal stub for ToolLoopCoordinator.run()."""

    def __init__(self, response: str, records: List[Any]) -> None:
        self._response = response
        self._records = records
        self.run_called = False
        self.last_kwargs: dict = {}

    async def run(
        self,
        prompt: str,
        generate_fn: Any,
        parse_fn: Any,
        repo: str,
        op_id: str,
        deadline: float,
    ) -> tuple:
        self.run_called = True
        self.last_kwargs = dict(repo=repo, op_id=op_id, deadline=deadline)
        # Call generate_fn once so the provider's _last_response / _last_msg cell
        # is populated — both PrimeProvider and ClaudeProvider depend on this.
        await generate_fn(prompt)
        return self._response, self._records


def _future_deadline() -> datetime:
    return datetime(2099, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# PrimeProvider coordinator path
# ---------------------------------------------------------------------------

class TestPrimeProviderCoordinatorPath:
    def _mock_prime_client(self, response_text: str) -> MagicMock:
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.model = "prime-7b"
        mock_resp.latency_ms = 50.0
        mock_resp.tokens_used = 80
        mock_resp.metadata = {}
        mock_resp.content = response_text

        async def _generate(**kwargs):
            return mock_resp

        client.generate = _generate
        return client

    async def test_coordinator_run_called(self, tmp_path: Path) -> None:
        """PrimeProvider.generate() delegates to coordinator.run() when tool_loop set."""
        from backend.core.ouroboros.governance.providers import PrimeProvider

        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=[])
        client = self._mock_prime_client(patch_json)

        provider = PrimeProvider(client, repo_root=tmp_path, tool_loop=stub)
        ctx = _make_ctx("op-prime-coord-001")
        result = await provider.generate(ctx, _future_deadline())

        assert stub.run_called, "coordinator.run() was not called"

    async def test_coordinator_tool_records_propagated(self, tmp_path: Path) -> None:
        """tool_execution_records on GenerationResult matches what coordinator returned."""
        from backend.core.ouroboros.governance.providers import PrimeProvider

        sentinel_record_1 = object()
        sentinel_record_2 = object()
        records = [sentinel_record_1, sentinel_record_2]
        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=records)
        client = self._mock_prime_client(patch_json)

        provider = PrimeProvider(client, repo_root=tmp_path, tool_loop=stub)
        ctx = _make_ctx("op-prime-coord-002")
        result = await provider.generate(ctx, _future_deadline())

        assert isinstance(result, GenerationResult)
        assert result.tool_execution_records == tuple(records)

    async def test_coordinator_receives_op_id(self, tmp_path: Path) -> None:
        """coordinator.run() receives the op_id from the OperationContext."""
        from backend.core.ouroboros.governance.providers import PrimeProvider

        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=[])
        client = self._mock_prime_client(patch_json)

        op_id = "op-prime-coord-verify-id"
        provider = PrimeProvider(client, repo_root=tmp_path, tool_loop=stub)
        ctx = _make_ctx(op_id)
        await provider.generate(ctx, _future_deadline())

        assert stub.last_kwargs.get("op_id") == op_id

    async def test_empty_records_gives_empty_tuple(self, tmp_path: Path) -> None:
        """When coordinator returns empty record list, tool_execution_records is ()."""
        from backend.core.ouroboros.governance.providers import PrimeProvider

        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=[])
        client = self._mock_prime_client(patch_json)

        provider = PrimeProvider(client, repo_root=tmp_path, tool_loop=stub)
        ctx = _make_ctx("op-prime-coord-empty")
        result = await provider.generate(ctx, _future_deadline())

        assert result.tool_execution_records == ()


# ---------------------------------------------------------------------------
# ClaudeProvider coordinator path
# ---------------------------------------------------------------------------

class TestClaudeProviderCoordinatorPath:
    def _mock_claude_client(self, response_text: str) -> MagicMock:
        async def _create(**kwargs):
            msg = MagicMock()
            msg.content = [MagicMock(text=response_text)]
            msg.usage = MagicMock(input_tokens=80, output_tokens=80)
            msg.model = "claude-sonnet-4-6"
            return msg

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = _create
        return client

    async def test_coordinator_run_called(self, tmp_path: Path) -> None:
        """ClaudeProvider.generate() delegates to coordinator.run() when tool_loop set."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=[])

        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tool_loop=stub)
        provider._client = self._mock_claude_client(patch_json)
        ctx = _make_ctx("op-claude-coord-001")
        result = await provider.generate(ctx, _future_deadline())

        assert stub.run_called, "coordinator.run() was not called"

    async def test_coordinator_tool_records_propagated(self, tmp_path: Path) -> None:
        """tool_execution_records on GenerationResult matches what coordinator returned."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        sentinel_a = object()
        sentinel_b = object()
        records = [sentinel_a, sentinel_b]
        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=records)

        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tool_loop=stub)
        provider._client = self._mock_claude_client(patch_json)
        ctx = _make_ctx("op-claude-coord-002")
        result = await provider.generate(ctx, _future_deadline())

        assert isinstance(result, GenerationResult)
        assert result.tool_execution_records == tuple(records)

    async def test_coordinator_receives_op_id(self, tmp_path: Path) -> None:
        """coordinator.run() receives the op_id from the OperationContext."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=[])

        op_id = "op-claude-coord-verify-id"
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tool_loop=stub)
        provider._client = self._mock_claude_client(patch_json)
        ctx = _make_ctx(op_id)
        await provider.generate(ctx, _future_deadline())

        assert stub.last_kwargs.get("op_id") == op_id

    async def test_empty_records_gives_empty_tuple(self, tmp_path: Path) -> None:
        """When coordinator returns empty record list, tool_execution_records is ()."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        patch_json = _patch_response()
        stub = _StubCoordinator(response=patch_json, records=[])

        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tool_loop=stub)
        provider._client = self._mock_claude_client(patch_json)
        ctx = _make_ctx("op-claude-coord-empty")
        result = await provider.generate(ctx, _future_deadline())

        assert result.tool_execution_records == ()
