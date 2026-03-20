"""
Tests for AgentSynthesisLoader.

Stage 1: AST scan -- blocks dangerous builtins and dangerous imports.
Stage 2: Import allowlist -- loaded from sandbox_allowlist.yaml.
Stage 3: Contract gate -- requires AGENT_MANIFEST, side_effect_policy, compensation_strategy.
"""
import textwrap
import pytest
from backend.neural_mesh.synthesis.agent_synthesis_loader import (
    AgentSynthesisLoader,
    AstScanError,
    SandboxImportError,
    ContractGateError,
    CompensationStrategy,
    SideEffectPolicy,
)


# Stage 1: breakpoint() is a blocked dangerous builtin
CODE_CALLS_BREAKPOINT = textwrap.dedent("""
    def run():
        breakpoint()
""")

# Stage 1: verify the blocked set includes critical names
def test_stage1_blocked_set_includes_eval_and_exec():
    from backend.neural_mesh.synthesis.agent_synthesis_loader import _DANGEROUS_BUILTINS
    assert "eval" in _DANGEROUS_BUILTINS
    assert "exec" in _DANGEROUS_BUILTINS
    assert "__import__" in _DANGEROUS_BUILTINS


def test_stage1_blocks_breakpoint():
    loader = AgentSynthesisLoader()
    with pytest.raises(AstScanError, match="breakpoint"):
        loader.validate(CODE_CALLS_BREAKPOINT)


# Stage 2: struct is stdlib but NOT on the allowlist
CODE_IMPORTS_STRUCT = textwrap.dedent("""
    import struct
    async def execute(goal, context):
        return {}
""")


def test_stage2_blocks_unlisted_import():
    loader = AgentSynthesisLoader()
    with pytest.raises(SandboxImportError, match="struct"):
        loader.validate(CODE_IMPORTS_STRUCT)


# Stage 3: missing all contract constants
CODE_NO_CONTRACT = textwrap.dedent("""
    import asyncio

    async def execute(goal, context):
        return {"status": "ok"}
""")


def test_stage3_rejects_missing_contract():
    loader = AgentSynthesisLoader()
    with pytest.raises(ContractGateError):
        loader.validate(CODE_NO_CONTRACT)


# Valid: passes all three stages
CODE_VALID = textwrap.dedent("""
    import asyncio

    AGENT_MANIFEST = {
        "name": "test_agent",
        "version": "0.1.0",
        "capabilities": ["vision_action"],
    }
    side_effect_policy = {
        "writes_files": False,
        "calls_external_apis": False,
        "modifies_system_state": False,
        "read_only": True,
    }
    compensation_strategy = {
        "strategy_type": "noop",
        "snapshot_paths": [],
        "undo_endpoint": None,
        "manual_instructions": "",
    }

    async def execute(goal, context):
        return {"status": "ok", "goal": goal}
""")


def test_valid_code_passes_all_stages():
    AgentSynthesisLoader().validate(CODE_VALID)  # must not raise


def test_extract_manifest():
    manifest = AgentSynthesisLoader().extract_manifest(CODE_VALID)
    assert manifest["name"] == "test_agent"


def test_compensation_strategy_fields():
    cs = CompensationStrategy(
        strategy_type="rollback_file",
        snapshot_paths=("/tmp/snap",),
        undo_endpoint=None,
        manual_instructions="",
    )
    assert cs.strategy_type == "rollback_file"


def test_side_effect_policy_read_only_consistency():
    policy = SideEffectPolicy(
        writes_files=False,
        calls_external_apis=False,
        modifies_system_state=False,
        read_only=True,
    )
    assert policy.read_only is True
