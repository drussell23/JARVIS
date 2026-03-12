# tests/test_ouroboros_governance/test_governing_tool_policy.py
from __future__ import annotations
from pathlib import Path
import pytest
from backend.core.ouroboros.governance.tool_executor import (
    GoverningToolPolicy, PolicyContext, PolicyDecision, ToolCall,
)

def _ctx(repo_root, repo="jarvis", round_index=0, tool="read_file"):
    return PolicyContext(repo=repo, repo_root=repo_root,
        op_id="op-t", call_id=f"op-t:r{round_index}:{tool}", round_index=round_index)

def _policy(repo_root, repo="jarvis", **kwargs):
    return GoverningToolPolicy(repo_roots={repo: repo_root}, **kwargs)

class TestGoverningToolPolicy:
    def test_policy_deny_path_escape(self, tmp_path):
        policy = _policy(tmp_path)
        tc = ToolCall(name="read_file", arguments={"path": "../../etc/passwd"})
        result = policy.evaluate(tc, _ctx(tmp_path))
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.path_outside_repo"

    def test_policy_deny_unknown_tool(self, tmp_path):
        policy = _policy(tmp_path)
        tc = ToolCall(name="delete_file", arguments={"path": "src/foo.py"})
        result = policy.evaluate(tc, _ctx(tmp_path, tool="delete_file"))
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.unknown_tool"

    def test_policy_deny_run_tests_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_TOOL_RUN_TESTS_ALLOWED", raising=False)
        (tmp_path / "tests").mkdir()
        policy = _policy(tmp_path)
        tc = ToolCall(name="run_tests", arguments={"paths": ["tests/test_foo.py"]})
        result = policy.evaluate(tc, _ctx(tmp_path, tool="run_tests"))
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.run_tests_disabled"

    def test_policy_cross_repo_isolation(self, tmp_path):
        jarvis_root = tmp_path / "jarvis"
        reactor_root = tmp_path / "reactor"
        jarvis_root.mkdir(); reactor_root.mkdir()
        (jarvis_root / "src").mkdir()
        (jarvis_root / "src" / "foo.py").write_text("x = 1")
        policy = GoverningToolPolicy(
            repo_roots={"jarvis": jarvis_root, "reactor-core": reactor_root})
        # in-repo path from correct context -> ALLOW
        tc = ToolCall(name="read_file", arguments={"path": "src/foo.py"})
        jarvis_ctx = PolicyContext(repo="jarvis", repo_root=jarvis_root,
            op_id="op-x", call_id="op-x:r0:read_file", round_index=0)
        assert policy.evaluate(tc, jarvis_ctx).decision == PolicyDecision.ALLOW
        # absolute path into jarvis from reactor context -> DENY
        tc_escape = ToolCall(name="read_file",
            arguments={"path": str(jarvis_root / "src" / "foo.py")})
        reactor_ctx = PolicyContext(repo="reactor-core", repo_root=reactor_root,
            op_id="op-x", call_id="op-x:r1:read_file", round_index=1)
        assert policy.evaluate(tc_escape, reactor_ctx).decision == PolicyDecision.DENY
        assert "path_outside_repo" in policy.evaluate(tc_escape, reactor_ctx).reason_code

    def test_policy_allow_list_symbols_in_repo(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mod.py").write_text("pass\n")
        policy = _policy(tmp_path)
        tc = ToolCall(name="list_symbols", arguments={"module_path": "src/mod.py"})
        assert policy.evaluate(tc, _ctx(tmp_path, tool="list_symbols")).decision == PolicyDecision.ALLOW

    def test_policy_allow_run_tests_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_TOOL_RUN_TESTS_ALLOWED", "true")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("def test_ok(): assert True\n")
        policy = _policy(tmp_path)
        tc = ToolCall(name="run_tests", arguments={"paths": ["tests/test_foo.py"]})
        assert policy.evaluate(tc, _ctx(tmp_path, tool="run_tests")).decision == PolicyDecision.ALLOW

    def test_policy_deny_run_tests_path_outside_tests_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_TOOL_RUN_TESTS_ALLOWED", "true")
        (tmp_path / "tests").mkdir(); (tmp_path / "src").mkdir()
        policy = _policy(tmp_path)
        tc = ToolCall(name="run_tests", arguments={"paths": ["src/not_a_test.py"]})
        result = policy.evaluate(tc, _ctx(tmp_path, tool="run_tests"))
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.path_outside_test_scope"

    def test_policy_search_code_glob_with_dotdot_denied(self, tmp_path):
        policy = _policy(tmp_path)
        tc = ToolCall(name="search_code", arguments={"pattern": "foo", "file_glob": "../**/*.py"})
        result = policy.evaluate(tc, _ctx(tmp_path, tool="search_code"))
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.path_outside_repo"

    def test_policy_deny_read_file_missing_path(self, tmp_path):
        policy = _policy(tmp_path)
        tc = ToolCall(name="read_file", arguments={})
        result = policy.evaluate(tc, _ctx(tmp_path))
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.path_outside_repo"

    def test_policy_deny_list_symbols_missing_module_path(self, tmp_path):
        policy = _policy(tmp_path)
        tc = ToolCall(name="list_symbols", arguments={})
        result = policy.evaluate(tc, _ctx(tmp_path, tool="list_symbols"))
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.path_outside_repo"

    def test_policy_repo_root_for_unknown_repo_raises(self, tmp_path):
        policy = _policy(tmp_path, repo="jarvis")
        with pytest.raises(KeyError, match="unknown-repo"):
            policy.repo_root_for("unknown-repo")
