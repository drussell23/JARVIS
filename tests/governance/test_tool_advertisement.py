"""A tool the model is never shown cannot be chosen.

2026-10-08: the generation prompt's tool list was hand-written and had
drifted from the manifests -- 10 of 29 tools unadvertised, among them
``delegate_to_agent`` (the ExplorationFleet was wired; the soak used it 0
times), and ``dispatch_subagent`` described as "explore only" while
review/plan/general were wired and allowed by the policy.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import providers as P
from backend.core.ouroboros.governance.tool_executor import _L1_MANIFESTS


def test_every_manifest_tool_is_advertised_or_excluded_with_a_reason(monkeypatch):
    monkeypatch.delenv("JARVIS_TOOL_DELEGATE_AGENT_ENABLED", raising=False)
    text = P._build_tool_section()
    unadvertised = {n for n in _L1_MANIFESTS if f"`{n}(" not in text}
    assert unadvertised == set(P._UNADVERTISED_BY_DESIGN), (
        f"advertise these or document why not: {sorted(unadvertised ^ set(P._UNADVERTISED_BY_DESIGN))}")
    assert all(reason.strip() for reason in P._UNADVERTISED_BY_DESIGN.values())


def test_the_subagent_types_shown_are_the_policys(monkeypatch):
    from backend.core.ouroboros.governance.subagent_contracts import policy_allowed_subagent_types
    block = P._subagent_tools_block()
    for t in policy_allowed_subagent_types():
        assert t in block
    assert "explore only" not in block and "'explore' only" not in block


def test_a_disabled_subagent_type_is_not_shown(monkeypatch):
    from backend.core.ouroboros.governance.subagent_contracts import policy_allowed_subagent_types
    allowed = sorted(policy_allowed_subagent_types())
    if len(allowed) < 2:
        pytest.skip("needs two enabled types to disable one")
    victim = allowed[-1]
    monkeypatch.setenv(f"JARVIS_SUBAGENT_{victim.upper()}_ENABLED", "false")
    if victim in policy_allowed_subagent_types():
        pytest.skip(f"{victim} has no per-type kill switch by that name")
    line = next(l for l in P._subagent_tools_block().splitlines() if "dispatch_subagent(" in l)
    assert victim not in line


def test_delegate_is_shown_exactly_when_the_policy_would_run_it(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_DELEGATE_AGENT_ENABLED", "true")
    assert "`delegate_to_agent(" in P._subagent_tools_block()
    monkeypatch.setenv("JARVIS_TOOL_DELEGATE_AGENT_ENABLED", "false")
    assert "`delegate_to_agent(" not in P._subagent_tools_block()
