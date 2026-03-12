from __future__ import annotations
import pytest


class TestGovernedLoopConfigToolUse:
    def test_env_toggle_disables_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_GOVERNED_TOOL_USE_ENABLED", raising=False)
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        assert GovernedLoopConfig.from_env().tool_use_enabled is False

    def test_env_toggle_enables(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GOVERNED_TOOL_USE_ENABLED", "true")
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        assert GovernedLoopConfig.from_env().tool_use_enabled is True

    def test_env_max_rounds(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GOVERNED_TOOL_MAX_ROUNDS", "7")
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        assert GovernedLoopConfig.from_env().max_tool_rounds == 7

    def test_env_defaults(self, monkeypatch):
        for k in ("JARVIS_GOVERNED_TOOL_USE_ENABLED", "JARVIS_GOVERNED_TOOL_MAX_ROUNDS",
                  "JARVIS_GOVERNED_TOOL_TIMEOUT_S", "JARVIS_GOVERNED_TOOL_MAX_CONCURRENT"):
            monkeypatch.delenv(k, raising=False)
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        cfg = GovernedLoopConfig.from_env()
        assert cfg.tool_use_enabled is False
        assert cfg.max_tool_rounds == 5
        assert cfg.tool_timeout_s == 30.0
        assert cfg.max_concurrent_tools == 2
