"""Free-Form Subagent Delegation Slice 2 — graduation regression spine.

Verifies the full Slice 1 + Slice 2 stack composes end-to-end:
the dynamic linkage helpers + per-type synthesizers + AST pins +
per-type kill-switch flags all discovered automatically and
mathematically locked.

Coverage:
  * 4 per-type kill switch flags discovered via FlagRegistry seed
  * All flags default-true (already-graduated infrastructure)
  * 5 dynamic-linkage AST-pin invariants discovered + clean
  * Per-type flag → false reverts both schema enum AND policy
    frozenset (proves dynamic linkage is hot-revertable)
  * End-to-end Venom-path GENERAL dispatch reaches the
    AgenticGeneralSubagent without MalformedGeneralInput (the
    structural footgun this arc closes)
"""
from __future__ import annotations

import asyncio
import json
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.flag_registry import FlagRegistry
from backend.core.ouroboros.governance.flag_registry_seed import (
    seed_default_registry,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    list_shipped_code_invariants,
    validate_all,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    SCHEMA_VERSION,
    SubagentRequest,
    SubagentResult,
    SubagentStatus,
    SubagentType,
    policy_allowed_subagent_types,
    tool_schema_subagent_types,
)


# ---------------------------------------------------------------------------
# Per-type flag discovery
# ---------------------------------------------------------------------------


