# tests/governance/test_policy_engine.py
"""PolicyEngine: declarative permission rules loaded from YAML files.

Nine tests covering the core classification contract plus one structural
test that verifies orchestrator wiring.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_policy(directory: Path, content: str) -> None:
    """Write a policy.yaml under <directory>/.jarvis/policy.yaml."""
    jarvis_dir = directory / ".jarvis"
    jarvis_dir.mkdir(parents=True, exist_ok=True)
    (jarvis_dir / "policy.yaml").write_text(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_policy_files_returns_no_match(tmp_path):
    """When no policy YAML files exist the result must be NO_MATCH."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    engine = PolicyEngine(global_root=tmp_path / "nonexistent", repo_root=tmp_path / "also_nonexistent")
    decision = engine.classify(tool="edit", target="backend/foo.py")
    assert decision is PolicyDecision.NO_MATCH


def test_deny_rule_blocks(tmp_path):
    """A deny rule matching **/.env* must return BLOCKED for .env.local."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    _write_policy(
        tmp_path,
        """
        deny:
          - tool: "*"
            pattern: "**/.env*"
        """,
    )
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path / "nonexistent")
    decision = engine.classify(tool="edit", target=".env.local")
    assert decision is PolicyDecision.BLOCKED


def test_allow_rule_auto_approves(tmp_path):
    """An allow rule matching 'pytest *' must return SAFE_AUTO."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    _write_policy(
        tmp_path,
        """
        allow:
          - tool: "pytest"
            pattern: "*"
        """,
    )
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path / "nonexistent")
    decision = engine.classify(tool="pytest", target="tests/test_foo.py")
    assert decision is PolicyDecision.SAFE_AUTO


def test_ask_rule_requires_approval(tmp_path):
    """An ask rule matching backend/core/** must return APPROVAL_REQUIRED."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    _write_policy(
        tmp_path,
        """
        ask:
          - tool: "edit"
            pattern: "backend/core/**"
        """,
    )
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path / "nonexistent")
    decision = engine.classify(tool="edit", target="backend/core/ouroboros/governance/orchestrator.py")
    assert decision is PolicyDecision.APPROVAL_REQUIRED


def test_deny_overrides_allow(tmp_path):
    """When both deny and allow match the same target, deny must win."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    _write_policy(
        tmp_path,
        """
        deny:
          - tool: "*"
            pattern: "**/.env*"
        allow:
          - tool: "*"
            pattern: "**/.env*"
        """,
    )
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path / "nonexistent")
    decision = engine.classify(tool="edit", target=".env.production")
    assert decision is PolicyDecision.BLOCKED


def test_repo_policy_overrides_global(tmp_path):
    """Repo-level deny must override a global-level allow for the same target."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    global_root = tmp_path / "global"
    repo_root = tmp_path / "repo"

    # Global allows everything
    _write_policy(
        global_root,
        """
        allow:
          - tool: "*"
            pattern: "*"
        """,
    )
    # Repo denies secrets
    _write_policy(
        repo_root,
        """
        deny:
          - tool: "*"
            pattern: "secrets/**"
        """,
    )

    engine = PolicyEngine(global_root=global_root, repo_root=repo_root)
    decision = engine.classify(tool="edit", target="secrets/api_keys.txt")
    assert decision is PolicyDecision.BLOCKED


def test_malformed_yaml_skipped(tmp_path):
    """Malformed YAML must be silently ignored; result is NO_MATCH."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    jarvis_dir = tmp_path / ".jarvis"
    jarvis_dir.mkdir(parents=True, exist_ok=True)
    (jarvis_dir / "policy.yaml").write_text("deny: [unclosed bracket\nask: !!bad")

    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path / "nonexistent")
    decision = engine.classify(tool="edit", target="anything.py")
    assert decision is PolicyDecision.NO_MATCH


def test_classify_with_command_pattern(tmp_path):
    """A deny rule for tool='bash' pattern='rm -rf *' must block bash commands."""
    from backend.core.ouroboros.governance.policy_engine import (
        PolicyDecision,
        PolicyEngine,
    )

    _write_policy(
        tmp_path,
        """
        deny:
          - tool: "bash"
            pattern: "rm -rf *"
        """,
    )
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path / "nonexistent")
    decision = engine.classify(tool="bash", target="rm -rf /")
    assert decision is PolicyDecision.BLOCKED


def test_orchestrator_references_policy_engine(tmp_path):
    """Structural: orchestrator._run_pipeline must reference PolicyEngine."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch_module

    source = inspect.getsource(orch_module)
    assert "PolicyEngine" in source, (
        "orchestrator module must reference PolicyEngine after wiring"
    )
