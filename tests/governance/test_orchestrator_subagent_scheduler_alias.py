"""Regression test for the W3(6) Slice 4 wiring bug surfaced in S7.

S7 (`bt-2026-04-25-001939`) showed the enforce-mode `dispatch_fanout` log
line `enforce_fanout skipped: orchestrator has no _subagent_scheduler
reference` despite both the master flag and the enforce flag being on.
Root cause: `phase_dispatcher.py` reads `orchestrator._subagent_scheduler`
but the orchestrator stores the same handle as
`_config.execution_graph_scheduler` (passed in via `OrchestratorConfig`
from `governed_loop_service`). The attribute name mismatch made the
enforce path structurally unreachable since W3(6) Slice 4 shipped.

This test pins the reachability contract:

- When `OrchestratorConfig(execution_graph_scheduler=<scheduler>)` is set,
  `orchestrator._subagent_scheduler` MUST resolve to that same scheduler
  (not `None`).
- When unset (default `None`), the alias MUST still be readable and
  return `None` (not raise `AttributeError`).

Without the alias property on `GovernedOrchestrator`, the first assertion
fails. With the alias, both pass — closing the wiring gap loudly the
next time anyone refactors either side.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)


def _make_orchestrator(execution_graph_scheduler=None) -> GovernedOrchestrator:
    """Construct an orchestrator with minimal mocked deps + an optional scheduler."""
    config = OrchestratorConfig(
        project_root=Path("."),
        repo_registry=MagicMock(),
        execution_graph_scheduler=execution_graph_scheduler,
    )
    return GovernedOrchestrator(
        stack=MagicMock(),
        generator=MagicMock(),
        approval_provider=MagicMock(),
        config=config,
        validation_runner=MagicMock(),
    )


def test_subagent_scheduler_alias_resolves_when_config_set():
    """When OrchestratorConfig.execution_graph_scheduler is set, the
    `_subagent_scheduler` alias must return the same handle.

    This is the assertion that fails pre-fix: pre-fix
    `orchestrator._subagent_scheduler` is `AttributeError` (no such attribute)
    so `getattr(orch, "_subagent_scheduler", None)` returned `None` in the
    phase_dispatcher's call site, skipping the enforce path.
    """
    sentinel_scheduler = MagicMock(name="execution_graph_scheduler_sentinel")
    orch = _make_orchestrator(execution_graph_scheduler=sentinel_scheduler)

    assert orch._subagent_scheduler is sentinel_scheduler


def test_subagent_scheduler_alias_returns_none_when_config_unset():
    """When the config field is None, the alias returns None — NOT raise.

    Pre-fix: AttributeError on the property name (no property existed,
    no instance attribute either). Post-fix: property exists and forwards
    `None` cleanly.
    """
    orch = _make_orchestrator(execution_graph_scheduler=None)

    assert orch._subagent_scheduler is None


def test_phase_dispatcher_getattr_pattern_returns_scheduler_post_fix():
    """Pin the EXACT call shape used by phase_dispatcher.py:608.

    The dispatcher does `getattr(orchestrator, "_subagent_scheduler", None)`.
    Pre-fix that returned `None` (no such attribute, default fallback fired).
    Post-fix it returns the scheduler handle.
    """
    sentinel_scheduler = MagicMock(name="scheduler_for_getattr_test")
    orch = _make_orchestrator(execution_graph_scheduler=sentinel_scheduler)

    resolved = getattr(orch, "_subagent_scheduler", None)

    assert resolved is sentinel_scheduler, (
        "Pre-fix: getattr returned None because _subagent_scheduler was not "
        "an attribute on GovernedOrchestrator (orchestrator stored the "
        "scheduler as _config.execution_graph_scheduler instead). The "
        "phase_dispatcher's enforce-mode dispatch_fanout path then logged "
        "`enforce_fanout skipped: orchestrator has no _subagent_scheduler "
        "reference` and never invoked the W3(6) Slice 4 enforce-mode "
        "fanout. The @property alias on GovernedOrchestrator restores "
        "reachability."
    )