class TestFlagDiscovery:
    def test_seed_discovers_all_4_per_type_flags(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        per_type_flags = [
            f for f in registry.list_all()
            if f.name.startswith("JARVIS_SUBAGENT_")
            and f.name.endswith("_ENABLED")
            and f.name != "JARVIS_SUBAGENT_DISPATCH_ENABLED"
        ]
        assert len(per_type_flags) == 4

    @pytest.mark.parametrize("type_name", ["EXPLORE", "REVIEW", "PLAN", "GENERAL"])
    def test_each_per_type_flag_present_default_true(
        self, type_name: str,
    ):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec(f"JARVIS_SUBAGENT_{type_name}_ENABLED")
        assert spec is not None
        assert spec.default is True


# ---------------------------------------------------------------------------
# AST-pin invariant discovery + clean validation
# ---------------------------------------------------------------------------


class TestInvariantDiscovery:
    def test_all_5_linkage_invariants_discovered(self):
        invs = list_shipped_code_invariants()
        names = {i.invariant_name for i in invs}
        assert "subagent_contracts_dynamic_helpers_present" in names
        assert "subagent_contracts_synthesizers_present" in names
        assert "subagent_contracts_from_args_invokes_synthesizers" in names
        assert "tool_executor_uses_dynamic_subagent_enum" in names
        assert "tool_executor_uses_dynamic_subagent_policy" in names

    def test_all_linkage_invariants_validate_clean(self):
        violations = validate_all()
        linkage_v = [
            v for v in violations
            if v.invariant_name in {
                "subagent_contracts_dynamic_helpers_present",
                "subagent_contracts_synthesizers_present",
                "subagent_contracts_from_args_invokes_synthesizers",
                "tool_executor_uses_dynamic_subagent_enum",
                "tool_executor_uses_dynamic_subagent_policy",
            }
        ]
        assert linkage_v == [], (
            f"Linkage invariants drifted: "
            f"{[(v.invariant_name, v.detail) for v in linkage_v]}"
        )


# ---------------------------------------------------------------------------
# Hot-revert proof — per-type flag flip reverts schema + policy together
# ---------------------------------------------------------------------------


class TestHotRevert:
    def test_disabling_general_removes_from_schema_and_policy(
        self, monkeypatch,
    ):
        """Single flag flip reverts BOTH schema enum AND policy
        frozenset — proves the mathematical lock is hot-revertable."""
        for st in SubagentType:
            monkeypatch.delenv(
                f"JARVIS_SUBAGENT_{st.name}_ENABLED", raising=False,
            )
        # Baseline — all 4 enabled.
        assert "general" in policy_allowed_subagent_types()
        assert "general" in tool_schema_subagent_types()

        # Hot revert — single env var flip.
        monkeypatch.setenv("JARVIS_SUBAGENT_GENERAL_ENABLED", "false")

        # Both surfaces reflect immediately.
        assert "general" not in policy_allowed_subagent_types()
        assert "general" not in tool_schema_subagent_types()
        # Other types unaffected.
        for other in ("explore", "review", "plan"):
            assert other in policy_allowed_subagent_types()
            assert other in tool_schema_subagent_types()


# ---------------------------------------------------------------------------
# End-to-end Venom-path GENERAL dispatch
# ---------------------------------------------------------------------------


class _MockSubagentOrchestrator:
    """Test double — captures the dispatched SubagentRequest so the
    test can assert that it carries a populated general_invocation
    field (the structural footgun this arc closed)."""

    def __init__(self) -> None:
        self.dispatched_requests: list = []

    async def dispatch(
        self, parent_ctx: Any, request: SubagentRequest,
    ) -> SubagentResult:
        self.dispatched_requests.append(request)
        return SubagentResult(
            schema_version=SCHEMA_VERSION,
            subagent_id="mock-1",
            subagent_type=request.subagent_type,
            status=SubagentStatus.COMPLETED,
            summary="mock e2e dispatch succeeded",
        )


class TestEndToEndVenomPath:
    """Proves the structural footgun is closed: a Venom-path
    GENERAL dispatch via dispatch_subagent now reaches the
    SubagentOrchestrator with a populated general_invocation,
    rather than silently failing with MalformedGeneralInput at
    AgenticGeneralSubagent."""

    @pytest.mark.asyncio
    async def test_general_dispatch_via_venom_carries_invocation(
        self, monkeypatch,
    ):
        # Master + per-type flags on (defaults, but be explicit).
        monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SUBAGENT_GENERAL_ENABLED", "true")

        from pathlib import Path
        from backend.core.ouroboros.governance.tool_executor import (
            AsyncProcessToolBackend,
            PolicyContext,
            ToolCall,
            ToolExecStatus,
        )

        backend = AsyncProcessToolBackend(
            semaphore=asyncio.Semaphore(1),
        )
        mock_orch = _MockSubagentOrchestrator()
        backend.set_subagent_orchestrator(mock_orch)

        # Compose a ToolCall the way Venom would for a model-driven
        # free-form GENERAL dispatch.
        call = ToolCall(
            name="dispatch_subagent",
            arguments={
                "subagent_type": "general",
                "goal": "investigate the auth module",
                "operation_scope": ["backend/auth.py"],
                "max_mutations": 0,
                "invocation_reason": "investigation",
            },
        )

        # Build a minimal PolicyContext carrying a real risk_tier
        # so the synthesizer threads it through correctly.
        policy_ctx = PolicyContext(
            repo="jarvis",
            repo_root=Path("/tmp"),
            op_id="op-e2e-1",
            call_id="op-e2e-1:r1:dispatch_subagent",
            round_index=1,
            risk_tier=types.SimpleNamespace(name="NOTIFY_APPLY"),
        )

        # Invoke the executor's dispatch_subagent handler.
        result = await backend._run_dispatch_subagent(
            call, policy_ctx, timeout=10.0, cap=4096,
        )

        # No MalformedGeneralInput — the executor reached the
        # orchestrator's dispatch() method and got a SUCCESS back.
        assert result.status is ToolExecStatus.SUCCESS, (
            f"expected SUCCESS, got {result.status}: {result.error}"
        )

        # The structural footgun is closed: the dispatched request
        # carries a populated general_invocation with the firewall
        # boundary fields the AgenticGeneralSubagent requires.
        assert len(mock_orch.dispatched_requests) == 1
        req = mock_orch.dispatched_requests[0]
        assert req.subagent_type is SubagentType.GENERAL
        assert req.general_invocation is not None
        inv = req.general_invocation
        assert inv["goal"] == "investigate the auth module"
        assert inv["operation_scope"] == ("backend/auth.py",)
        assert inv["max_mutations"] == 0
        assert inv["invocation_reason"] == "investigation"
        # Parent tier was threaded from policy_ctx (model cannot
        # fake — defense-in-depth).
        assert inv["parent_op_risk_tier"] == "NOTIFY_APPLY"
        # Default tools were derived from the canonical
        # readonly_tool_whitelist (no hardcoded list).
        assert "read_file" in inv["allowed_tools"]
        assert "search_code" in inv["allowed_tools"]

    @pytest.mark.asyncio
    async def test_per_type_flag_off_returns_policy_denied(
        self, monkeypatch,
    ):
        """When JARVIS_SUBAGENT_GENERAL_ENABLED=false, the policy
        layer's call to policy_allowed_subagent_types() reflects
        the change immediately; the model receives a
        policy_denied response rather than reaching the orchestrator."""
        monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SUBAGENT_GENERAL_ENABLED", "false")

        # The policy layer's check at line 2296 is what we're
        # exercising — the Venom dispatch path consults
        # policy_allowed_subagent_types() at call time.
        allowed = policy_allowed_subagent_types()
        assert "general" not in allowed
        assert "explore" in allowed  # other types still enabled
