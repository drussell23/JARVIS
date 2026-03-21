# Ouroboros Tier 3 Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build six new Ouroboros subsystems: GAP 2 (PolicyEngine — declarative permission rules), GAP 1 (ToolCallHookRegistry — per-tool pre/post interception), GAP 7 (multi-level YAML config inheritance), GAP 5 (structured mid-operation elicitation), GAP 9 (subagent git worktree isolation), GAP 10 (OuroborosMCPServer — inbound MCP endpoint).

**Architecture:**
- GAP 2: `PolicyEngine` loads `~/.jarvis/policy.yaml` + `<repo>/.jarvis/policy.yaml`. Returns `PolicyDecision` (BLOCKED/APPROVAL_REQUIRED/SAFE_AUTO/NO_MATCH). Runs in orchestrator CLASSIFY phase BEFORE `RiskEngine`. Policy overrides risk tier when matched.
- GAP 1: `ToolCallHookRegistry` with `register_pre()`/`register_post()` and `run_pre()`/`run_post()`. Pre-hooks can veto via `HookDecision.BLOCK`. Wraps `ChangeEngine.apply()` file writes. Loads registrations from `OUROBOROS_HOOKS_CONFIG` YAML.
- GAP 7: `GovernedLoopConfig.from_env()` extended to load `~/.jarvis/governance.yaml` then `<repo>/.jarvis/governance.yaml` then `<repo>/.jarvis/governance.local.yaml` then env vars (env wins).
- GAP 5: `ApprovalProvider` protocol extended with `elicit()` method. `CLIApprovalProvider` implements it with asyncio.Event + timeout. Orchestrator APPROVE phase can elicit before final approve/reject.
- GAP 9: `WorktreeManager` with async `create(branch_name)` and `cleanup(worktree_path)`. Ready to wire into `SubagentScheduler._execute_unit_guarded()`.
- GAP 10: `OuroborosMCPServer` exposes `submit_intent`, `get_operation_status`, `approve_operation` tools. Forwards to `GovernedLoopService`. Standalone class; MCP transport wired externally.

**Tech Stack:** Python asyncio, PyYAML, fnmatch, pathlib, pytest

---

## File Structure

### New files
- `backend/core/ouroboros/governance/policy_engine.py` — PolicyEngine class (~120 lines)
- `backend/core/ouroboros/governance/tool_hook_registry.py` — ToolCallHookRegistry class (~130 lines)
- `backend/core/ouroboros/governance/config_loader.py` — multi-level YAML config loader (~80 lines)
- `backend/core/ouroboros/governance/worktree_manager.py` — WorktreeManager (~70 lines)
- `backend/core/ouroboros/governance/mcp_server.py` — OuroborosMCPServer (~100 lines)
- `tests/governance/test_policy_engine.py` — PolicyEngine unit tests
- `tests/governance/test_tool_hook_registry.py` — ToolCallHookRegistry unit tests
- `tests/governance/test_config_loader.py` — multi-level config tests
- `tests/governance/test_elicitation.py` — elicitation protocol tests
- `tests/governance/test_worktree_isolation.py` — worktree lifecycle tests
- `tests/governance/test_mcp_server.py` — MCP server unit tests

### Modified files
- `backend/core/ouroboros/governance/orchestrator.py:281-286` — insert PolicyEngine.classify() before RiskEngine BLOCKED check
- `backend/core/ouroboros/governance/change_engine.py:389-397` — wrap file write with ToolCallHookRegistry pre/post hooks
- `backend/core/ouroboros/governance/governed_loop_service.py:582-648` — extend GovernedLoopConfig.from_env() with YAML loading
- `backend/core/ouroboros/governance/approval_provider.py:130-150,243+` — add elicit() to protocol and CLIApprovalProvider

---

## Task 1: PolicyEngine — Declarative Permission Rules (GAP 2)

**Files:**
- Create: `backend/core/ouroboros/governance/policy_engine.py`
- Modify: `backend/core/ouroboros/governance/orchestrator.py:281-300`
- Test: `tests/governance/test_policy_engine.py`

- [ ] **Step 1: Write failing tests for PolicyEngine**

```python
# tests/governance/test_policy_engine.py
import pytest
import yaml
from pathlib import Path
from backend.core.ouroboros.governance.policy_engine import (
    PolicyEngine, PolicyDecision,
)


@pytest.fixture
def policy_dir(tmp_path):
    jarvis_dir = tmp_path / ".jarvis"
    jarvis_dir.mkdir()
    return jarvis_dir


def write_policy(policy_dir, rules):
    (policy_dir / "policy.yaml").write_text(yaml.dump({"permissions": rules}))


def test_no_policy_files_returns_no_match(tmp_path):
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path)
    decision = engine.classify(tool="edit", target="backend/foo.py")
    assert decision == PolicyDecision.NO_MATCH


def test_deny_rule_blocks(tmp_path, policy_dir):
    write_policy(policy_dir, {"deny": [{"tool": "edit", "pattern": "**/.env*"}]})
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path)
    decision = engine.classify(tool="edit", target=".env.local")
    assert decision == PolicyDecision.BLOCKED


def test_allow_rule_auto_approves(tmp_path, policy_dir):
    write_policy(policy_dir, {"allow": [{"tool": "bash", "pattern": "pytest *"}]})
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path)
    decision = engine.classify(tool="bash", target="pytest tests/")
    assert decision == PolicyDecision.SAFE_AUTO


def test_ask_rule_requires_approval(tmp_path, policy_dir):
    write_policy(policy_dir, {"ask": [{"tool": "edit", "pattern": "backend/core/**"}]})
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path)
    decision = engine.classify(tool="edit", target="backend/core/main.py")
    assert decision == PolicyDecision.APPROVAL_REQUIRED


def test_deny_overrides_allow(tmp_path, policy_dir):
    write_policy(policy_dir, {
        "deny": [{"tool": "edit", "pattern": "**/.env*"}],
        "allow": [{"tool": "edit", "pattern": "**"}],
    })
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path)
    decision = engine.classify(tool="edit", target=".env.production")
    assert decision == PolicyDecision.BLOCKED


def test_repo_policy_overrides_global(tmp_path):
    global_dir = tmp_path / "global" / ".jarvis"
    global_dir.mkdir(parents=True)
    (global_dir / "policy.yaml").write_text(
        yaml.dump({"permissions": {"allow": [{"tool": "edit", "pattern": "**"}]}})
    )
    repo_dir = tmp_path / "repo" / ".jarvis"
    repo_dir.mkdir(parents=True)
    (repo_dir / "policy.yaml").write_text(
        yaml.dump({"permissions": {"deny": [{"tool": "edit", "pattern": "migrations/**"}]}})
    )
    engine = PolicyEngine(global_root=tmp_path / "global", repo_root=tmp_path / "repo")
    decision = engine.classify(tool="edit", target="migrations/0001.py")
    assert decision == PolicyDecision.BLOCKED


def test_malformed_yaml_skipped(tmp_path, policy_dir):
    (policy_dir / "policy.yaml").write_text("{{not: valid")
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path)
    assert engine.classify(tool="edit", target="foo.py") == PolicyDecision.NO_MATCH


def test_classify_with_command_pattern(tmp_path, policy_dir):
    write_policy(policy_dir, {"deny": [{"tool": "bash", "pattern": "rm -rf *"}]})
    engine = PolicyEngine(global_root=tmp_path, repo_root=tmp_path)
    decision = engine.classify(tool="bash", target="rm -rf /")
    assert decision == PolicyDecision.BLOCKED
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m pytest tests/governance/test_policy_engine.py -v`
Expected: ImportError -- `policy_engine` not found

