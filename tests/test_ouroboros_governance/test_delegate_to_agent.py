"""Tests for the ``delegate_to_agent`` Venom tool — Phase 2 sub-agent delegation.

The ``delegate_to_agent`` tool lets the main tool loop spawn an isolated
read-only exploration sub-agent that runs with its own goal scope and
returns a structured findings report. These tests lock in:

  * Tool manifest registration
  * Policy rules (env-gated, goal validation, agent_type whitelist)
  * Handler dispatch to the injected ExplorationFleet
  * Structured JSON report shape
  * Failure modes: missing fleet, timeout, fleet exception
  * Defence-in-depth env-var re-check at execution time
  * Backend late-binding via ``set_exploration_fleet``

The sub-agent pipeline in the tool backend is purely in-process — the
tests use a mock ExplorationFleet so they never hit disk or subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.tool_executor import (
    AsyncProcessToolBackend,
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
    ToolExecStatus,
    _L1_MANIFESTS,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeFinding:
    category: str
    description: str
    file_path: str = ""
    evidence: str = ""
    relevance: float = 0.0


@dataclass
class _FakeFleetReport:
    """Minimal mock that matches the fields delegate_to_agent reads."""

    goal: str = "fake-goal"
    agents_deployed: int = 3
    agents_completed: int = 3
    agents_failed: int = 0
    total_files_explored: int = 12
    total_findings: int = 2
    findings: List[_FakeFinding] = field(default_factory=list)
    per_repo_summary: Dict[str, str] = field(default_factory=dict)
    duration_s: float = 1.5
    synthesis: str = "Fake synthesis text."


def _make_fleet(report: _FakeFleetReport) -> Any:
    """Build a mock ExplorationFleet whose ``deploy()`` returns ``report``."""
    fleet = MagicMock()
    fleet.deploy = AsyncMock(return_value=report)
    return fleet


def _make_backend(fleet: Any = None, n_sem: int = 2) -> AsyncProcessToolBackend:
    return AsyncProcessToolBackend(
        semaphore=asyncio.Semaphore(n_sem),
        exploration_fleet=fleet,
    )


def _policy_ctx(repo_root: Path, op_id: str = "op-delegate-001") -> PolicyContext:
    return PolicyContext(
        repo="jarvis",
        repo_root=repo_root,
        op_id=op_id,
        call_id=f"{op_id}:r0:delegate_to_agent",
        round_index=0,
    )


@pytest.fixture(autouse=True)
def _clean_delegate_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure each test starts with delegate_to_agent env vars at defaults."""
    monkeypatch.delenv("JARVIS_TOOL_DELEGATE_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_DELEGATE_TOP_FINDINGS", raising=False)
    yield


# ---------------------------------------------------------------------------
# Manifest registration
# ---------------------------------------------------------------------------


class TestManifest:
    def test_delegate_to_agent_registered(self) -> None:
        assert "delegate_to_agent" in _L1_MANIFESTS

    def test_manifest_fields(self) -> None:
        m = _L1_MANIFESTS["delegate_to_agent"]
        assert m.name == "delegate_to_agent"
        assert m.version == "1.0"
        assert "delegate" in m.capabilities
        assert "read" in m.capabilities
        # Sub-agents are read-only — no 'write' capability
        assert "write" not in m.capabilities

    def test_manifest_arg_schema(self) -> None:
        m = _L1_MANIFESTS["delegate_to_agent"]
        assert "subtask_description" in m.arg_schema
        assert "agent_type" in m.arg_schema
        assert "timeout_s" in m.arg_schema
        # Only 'explore' is supported in v1
        assert m.arg_schema["agent_type"]["enum"] == ["explore"]
        assert m.arg_schema["agent_type"]["default"] == "explore"


# ---------------------------------------------------------------------------
# Policy — GoverningToolPolicy
# ---------------------------------------------------------------------------