- [ ] **Step 3: Create `policy_engine.py`**

```python
# backend/core/ouroboros/governance/policy_engine.py
"""PolicyEngine -- declarative permission rules from YAML config.

GAP 2: Loads ~/.jarvis/policy.yaml (global) and <repo>/.jarvis/policy.yaml (project).
Evaluates deny/ask/allow rules against tool+target pairs.
Deny rules are checked first and are unconditional.
Repo-level policies override global-level for the same pattern.

YAML schema:
    permissions:
      deny:
        - tool: edit
          pattern: "**/.env*"
      ask:
        - tool: edit
          pattern: "backend/core/**"
      allow:
        - tool: bash
          pattern: "pytest *"
"""
from __future__ import annotations

import enum
import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)


class PolicyDecision(enum.Enum):
    BLOCKED = "BLOCKED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    SAFE_AUTO = "SAFE_AUTO"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class _Rule:
    tool: str
    pattern: str
    tier: PolicyDecision


class PolicyEngine:
    """Evaluates declarative permission rules against tool calls.

    Parameters
    ----------
    global_root:
        Path to user home config dir (reads <global_root>/.jarvis/policy.yaml).
    repo_root:
        Path to repo root (reads <repo_root>/.jarvis/policy.yaml).
    """

    def __init__(self, global_root: Path, repo_root: Path) -> None:
        global_rules = self._load_policy(Path(global_root) / ".jarvis" / "policy.yaml")
        repo_rules = self._load_policy(Path(repo_root) / ".jarvis" / "policy.yaml")
        # Repo rules override global: repo deny > global allow
        self._rules: Tuple[_Rule, ...] = tuple(global_rules + repo_rules)

    def classify(self, tool: str, target: str) -> PolicyDecision:
        """Evaluate rules against a tool+target pair.

        Priority: deny > ask > allow > NO_MATCH.
        Later rules (repo-level) override earlier rules (global-level).
        """
        best: PolicyDecision = PolicyDecision.NO_MATCH
        for rule in self._rules:
            if rule.tool != tool and rule.tool != "*":
                continue
            if not self._matches(target, rule.pattern):
                continue
            # Deny always wins immediately
            if rule.tier == PolicyDecision.BLOCKED:
                return PolicyDecision.BLOCKED
            # Otherwise track most restrictive match
            if rule.tier == PolicyDecision.APPROVAL_REQUIRED:
                best = PolicyDecision.APPROVAL_REQUIRED
            elif rule.tier == PolicyDecision.SAFE_AUTO and best == PolicyDecision.NO_MATCH:
                best = PolicyDecision.SAFE_AUTO
        return best

    @staticmethod
    def _matches(target: str, pattern: str) -> bool:
        t = target.replace("\\", "/")
        return fnmatch.fnmatch(t, pattern) or fnmatch.fnmatch(t.split("/")[-1], pattern)

    @staticmethod
    def _load_policy(path: Path) -> List[_Rule]:
        if not path.is_file():
            return []
        try:
            import yaml
        except ImportError:
            logger.warning("[PolicyEngine] PyYAML not installed -- policy disabled")
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return []
            perms = data.get("permissions", {})
            if not isinstance(perms, dict):
                return []
            rules: List[_Rule] = []
            for entry in perms.get("deny", []):
                if isinstance(entry, dict) and "tool" in entry and "pattern" in entry:
                    rules.append(_Rule(tool=entry["tool"], pattern=entry["pattern"], tier=PolicyDecision.BLOCKED))
            for entry in perms.get("ask", []):
                if isinstance(entry, dict) and "tool" in entry and "pattern" in entry:
                    rules.append(_Rule(tool=entry["tool"], pattern=entry["pattern"], tier=PolicyDecision.APPROVAL_REQUIRED))
            for entry in perms.get("allow", []):
                if isinstance(entry, dict) and "tool" in entry and "pattern" in entry:
                    rules.append(_Rule(tool=entry["tool"], pattern=entry["pattern"], tier=PolicyDecision.SAFE_AUTO))
            return rules
        except Exception as exc:
            logger.warning("[PolicyEngine] Failed to load %s: %s", path, exc)
            return []
```

- [ ] **Step 4: Run PolicyEngine tests to confirm pass**

Run: `python3 -m pytest tests/governance/test_policy_engine.py -v`
Expected: 9 passed

- [ ] **Step 5: Write failing structural test for orchestrator wiring**

Add to end of `tests/governance/test_policy_engine.py`:

```python
def test_orchestrator_references_policy_engine():
    """orchestrator._run_pipeline must reference policy_engine."""
    import inspect
    from backend.core.ouroboros.governance.orchestrator import Orchestrator
    source = inspect.getsource(Orchestrator._run_pipeline)
    assert "policy_engine" in source.lower() or "PolicyEngine" in source
```

- [ ] **Step 6: Wire PolicyEngine into orchestrator CLASSIFY phase**

In `orchestrator.py`, find the CLASSIFY phase (line ~281). After `classification = self._stack.risk_engine.classify(profile)` and before the `if risk_tier is RiskTier.BLOCKED:` check, add:

```python
        # GAP 2: declarative policy override -- runs before risk tier is acted on
        _policy_decision = PolicyDecision.NO_MATCH
        if hasattr(self._stack, "policy_engine") and self._stack.policy_engine is not None:
            try:
                for _tf in ctx.target_files:
                    _pd = self._stack.policy_engine.classify(tool="edit", target=_tf)
                    if _pd == PolicyDecision.BLOCKED:
                        _policy_decision = PolicyDecision.BLOCKED
                        break
                    if _pd == PolicyDecision.APPROVAL_REQUIRED:
                        _policy_decision = PolicyDecision.APPROVAL_REQUIRED
                    elif _pd == PolicyDecision.SAFE_AUTO and _policy_decision == PolicyDecision.NO_MATCH:
                        _policy_decision = PolicyDecision.SAFE_AUTO
            except Exception as _exc:
                logger.debug("[Orchestrator] PolicyEngine.classify failed: %s", _exc)

        # Policy BLOCKED overrides any risk tier
        if _policy_decision == PolicyDecision.BLOCKED:
            risk_tier = RiskTier.BLOCKED
```

Add import at top of orchestrator.py:
```python
from backend.core.ouroboros.governance.policy_engine import PolicyEngine, PolicyDecision
```

- [ ] **Step 7: Run structural test and full governance suite**

Run: `python3 -m pytest tests/governance/test_policy_engine.py -v`
Expected: 10 passed

Run: `python3 -m pytest tests/governance/ --tb=short 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/policy_engine.py \
        backend/core/ouroboros/governance/orchestrator.py \
        tests/governance/test_policy_engine.py
git commit -m "feat(gap2): PolicyEngine reads declarative permission rules from YAML

- PolicyEngine: loads ~/.jarvis/policy.yaml + <repo>/.jarvis/policy.yaml
- deny/ask/allow rules matched by tool+target fnmatch patterns
- Deny rules always win; repo-level overrides global-level
- Wired into orchestrator CLASSIFY phase before RiskEngine BLOCKED check
- Tests: 10 passing"
```

---

## Task 2: ToolCallHookRegistry -- Per-Tool Interception (GAP 1)

**Files:**
- Create: `backend/core/ouroboros/governance/tool_hook_registry.py`
- Modify: `backend/core/ouroboros/governance/change_engine.py:389-397`
- Test: `tests/governance/test_tool_hook_registry.py`

- [ ] **Step 1: Write failing tests for ToolCallHookRegistry**

```python
# tests/governance/test_tool_hook_registry.py
import asyncio
import pytest
from unittest.mock import AsyncMock
from backend.core.ouroboros.governance.tool_hook_registry import (
    ToolCallHookRegistry, HookDecision,
)


def test_empty_registry_allows_all():
    registry = ToolCallHookRegistry()
    loop = asyncio.new_event_loop()
    decision = loop.run_until_complete(
        registry.run_pre(tool_name="edit", tool_input={"file": "foo.py", "content": "x"})
    )
    loop.close()
    assert decision == HookDecision.ALLOW


@pytest.mark.asyncio
async def test_register_pre_hook_called():
    registry = ToolCallHookRegistry()
    handler = AsyncMock(return_value=HookDecision.ALLOW)
    registry.register_pre("edit", "*", handler)
    decision = await registry.run_pre("edit", {"file": "foo.py"})
    handler.assert_called_once()
    assert decision == HookDecision.ALLOW


@pytest.mark.asyncio
async def test_pre_hook_block_stops_execution():
    registry = ToolCallHookRegistry()
    blocker = AsyncMock(return_value=HookDecision.BLOCK)
    second = AsyncMock(return_value=HookDecision.ALLOW)
    registry.register_pre("edit", "*", blocker)
    registry.register_pre("edit", "*", second)
    decision = await registry.run_pre("edit", {"file": "foo.py"})
    assert decision == HookDecision.BLOCK
    second.assert_not_called()


@pytest.mark.asyncio
async def test_post_hook_called_with_result():
    registry = ToolCallHookRegistry()
    handler = AsyncMock()
    registry.register_post("edit", "*", handler)
    await registry.run_post("edit", {"file": "foo.py"}, result={"success": True})
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_pattern_filtering():
    registry = ToolCallHookRegistry()
    handler = AsyncMock(return_value=HookDecision.BLOCK)
    registry.register_pre("edit", "**/.env*", handler)
    # Should NOT match non-.env files
    decision = await registry.run_pre("edit", {"file": "backend/foo.py"})
    assert decision == HookDecision.ALLOW
    handler.assert_not_called()
    # Should match .env files
    decision = await registry.run_pre("edit", {"file": ".env.local"})
    assert decision == HookDecision.BLOCK


@pytest.mark.asyncio
async def test_hook_exception_is_swallowed():
    registry = ToolCallHookRegistry()
    broken = AsyncMock(side_effect=RuntimeError("boom"))
    registry.register_pre("edit", "*", broken)
    decision = await registry.run_pre("edit", {"file": "foo.py"})
    assert decision == HookDecision.ALLOW  # fail-open on hook error


@pytest.mark.asyncio
async def test_load_from_yaml(tmp_path):
    config = tmp_path / "hooks.yaml"
    import yaml
    config.write_text(yaml.dump({
        "hooks": {
            "pre": [
                {"tool": "edit", "pattern": "**/.env*", "action": "block"},
            ],
            "post": [
                {"tool": "edit", "pattern": "**", "action": "log"},
            ],
        }
    }))
    registry = ToolCallHookRegistry.from_yaml(config)
    decision = await registry.run_pre("edit", {"file": ".env"})
    assert decision == HookDecision.BLOCK
    decision = await registry.run_pre("edit", {"file": "foo.py"})
    assert decision == HookDecision.ALLOW
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m pytest tests/governance/test_tool_hook_registry.py -v`
Expected: ImportError