class TestPolicy:
    def _policy(self, tmp_path: Path) -> GoverningToolPolicy:
        return GoverningToolPolicy(repo_roots={"jarvis": tmp_path})

    def test_allow_by_default(self, tmp_path: Path) -> None:
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "understand auth middleware"},
        )
        result = self._policy(tmp_path).evaluate(call, _policy_ctx(tmp_path))
        assert result.decision is PolicyDecision.ALLOW

    def test_denied_when_env_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_TOOL_DELEGATE_AGENT_ENABLED", "false")
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "x"},
        )
        result = self._policy(tmp_path).evaluate(call, _policy_ctx(tmp_path))
        assert result.decision is PolicyDecision.DENY
        assert result.reason_code == "tool.denied.delegate_disabled"

    def test_denied_when_goal_empty(self, tmp_path: Path) -> None:
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "   "},
        )
        result = self._policy(tmp_path).evaluate(call, _policy_ctx(tmp_path))
        assert result.decision is PolicyDecision.DENY
        assert result.reason_code == "tool.denied.delegate_empty_goal"

    def test_denied_when_goal_missing(self, tmp_path: Path) -> None:
        call = ToolCall(name="delegate_to_agent", arguments={})
        result = self._policy(tmp_path).evaluate(call, _policy_ctx(tmp_path))
        assert result.decision is PolicyDecision.DENY
        assert result.reason_code == "tool.denied.delegate_empty_goal"

    def test_denied_bad_agent_type(self, tmp_path: Path) -> None:
        call = ToolCall(
            name="delegate_to_agent",
            arguments={
                "subtask_description": "x",
                "agent_type": "plan",  # not yet supported
            },
        )
        result = self._policy(tmp_path).evaluate(call, _policy_ctx(tmp_path))
        assert result.decision is PolicyDecision.DENY
        assert result.reason_code == "tool.denied.delegate_bad_type"

    def test_allow_agent_type_explore_explicit(self, tmp_path: Path) -> None:
        call = ToolCall(
            name="delegate_to_agent",
            arguments={
                "subtask_description": "map data flow",
                "agent_type": "explore",
            },
        )
        result = self._policy(tmp_path).evaluate(call, _policy_ctx(tmp_path))
        assert result.decision is PolicyDecision.ALLOW

    def test_allow_agent_type_case_insensitive(self, tmp_path: Path) -> None:
        call = ToolCall(
            name="delegate_to_agent",
            arguments={
                "subtask_description": "map data flow",
                "agent_type": "EXPLORE",
            },
        )
        result = self._policy(tmp_path).evaluate(call, _policy_ctx(tmp_path))
        assert result.decision is PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# Backend setter — late binding
# ---------------------------------------------------------------------------


class TestBackendSetter:
    def test_set_exploration_fleet_attaches(self) -> None:
        backend = _make_backend(fleet=None)
        assert backend._exploration_fleet is None
        fleet = _make_fleet(_FakeFleetReport())
        backend.set_exploration_fleet(fleet)
        assert backend._exploration_fleet is fleet

    def test_set_exploration_fleet_none_detaches(self) -> None:
        fleet = _make_fleet(_FakeFleetReport())
        backend = _make_backend(fleet=fleet)
        backend.set_exploration_fleet(None)
        assert backend._exploration_fleet is None

    def test_constructor_accepts_fleet(self) -> None:
        fleet = _make_fleet(_FakeFleetReport())
        backend = _make_backend(fleet=fleet)
        assert backend._exploration_fleet is fleet


# ---------------------------------------------------------------------------
# Handler — execute_async path
# ---------------------------------------------------------------------------


class TestHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_structured_report(self, tmp_path: Path) -> None:
        report = _FakeFleetReport(
            agents_deployed=4,
            agents_completed=4,
            agents_failed=0,
            total_files_explored=25,
            total_findings=2,
            findings=[
                _FakeFinding(
                    category="call_graph",
                    description="verify() calls hash_password",
                    file_path="backend/voice_unlock/core/verify.py",
                    evidence="return hash_password(pwd)",
                    relevance=0.9,
                ),
                _FakeFinding(
                    category="structure",
                    description="VerifyService class",
                    file_path="backend/voice_unlock/core/verify.py",
                    relevance=0.6,
                ),
            ],
            per_repo_summary={"jarvis": "backend/voice_unlock/: 2 findings"},
            duration_s=2.1,
            synthesis="Exploration identified the auth flow.",
        )
        backend = _make_backend(fleet=_make_fleet(report))

        call = ToolCall(
            name="delegate_to_agent",
            arguments={
                "subtask_description": "understand voice_unlock auth flow",
            },
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )

        assert result.status is ToolExecStatus.SUCCESS
        payload = json.loads(result.output)
        assert payload["agent_type"] == "explore"
        assert payload["subtask"] == "understand voice_unlock auth flow"
        assert payload["agents_deployed"] == 4
        assert payload["agents_completed"] == 4
        assert payload["total_files_explored"] == 25
        assert payload["total_findings"] == 2
        assert payload["duration_s"] == 2.1
        assert payload["synthesis"] == "Exploration identified the auth flow."
        assert payload["per_repo_summary"] == {
            "jarvis": "backend/voice_unlock/: 2 findings"
        }
        assert len(payload["top_findings"]) == 2
        assert payload["top_findings"][0]["category"] == "call_graph"
        assert payload["top_findings"][0]["description"] == "verify() calls hash_password"
        assert payload["top_findings"][0]["relevance"] == 0.9

    @pytest.mark.asyncio
    async def test_missing_fleet_returns_exec_error(self, tmp_path: Path) -> None:
        backend = _make_backend(fleet=None)
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "x"},
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.EXEC_ERROR
        assert result.error is not None
        assert "no ExplorationFleet" in result.error

    @pytest.mark.asyncio
    async def test_env_disabled_execution_time_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defence-in-depth: even if policy allowed (e.g. env flipped mid-op),
        the handler re-checks and returns POLICY_DENIED."""
        monkeypatch.setenv("JARVIS_TOOL_DELEGATE_AGENT_ENABLED", "false")
        backend = _make_backend(fleet=_make_fleet(_FakeFleetReport()))
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "x"},
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.POLICY_DENIED
        assert result.error is not None
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_empty_goal_returns_exec_error(self, tmp_path: Path) -> None:
        backend = _make_backend(fleet=_make_fleet(_FakeFleetReport()))
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": ""},
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.EXEC_ERROR
        assert result.error is not None
        assert "subtask_description" in result.error

    @pytest.mark.asyncio
    async def test_whitespace_only_goal_rejected(self, tmp_path: Path) -> None:
        backend = _make_backend(fleet=_make_fleet(_FakeFleetReport()))
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "   \n\t  "},
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.EXEC_ERROR

    @pytest.mark.asyncio
    async def test_unknown_agent_type_rejected(self, tmp_path: Path) -> None:
        backend = _make_backend(fleet=_make_fleet(_FakeFleetReport()))
        call = ToolCall(
            name="delegate_to_agent",
            arguments={
                "subtask_description": "x",
                "agent_type": "write",  # not supported
            },
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.EXEC_ERROR
        assert result.error is not None
        assert "unsupported" in result.error.lower()

    @pytest.mark.asyncio
    async def test_fleet_exception_returns_exec_error(self, tmp_path: Path) -> None:
        fleet = MagicMock()
        fleet.deploy = AsyncMock(side_effect=RuntimeError("fleet kaboom"))
        backend = _make_backend(fleet=fleet)
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "x"},
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.EXEC_ERROR
        assert result.error is not None
        assert "RuntimeError" in result.error
        assert "fleet kaboom" in result.error

    @pytest.mark.asyncio
    async def test_fleet_timeout_returns_timeout_status(
        self, tmp_path: Path
    ) -> None:
        async def _slow_deploy(*_args: Any, **_kwargs: Any) -> None:
            await asyncio.sleep(10.0)
            return _FakeFleetReport()  # type: ignore[return-value]

        fleet = MagicMock()
        fleet.deploy = _slow_deploy  # type: ignore[method-assign]
        backend = _make_backend(fleet=fleet)
        call = ToolCall(
            name="delegate_to_agent",
            arguments={
                "subtask_description": "x",
                "timeout_s": 0.05,
            },
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.TIMEOUT
        assert result.error is not None
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_timeout_is_floor_clamped(self, tmp_path: Path) -> None:
        """timeout_s below 5s should be clamped to the 5s floor."""
        fleet = _make_fleet(_FakeFleetReport())
        backend = _make_backend(fleet=fleet)
        call = ToolCall(
            name="delegate_to_agent",
            arguments={
                "subtask_description": "x",
                "timeout_s": 0.01,  # absurdly low
            },
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        # Should succeed — the mock returns immediately, so the clamped
        # 5s floor never matters for the happy path, but we verify that
        # the call did not fail with TIMEOUT from the 0.01s.
        assert result.status is ToolExecStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_empty_findings_is_success(self, tmp_path: Path) -> None:
        report = _FakeFleetReport(
            total_findings=0,
            findings=[],
            synthesis="No findings — area is empty or goal too narrow.",
        )
        backend = _make_backend(fleet=_make_fleet(report))
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "x"},
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.SUCCESS
        payload = json.loads(result.output)
        assert payload["total_findings"] == 0
        assert payload["top_findings"] == []
        assert payload["synthesis"].startswith("No findings")

    @pytest.mark.asyncio
    async def test_top_findings_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DELEGATE_TOP_FINDINGS", "3")
        findings = [
            _FakeFinding(
                category="pattern",
                description=f"hit #{i}",
                file_path=f"f{i}.py",
                relevance=float(i) / 10.0,
            )
            for i in range(10)
        ]
        report = _FakeFleetReport(total_findings=10, findings=findings)
        backend = _make_backend(fleet=_make_fleet(report))
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "x"},
        )
        result = await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        assert result.status is ToolExecStatus.SUCCESS
        payload = json.loads(result.output)
        # Cap applies to top_findings list, not to total_findings count
        assert len(payload["top_findings"]) == 3
        assert payload["total_findings"] == 10

    @pytest.mark.asyncio
    async def test_fleet_deploy_called_with_goal(self, tmp_path: Path) -> None:
        fleet = _make_fleet(_FakeFleetReport())
        backend = _make_backend(fleet=fleet)
        call = ToolCall(
            name="delegate_to_agent",
            arguments={"subtask_description": "find the bootstrap path"},
        )
        await backend.execute_async(
            call, _policy_ctx(tmp_path), deadline=time.monotonic() + 30.0
        )
        fleet.deploy.assert_awaited_once()
        _, kwargs = fleet.deploy.call_args
        assert kwargs.get("goal") == "find the bootstrap path"