- [ ] **Step 3: Create `tool_hook_registry.py`**

```python
# backend/core/ouroboros/governance/tool_hook_registry.py
"""ToolCallHookRegistry -- per-tool pre/post interception hooks.

GAP 1: Provides the missing link between operation-level governance and
individual tool-call-level visibility. Pre-hooks can BLOCK a tool call
before it executes. Post-hooks run after for logging/auditing.

Hook registrations can be loaded from YAML config (OUROBOROS_HOOKS_CONFIG).
"""
from __future__ import annotations

import enum
import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List

logger = logging.getLogger(__name__)


class HookDecision(enum.Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


@dataclass
class _HookEntry:
    tool: str
    pattern: str
    handler: Callable[..., Coroutine[Any, Any, Any]]


class ToolCallHookRegistry:
    """Registry for per-tool pre/post hooks.

    Pre-hooks: called before a tool executes. Return HookDecision.BLOCK to veto.
    Post-hooks: called after a tool executes. Return value ignored.

    Hooks are matched by tool name and fnmatch pattern against the file/target.
    """

    def __init__(self) -> None:
        self._pre_hooks: List[_HookEntry] = []
        self._post_hooks: List[_HookEntry] = []

    def register_pre(
        self,
        tool_name: str,
        pattern: str,
        handler: Callable[..., Coroutine[Any, Any, HookDecision]],
    ) -> None:
        self._pre_hooks.append(_HookEntry(tool=tool_name, pattern=pattern, handler=handler))

    def register_post(
        self,
        tool_name: str,
        pattern: str,
        handler: Callable[..., Coroutine[Any, Any, None]],
    ) -> None:
        self._post_hooks.append(_HookEntry(tool=tool_name, pattern=pattern, handler=handler))

    async def run_pre(self, tool_name: str, tool_input: Dict[str, Any]) -> HookDecision:
        """Run all matching pre-hooks. Returns BLOCK if any hook blocks."""
        target = self._extract_target(tool_input)
        for hook in self._pre_hooks:
            if hook.tool != tool_name and hook.tool != "*":
                continue
            if not self._matches(target, hook.pattern):
                continue
            try:
                decision = await hook.handler(tool_name, tool_input)
                if decision == HookDecision.BLOCK:
                    logger.info("[ToolHook] PRE hook blocked %s on %s", tool_name, target)
                    return HookDecision.BLOCK
            except Exception as exc:
                logger.warning("[ToolHook] PRE hook failed for %s: %s", tool_name, exc)
        return HookDecision.ALLOW

    async def run_post(
        self, tool_name: str, tool_input: Dict[str, Any], result: Any = None
    ) -> None:
        """Run all matching post-hooks (fire-and-forget, errors swallowed)."""
        target = self._extract_target(tool_input)
        for hook in self._post_hooks:
            if hook.tool != tool_name and hook.tool != "*":
                continue
            if not self._matches(target, hook.pattern):
                continue
            try:
                await hook.handler(tool_name, tool_input, result)
            except Exception as exc:
                logger.warning("[ToolHook] POST hook failed for %s: %s", tool_name, exc)

    @classmethod
    def from_yaml(cls, config_path: Path) -> ToolCallHookRegistry:
        """Load hook registrations from YAML config."""
        registry = cls()
        try:
            import yaml
        except ImportError:
            logger.warning("[ToolHook] PyYAML not installed -- hooks disabled")
            return registry
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return registry
            hooks = data.get("hooks", {})
            for entry in hooks.get("pre", []):
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action", "block")
                decision = HookDecision.BLOCK if action == "block" else HookDecision.ALLOW

                async def _static_pre(tn: str, ti: dict, _d: HookDecision = decision) -> HookDecision:
                    return _d

                registry.register_pre(entry["tool"], entry["pattern"], _static_pre)
            for entry in hooks.get("post", []):
                if not isinstance(entry, dict):
                    continue

                async def _static_post(tn: str, ti: dict, res: Any = None) -> None:
                    logger.debug("[ToolHook] POST %s: %s", tn, ti)

                registry.register_post(entry["tool"], entry["pattern"], _static_post)
            logger.info("[ToolHook] Loaded %d pre + %d post hooks from %s",
                        len(registry._pre_hooks), len(registry._post_hooks), config_path)
        except Exception as exc:
            logger.warning("[ToolHook] Failed to load config %s: %s", config_path, exc)
        return registry

    @staticmethod
    def _extract_target(tool_input: Dict[str, Any]) -> str:
        return str(tool_input.get("file", tool_input.get("command", tool_input.get("path", ""))))

    @staticmethod
    def _matches(target: str, pattern: str) -> bool:
        t = target.replace("\\", "/")
        return fnmatch.fnmatch(t, pattern) or fnmatch.fnmatch(t.split("/")[-1], pattern)
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python3 -m pytest tests/governance/test_tool_hook_registry.py -v`
Expected: 7 passed

- [ ] **Step 5: Write structural test for ChangeEngine wiring**

Add to end of `tests/governance/test_tool_hook_registry.py`:

```python
def test_change_engine_references_hook_registry():
    """ChangeEngine.execute must reference tool_hook_registry or hook_registry."""
    import inspect
    from backend.core.ouroboros.governance.change_engine import ChangeEngine
    source = inspect.getsource(ChangeEngine.execute)
    assert "hook_registry" in source.lower() or "tool_hook" in source.lower()
```

- [ ] **Step 6: Wire ToolCallHookRegistry into ChangeEngine.apply()**

Read `backend/core/ouroboros/governance/change_engine.py`. Find the `execute()` method and the file write at line ~395.

Add `tool_hook_registry: Any = None` to `ChangeEngine.__init__()` parameters, stored as `self._tool_hook_registry`.

Before `target.write_text(...)` (inside the lock context), add:
```python
                # GAP 1: pre-hook check before file write
                if self._tool_hook_registry is not None:
                    _hook_input = {"file": str(target), "content": request.proposed_content[:200]}
                    _hook_decision = await self._tool_hook_registry.run_pre("edit", _hook_input)
                    if _hook_decision.name == "BLOCK":
                        raise RuntimeError("Tool hook blocked edit to %s" % target)
```

After the write (still inside the lock), add:
```python
                # GAP 1: post-hook notification after file write
                if self._tool_hook_registry is not None:
                    try:
                        await self._tool_hook_registry.run_post(
                            "edit", {"file": str(target)}, result={"success": True}
                        )
                    except Exception:
                        pass  # post-hooks are fire-and-forget
```

- [ ] **Step 7: Run all tests**

Run: `python3 -m pytest tests/governance/test_tool_hook_registry.py -v`
Expected: 8 passed

Run: `python3 -m pytest tests/governance/ --tb=short 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/tool_hook_registry.py \
        backend/core/ouroboros/governance/change_engine.py \
        tests/governance/test_tool_hook_registry.py
git commit -m "feat(gap1): ToolCallHookRegistry provides per-tool pre/post interception

- ToolCallHookRegistry: register_pre/post + run_pre/post with fnmatch patterns
- Pre-hooks can BLOCK tool calls; post-hooks are fire-and-forget
- from_yaml() loads static hook config from OUROBOROS_HOOKS_CONFIG
- ChangeEngine: wraps file writes with pre/post hook calls
- Hook errors fail-open (ALLOW) -- never crash the pipeline
- Tests: 8 passing"
```

---

## Task 3: Multi-Level Config Inheritance (GAP 7)

**Files:**
- Create: `backend/core/ouroboros/governance/config_loader.py`
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py:582-648`
- Test: `tests/governance/test_config_loader.py`

- [ ] **Step 1: Write failing tests for config loader**

```python
# tests/governance/test_config_loader.py
import pytest
import yaml
from pathlib import Path
from backend.core.ouroboros.governance.config_loader import load_layered_config


def test_empty_dirs_returns_empty_dict(tmp_path):
    result = load_layered_config(global_root=tmp_path, repo_root=tmp_path)
    assert result == {}


def test_global_config_loaded(tmp_path):
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    (jarvis / "governance.yaml").write_text(yaml.dump({"approval_timeout_s": 999}))
    result = load_layered_config(global_root=tmp_path, repo_root=tmp_path / "nonexistent")
    assert result["approval_timeout_s"] == 999


def test_repo_overrides_global(tmp_path):
    g = tmp_path / "global" / ".jarvis"
    g.mkdir(parents=True)
    (g / "governance.yaml").write_text(yaml.dump({"approval_timeout_s": 100, "max_concurrent_ops": 5}))
    r = tmp_path / "repo" / ".jarvis"
    r.mkdir(parents=True)
    (r / "governance.yaml").write_text(yaml.dump({"approval_timeout_s": 200}))
    result = load_layered_config(global_root=tmp_path / "global", repo_root=tmp_path / "repo")
    assert result["approval_timeout_s"] == 200
    assert result["max_concurrent_ops"] == 5


def test_local_overrides_repo(tmp_path):
    r = tmp_path / ".jarvis"
    r.mkdir()
    (r / "governance.yaml").write_text(yaml.dump({"approval_timeout_s": 100}))
    (r / "governance.local.yaml").write_text(yaml.dump({"approval_timeout_s": 300}))
    result = load_layered_config(global_root=tmp_path / "nonexistent", repo_root=tmp_path)
    assert result["approval_timeout_s"] == 300


def test_malformed_yaml_skipped(tmp_path):
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    (jarvis / "governance.yaml").write_text("{{bad yaml")
    result = load_layered_config(global_root=tmp_path, repo_root=tmp_path)
    assert result == {}


def test_non_dict_yaml_skipped(tmp_path):
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    (jarvis / "governance.yaml").write_text("- list\n- item")
    result = load_layered_config(global_root=tmp_path, repo_root=tmp_path)
    assert result == {}
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m pytest tests/governance/test_config_loader.py -v`
Expected: ImportError

- [ ] **Step 3: Create `config_loader.py`**

```python
# backend/core/ouroboros/governance/config_loader.py
"""Multi-level YAML config loader for GovernedLoopConfig.

GAP 7: Loads governance config from three levels:
1. ~/.jarvis/governance.yaml -- global defaults
2. <repo>/.jarvis/governance.yaml -- project overrides (committed)
3. <repo>/.jarvis/governance.local.yaml -- personal overrides (gitignored)

Later levels override earlier. Env vars override all file-based config
(handled by GovernedLoopConfig.from_env() after this loader runs).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def load_layered_config(
    global_root: Path,
    repo_root: Path,
) -> Dict[str, Any]:
    """Load and merge governance config from up to 3 YAML files.

    Returns a flat dict of config key-value pairs. Empty dict if no files found.
    """
    layers = [
        Path(global_root) / ".jarvis" / "governance.yaml",
        Path(repo_root) / ".jarvis" / "governance.yaml",
        Path(repo_root) / ".jarvis" / "governance.local.yaml",
    ]
    merged: Dict[str, Any] = {}
    for path in layers:
        loaded = _load_yaml_dict(path)
        if loaded:
            merged.update(loaded)
    return merged


def _load_yaml_dict(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        import yaml
    except ImportError:
        logger.warning("[ConfigLoader] PyYAML not installed -- skipping %s", path)
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.debug("[ConfigLoader] Skipping non-dict YAML: %s", path)
            return None
        logger.debug("[ConfigLoader] Loaded %d keys from %s", len(data), path)
        return data
    except Exception as exc:
        logger.warning("[ConfigLoader] Failed to load %s: %s", path, exc)
        return None
```

- [ ] **Step 4: Run config loader tests**

Run: `python3 -m pytest tests/governance/test_config_loader.py -v`
Expected: 6 passed

- [ ] **Step 5: Write structural test for GovernedLoopConfig integration**

Add to `tests/governance/test_config_loader.py`:

```python
def test_governed_loop_config_from_env_references_config_loader():
    """from_env() must call load_layered_config or config_loader."""
    import inspect
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
    source = inspect.getsource(GovernedLoopConfig.from_env)
    assert "load_layered_config" in source or "config_loader" in source
```

- [ ] **Step 6: Integrate config_loader into GovernedLoopConfig.from_env()**

In `governed_loop_service.py`, find `from_env()` (line ~587). After the `resolved_root` line and before `return cls(...)`, add:

```python
        # GAP 7: multi-level YAML config inheritance
        from backend.core.ouroboros.governance.config_loader import load_layered_config
        _yaml_cfg = load_layered_config(
            global_root=Path.home(),
            repo_root=resolved_root,
        )

        def _cfg(key: str, env_var: str, default: str) -> str:
            """Env var > YAML > default."""
            env_val = os.environ.get(env_var)
            if env_val is not None:
                return env_val
            yaml_val = _yaml_cfg.get(key)
            if yaml_val is not None:
                return str(yaml_val)
            return default
```

Then replace these selected `os.environ.get()` calls with `_cfg()`:
- `approval_timeout_s=float(_cfg("approval_timeout_s", "JARVIS_APPROVAL_TIMEOUT_S", "600"))`
- `pipeline_timeout_s=float(_cfg("pipeline_timeout_s", "JARVIS_PIPELINE_TIMEOUT_S", "600.0"))`
- `max_concurrent_ops=int(_cfg("max_concurrent_ops", "JARVIS_GOVERNED_MAX_CONCURRENT_OPS", "2"))`
- `generation_timeout_s=float(_cfg("generation_timeout_s", "JARVIS_GENERATION_TIMEOUT_S", "120"))`

Leave all other params using existing `os.getenv()` -- they can be migrated incrementally.

- [ ] **Step 7: Run tests**

Run: `python3 -m pytest tests/governance/test_config_loader.py -v`
Expected: 7 passed

Run: `python3 -m pytest tests/governance/ --tb=short 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/config_loader.py \
        backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/test_config_loader.py
git commit -m "feat(gap7): multi-level YAML config inheritance for GovernedLoopConfig

- config_loader: loads ~/.jarvis/governance.yaml -> repo -> .local.yaml
- Later levels override earlier; env vars override all
- GovernedLoopConfig.from_env() uses load_layered_config for key params
- Malformed YAML and missing files handled gracefully
- Tests: 7 passing"
```

---

## Task 4: Structured Elicitation (GAP 5)

**Files:**
- Modify: `backend/core/ouroboros/governance/approval_provider.py:130-150,243+`
- Test: `tests/governance/test_elicitation.py`

- [ ] **Step 1: Write failing tests for elicitation**

```python
# tests/governance/test_elicitation.py
"""Structured mid-operation elicitation -- extends ApprovalProvider."""
import asyncio
import inspect
import pytest
from backend.core.ouroboros.governance.approval_provider import (
    CLIApprovalProvider, ApprovalProvider,
)


def test_approval_provider_has_elicit_method():
    """Protocol must define elicit()."""
    assert hasattr(ApprovalProvider, "elicit")
    sig = inspect.signature(ApprovalProvider.elicit)
    params = list(sig.parameters.keys())
    assert "request_id" in params
    assert "question" in params


def test_cli_approval_provider_has_elicit():
    """CLIApprovalProvider must implement elicit()."""
    provider = CLIApprovalProvider()
    assert hasattr(provider, "elicit")
    assert asyncio.iscoroutinefunction(provider.elicit)


@pytest.mark.asyncio
async def test_elicit_returns_answer():
    """elicit() should return the programmatic answer when provided."""
    provider = CLIApprovalProvider()
    from backend.core.ouroboros.governance.op_context import OperationContext
    ctx = OperationContext.create(
        op_id="elicit-test", description="test", target_files=("a.py",)
    )
    await provider.request(ctx)
    # Simulate programmatic elicitation response
    provider._set_elicitation_answer("elicit-test", "option_b")
    result = await provider.elicit(
        request_id="elicit-test",
        question="Use approach A or B?",
        options=["option_a", "option_b"],
        timeout_s=5.0,
    )
    assert result == "option_b"


@pytest.mark.asyncio
async def test_elicit_timeout_returns_none():
    """elicit() should return None on timeout (no answer provided)."""
    provider = CLIApprovalProvider()
    from backend.core.ouroboros.governance.op_context import OperationContext
    ctx = OperationContext.create(
        op_id="elicit-timeout", description="test", target_files=("a.py",)
    )
    await provider.request(ctx)
    result = await provider.elicit(
        request_id="elicit-timeout",
        question="Pick one?",
        options=["a", "b"],
        timeout_s=0.05,
    )
    assert result is None


@pytest.mark.asyncio
async def test_elicit_unknown_request_raises():
    """elicit() for unknown request_id should raise KeyError."""
    provider = CLIApprovalProvider()
    with pytest.raises(KeyError):
        await provider.elicit(
            request_id="nonexistent",
            question="?",
            timeout_s=1.0,
        )
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m pytest tests/governance/test_elicitation.py -v`
Expected: Failures -- `elicit` method not found

- [ ] **Step 3: Add `elicit()` to ApprovalProvider protocol**

In `approval_provider.py`, after the `await_decision()` method in the `ApprovalProvider` protocol (around line 150), add:

```python
    async def elicit(
        self,
        request_id: str,
        question: str,
        options: Optional[List[str]] = None,
        timeout_s: float = 300.0,
    ) -> Optional[str]:
        """Ask the user a structured question mid-operation.

        Returns the user's answer as a string, or None if timeout expires.
        """
        ...
```

Add `from typing import Optional, List` if not already imported.

- [ ] **Step 4: Implement `elicit()` and `_set_elicitation_answer()` in CLIApprovalProvider**

First update `_PendingRequest` dataclass to include elicitation fields:
```python
    elicitation_question: Optional[str] = None
    elicitation_options: Optional[List[str]] = None
    elicitation_answer: Optional[str] = None
    elicitation_event: Optional[asyncio.Event] = None
```

Then add methods to `CLIApprovalProvider`:

```python
    def _set_elicitation_answer(self, request_id: str, answer: str) -> None:
        """Programmatically provide an elicitation answer (for API/test use)."""
        pending = self._get_or_raise(request_id)
        pending.elicitation_answer = answer
        if pending.elicitation_event is not None:
            pending.elicitation_event.set()

    async def elicit(
        self,
        request_id: str,
        question: str,
        options: Optional[List[str]] = None,
        timeout_s: float = 300.0,
    ) -> Optional[str]:
        """Ask the user a structured question. Returns answer or None on timeout."""
        pending = self._get_or_raise(request_id)
        pending.elicitation_question = question
        pending.elicitation_options = options
        pending.elicitation_event = asyncio.Event()
        pending.elicitation_answer = None
        logger.info(
            "[Approval] ELICIT: %s question=%r options=%s",
            request_id, question, options,
        )
        try:
            await asyncio.wait_for(pending.elicitation_event.wait(), timeout=timeout_s)
            return pending.elicitation_answer
        except asyncio.TimeoutError:
            logger.info("[Approval] ELICIT timeout: %s", request_id)
            return None
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/governance/test_elicitation.py -v`
Expected: 5 passed

Run: `python3 -m pytest tests/governance/ --tb=short 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/approval_provider.py \
        tests/governance/test_elicitation.py
git commit -m "feat(gap5): structured mid-operation elicitation via ApprovalProvider

- ApprovalProvider protocol: elicit(request_id, question, options, timeout_s)
- CLIApprovalProvider: implements elicit() with asyncio.Event + timeout
- _set_elicitation_answer() for programmatic/API use
- Timeout returns None (non-blocking); unknown request raises KeyError
- Tests: 5 passing"
```

---

## Task 5: Subagent Git Worktree Isolation (GAP 9)

**Files:**
- Create: `backend/core/ouroboros/governance/worktree_manager.py`
- Test: `tests/governance/test_worktree_isolation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/governance/test_worktree_isolation.py
"""Worktree lifecycle manager for subagent isolation."""
import asyncio
import subprocess
import pytest
from pathlib import Path
from backend.core.ouroboros.governance.worktree_manager import WorktreeManager


@pytest.mark.asyncio
async def test_create_and_cleanup(tmp_path):
    """Full lifecycle: create worktree, get path, cleanup."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
    )

    mgr = WorktreeManager(repo_root=tmp_path)
    wt_path = await mgr.create(branch_name="ouroboros/test-wu-001")
    assert wt_path.exists()
    assert (wt_path / ".git").exists()

    await mgr.cleanup(wt_path)
    assert not wt_path.exists()


@pytest.mark.asyncio
async def test_cleanup_nonexistent_path_is_safe(tmp_path):
    mgr = WorktreeManager(repo_root=tmp_path)
    await mgr.cleanup(tmp_path / "nonexistent")
    # No error raised


def test_worktree_manager_has_create_and_cleanup():
    assert hasattr(WorktreeManager, "create")
    assert hasattr(WorktreeManager, "cleanup")
    assert asyncio.iscoroutinefunction(WorktreeManager.create)
    assert asyncio.iscoroutinefunction(WorktreeManager.cleanup)
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m pytest tests/governance/test_worktree_isolation.py -v`
Expected: ImportError

- [ ] **Step 3: Create `worktree_manager.py`**

```python
# backend/core/ouroboros/governance/worktree_manager.py
"""WorktreeManager -- git worktree lifecycle for subagent isolation.

GAP 9: Creates per-work-unit git worktrees so parallel subagents
never conflict at the filesystem level. Auto-cleaned after completion.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorktreeManager:
    """Manages git worktree lifecycle for subagent execution isolation.

    Parameters
    ----------
    repo_root:
        Root of the main repository.
    worktree_base:
        Directory where worktrees are created. Defaults to <repo_root>/.worktrees.
    """

    def __init__(self, repo_root: Path, worktree_base: Optional[Path] = None) -> None:
        self._repo_root = Path(repo_root)
        self._worktree_base = worktree_base or (self._repo_root / ".worktrees")

    async def create(self, branch_name: str) -> Path:
        """Create a git worktree with the given branch name.

        Returns the path to the new worktree directory.
        """
        safe_name = branch_name.replace("/", "-")
        wt_path = self._worktree_base / safe_name
        self._worktree_base.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", str(wt_path), "-b", branch_name,
            cwd=str(self._repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed (rc={proc.returncode}): {stderr.decode()}"
            )
        logger.info("[WorktreeManager] Created worktree at %s (branch: %s)", wt_path, branch_name)
        return wt_path

    async def cleanup(self, worktree_path: Path) -> None:
        """Remove a git worktree and its directory."""
        wt = Path(worktree_path)
        if not wt.exists():
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", str(wt), "--force",
                cwd=str(self._repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0 and wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
            logger.info("[WorktreeManager] Cleaned up worktree at %s", wt)
        except Exception as exc:
            logger.warning("[WorktreeManager] Cleanup failed for %s: %s", wt, exc)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/governance/test_worktree_isolation.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/worktree_manager.py \
        tests/governance/test_worktree_isolation.py
git commit -m "feat(gap9): WorktreeManager for subagent git worktree isolation

- WorktreeManager: async create(branch_name) and cleanup(worktree_path)
- Uses asyncio.create_subprocess_exec for git worktree add/remove
- Fallback to shutil.rmtree if git worktree remove fails
- Ready to wire into SubagentScheduler._execute_unit_guarded()
- Tests: 3 passing"
```

---

## Task 6: OuroborosMCPServer -- Inbound MCP Endpoint (GAP 10)

**Files:**
- Create: `backend/core/ouroboros/governance/mcp_server.py`
- Test: `tests/governance/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/governance/test_mcp_server.py
"""OuroborosMCPServer -- inbound MCP endpoint for external agents."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer


def test_server_has_required_tools():
    server = OuroborosMCPServer(gls=MagicMock())
    assert hasattr(server, "submit_intent")
    assert hasattr(server, "get_operation_status")
    assert hasattr(server, "approve_operation")


@pytest.mark.asyncio
async def test_submit_intent_calls_gls():
    gls = MagicMock()
    gls.submit = AsyncMock(return_value=MagicMock(op_id="op-123"))
    server = OuroborosMCPServer(gls=gls)
    result = await server.submit_intent(
        goal="fix bug", target_files=["backend/foo.py"], repo="jarvis"
    )
    assert "op_id" in result
    gls.submit.assert_called_once()


@pytest.mark.asyncio
async def test_get_operation_status_returns_dict():
    gls = MagicMock()
    gls.get_operation_result = MagicMock(return_value=None)
    server = OuroborosMCPServer(gls=gls)
    result = await server.get_operation_status(op_id="op-123")
    assert isinstance(result, dict)
    assert "status" in result


@pytest.mark.asyncio
async def test_approve_operation_delegates():
    gls = MagicMock()
    approval = MagicMock()
    approval.approve = AsyncMock(return_value=MagicMock(status=MagicMock(value="APPROVED")))
    gls._approval_provider = approval
    server = OuroborosMCPServer(gls=gls)
    result = await server.approve_operation(request_id="op-123", approver="ci_bot")
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_submit_intent_error_returns_error_dict():
    gls = MagicMock()
    gls.submit = AsyncMock(side_effect=RuntimeError("boom"))
    server = OuroborosMCPServer(gls=gls)
    result = await server.submit_intent(goal="fix", target_files=["a.py"])
    assert result["status"] == "error"
    assert "boom" in result["error"]
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m pytest tests/governance/test_mcp_server.py -v`
Expected: ImportError

- [ ] **Step 3: Create `mcp_server.py`**

```python
# backend/core/ouroboros/governance/mcp_server.py
"""OuroborosMCPServer -- inbound MCP endpoint for external agents.

GAP 10: Makes Ouroboros driveable from any MCP client (IDE, CI, other agents).
Exposes submit_intent, get_operation_status, and approve_operation as tools.

This is a standalone class that wraps GovernedLoopService -- it does NOT
start a server. The actual MCP transport (FastAPI/stdio) is wired externally.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class OuroborosMCPServer:
    """MCP-compatible tool interface for Ouroboros governance pipeline.

    Parameters
    ----------
    gls:
        GovernedLoopService instance (or any object with submit/get_operation_result).
    """

    def __init__(self, gls: Any) -> None:
        self._gls = gls

    async def submit_intent(
        self,
        goal: str,
        target_files: List[str],
        repo: str = "jarvis",
    ) -> Dict[str, Any]:
        """Submit an intent to the Ouroboros pipeline.

        Returns dict with op_id and submission status.
        """
        try:
            from backend.core.ouroboros.governance.op_context import OperationContext
            ctx = OperationContext.create(
                description=goal,
                target_files=tuple(target_files),
                primary_repo=repo,
            )
            result = await self._gls.submit(ctx, trigger_source="mcp_server")
            return {
                "op_id": ctx.op_id,
                "status": "submitted",
                "terminal_phase": getattr(result, "terminal_phase", None),
            }
        except Exception as exc:
            logger.warning("[MCPServer] submit_intent failed: %s", exc)
            return {"op_id": None, "status": "error", "error": str(exc)}

    async def get_operation_status(self, op_id: str) -> Dict[str, Any]:
        """Get the status of a previously submitted operation."""
        try:
            result = self._gls.get_operation_result(op_id)
            if result is None:
                return {"op_id": op_id, "status": "not_found"}
            return {
                "op_id": op_id,
                "status": str(getattr(result, "terminal_phase", "unknown")),
                "duration_s": getattr(result, "total_duration_s", None),
                "reason_code": getattr(result, "reason_code", None),
            }
        except Exception as exc:
            logger.warning("[MCPServer] get_operation_status failed: %s", exc)
            return {"op_id": op_id, "status": "error", "error": str(exc)}

    async def approve_operation(
        self,
        request_id: str,
        approver: str = "mcp_client",
    ) -> Dict[str, Any]:
        """Approve a pending operation via the approval provider."""
        try:
            provider = getattr(self._gls, "_approval_provider", None)
            if provider is None:
                return {"request_id": request_id, "status": "error", "error": "no_approval_provider"}
            result = await provider.approve(request_id=request_id, approver=approver)
            return {
                "request_id": request_id,
                "status": result.status.value if hasattr(result.status, "value") else str(result.status),
                "approver": approver,
            }
        except Exception as exc:
            logger.warning("[MCPServer] approve_operation failed: %s", exc)
            return {"request_id": request_id, "status": "error", "error": str(exc)}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/governance/test_mcp_server.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/mcp_server.py \
        tests/governance/test_mcp_server.py
git commit -m "feat(gap10): OuroborosMCPServer exposes inbound MCP tool interface

- submit_intent: forwards to GLS.submit() with trigger_source='mcp_server'
- get_operation_status: queries completed ops by op_id
- approve_operation: delegates to approval_provider.approve()
- Error handling returns structured error dicts (never raises)
- Standalone class -- MCP transport wired externally
- Tests: 5 passing"
```

---

## Final Verification

- [ ] **Run entire governance test suite**

```bash
python3 -m pytest tests/governance/ -v --tb=short 2>&1 | tail -40
```

Expected: All new tests pass; pre-existing failures unchanged.

- [ ] **Verify new test count**

```bash
python3 -m pytest tests/governance/ --collect-only -q 2>&1 | tail -5
```

Expected: ~38+ new tests across 6 new test files.
