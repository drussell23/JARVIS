"""Task #1 — the self-modification cage hole, closed.

Regression spine for the audit finding: the self-mod BLOCK in RiskEngine was
source-gated to {exploration, roadmap, architecture} only, and its sentinel
list was a hand-maintained per-file enumeration that went stale when the live
GATE enforcement code was extracted into ``phase_runners/``. Verified live: the
orchestrator's main ``_build_profile`` never sets ``source`` (defaults to ``""``)
and governance files don't trip the ``touches_*`` boolean heuristics — so a
governance self-edit fell all the way through to the general rules and could
land SAFE_AUTO/NOTIFY_APPLY.

These tests assert the fix from the ATTACKER's side: every source (especially
the empty default and the general sensor sources), and every governance path
(especially the ``phase_runners/`` package the old list missed), is now caught
by the unconditional, package-derived, source-independent self-protection gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskEngine,
    RiskTier,
)


def _profile(file_path: str, *, source: str = "", blast_radius: int = 1) -> OperationProfile:
    """A profile that would otherwise classify SAFE_AUTO — benign on every
    axis EXCEPT the file it targets. This is the attack shape: a small,
    high-confidence, single-file edit that lands SAFE_AUTO unless the
    self-protection gate catches the *path*."""
    return OperationProfile(
        files_affected=[Path(file_path)],
        change_type=ChangeType.MODIFY,
        blast_radius=blast_radius,
        crosses_repo_boundary=False,
        touches_security_surface=False,   # deliberately False — governance files
        touches_supervisor=False,          # don't trip these heuristics
        test_scope_confidence=0.99,
        source=source,
    )


# The live enforcement code the OLD per-file sentinel list missed entirely.
LIVE_CAGE_PATHS = [
    "backend/core/ouroboros/governance/phase_runners/gate_runner.py",
    "backend/core/ouroboros/governance/phase_runners/slice4b_runner.py",
    "backend/core/ouroboros/governance/phase_runners/generate_runner.py",
    "backend/core/ouroboros/governance/phase_runners/validate_runner.py",
    "backend/core/ouroboros/governance/phase_dispatcher.py",
    "backend/core/ouroboros/governance/scoped_tool_backend.py",
    # …and the classics, still covered
    "backend/core/ouroboros/governance/orchestrator.py",
    "backend/core/ouroboros/governance/risk_engine.py",
    "backend/core/ouroboros/governance/semantic_guardian.py",
]

# The full spectrum of real sources observed in the tree (grep of source=…),
# NONE of which is exploration/roadmap/architecture — every one bypassed the
# old block.
GENERAL_SOURCES = [
    "",                    # THE MAIN PATH: orchestrator _build_profile sets nothing
    "testfailure",
    "backlog",
    "opportunity_miner",
    "ai_miner",
    "postmortem",
    "todo_scanner",
    "intent_discovery",
    "voice_command",
    "runtime_health",
    "github",
    "contract",
    "ledger",
    "cadence_synthetic",
]


# ── The hole: general/default sources editing the live cage ───────────────

@pytest.mark.parametrize("path", LIVE_CAGE_PATHS)
@pytest.mark.parametrize("source", GENERAL_SOURCES)
def test_general_source_cannot_self_modify_the_cage(path, source):
    """THE FIX. Any non-sanctioned source editing any governance/cage file is
    BLOCKED — never SAFE_AUTO. Covers the empty default the orchestrator emits
    and the phase_runners/ package the old list missed."""
    result = RiskEngine().classify(_profile(path, source=source))
    assert result.tier == RiskTier.BLOCKED, (
        f"HOLE OPEN: source={source!r} editing {path} classified "
        f"{result.tier} ({result.reason_code}) — must be BLOCKED"
    )
    assert result.reason_code == "self_modification_unsanctioned_source"


def test_the_exact_incident_shape_is_blocked():
    """The audit's concrete attack: a Backlog-sourced op that edits gate_runner.py
    to drop the SemanticGuardian call. Benign on every risk axis, would have
    landed SAFE_AUTO. Now BLOCKED."""
    result = RiskEngine().classify(_profile(
        "backend/core/ouroboros/governance/phase_runners/gate_runner.py",
        source="backlog",
    ))
    assert result.tier == RiskTier.BLOCKED


# ── Sanctioned + untrusted paths preserved (historical contract) ──────────

@pytest.mark.parametrize("path", LIVE_CAGE_PATHS)
def test_architecture_self_mod_is_approval_required(path):
    """The one sanctioned Order-2 path stays human-gated, not blocked —
    including for the newly-covered phase_runners/ modules."""
    result = RiskEngine().classify(_profile(path, source="architecture"))
    assert result.tier == RiskTier.APPROVAL_REQUIRED
    assert result.reason_code == "architecture_self_modification"


@pytest.mark.parametrize("path", LIVE_CAGE_PATHS)
@pytest.mark.parametrize("source", ["exploration", "roadmap"])
def test_untrusted_self_mod_is_blocked(path, source):
    result = RiskEngine().classify(_profile(path, source=source))
    assert result.tier == RiskTier.BLOCKED
    assert result.reason_code == "exploration_self_modification"


# ── Kernel + security are BLOCKED for EVERY source ────────────────────────

@pytest.mark.parametrize("source", GENERAL_SOURCES + ["exploration", "architecture"])
def test_kernel_blocked_for_every_source(source):
    result = RiskEngine().classify(_profile(
        "backend/jarvis/unified_supervisor.py", source=source,
    ))
    assert result.tier == RiskTier.BLOCKED
    assert "touches_kernel" in result.reason_code


@pytest.mark.parametrize("source", GENERAL_SOURCES + ["exploration", "architecture"])
def test_security_surface_blocked_for_every_source(source):
    result = RiskEngine().classify(_profile(
        "backend/app/auth/credential_store.py", source=source,
    ))
    assert result.tier == RiskTier.BLOCKED
    assert "touches_security" in result.reason_code


# ── No false positives: benign work is untouched for every source ─────────

@pytest.mark.parametrize("source", GENERAL_SOURCES + ["exploration", "architecture"])
def test_benign_files_not_escalated(source):
    """Normal O+V work — vision, voice, tests, app code — must NOT be caught by
    the cage gate. This is what keeps the fix from strangling ordinary autonomy."""
    for benign in (
        "backend/vision/frame_server.py",
        "backend/voice/wake_word.py",
        "src/app.py",
        "tests/test_something.py",
    ):
        result = RiskEngine().classify(_profile(benign, source=source))
        assert result.tier not in (RiskTier.BLOCKED,), (
            f"FALSE POSITIVE: source={source!r} on benign {benign} → {result.tier}"
        )


# ── Refactor-proof by construction: a hypothetical FUTURE governance module ─

def test_future_governance_module_auto_covered():
    """The package-derived sentinel means a governance module that does not even
    exist yet is already protected — the property the old per-file list lacked."""
    result = RiskEngine().classify(_profile(
        "backend/core/ouroboros/governance/some_future_gate_v99.py",
        source="backlog",
    ))
    assert result.tier == RiskTier.BLOCKED


# ── Env-additive protection (widen only; never weaken) ────────────────────

def test_env_can_widen_protection(monkeypatch):
    """An operator can ADD a sentinel to protect a path outside the governance
    package (additive-only). Here: protect a hypothetical external policy file."""
    target = "backend/policy/external_ruleset.py"
    # Baseline: not a cage file → benign.
    assert RiskEngine().classify(_profile(target, source="backlog")).tier != RiskTier.BLOCKED
    monkeypatch.setenv("JARVIS_RISK_SELF_MOD_EXTRA_SENTINELS", "policy/external_ruleset")
    assert RiskEngine().classify(_profile(target, source="backlog")).tier == RiskTier.BLOCKED


def test_env_cannot_weaken_the_baseline(monkeypatch):
    """No env value removes a baseline sentinel. Even a hostile env cannot
    un-protect the governance package."""
    monkeypatch.setenv("JARVIS_RISK_SELF_MOD_EXTRA_SENTINELS", "")
    monkeypatch.setenv("JARVIS_RISK_KERNEL_EXTRA_SENTINELS", "garbage,,, ")
    result = RiskEngine().classify(_profile(
        "backend/core/ouroboros/governance/orchestrator.py", source="backlog",
    ))
    assert result.tier == RiskTier.BLOCKED


def test_sanctioned_source_set_is_not_env_configurable(monkeypatch):
    """The PERMISSION to self-modify cannot be widened by env — only the
    architecture source is ever APPROVAL_REQUIRED; a hostile env naming another
    source as sanctioned has no effect (the tuple is immutable)."""
    monkeypatch.setenv("JARVIS_RISK_SANCTIONED_SELF_MOD_SOURCES", "backlog,testfailure")
    result = RiskEngine().classify(_profile(
        "backend/core/ouroboros/governance/gate_runner.py", source="backlog",
    ))
    # Still BLOCKED — the env var is ignored by design.
    assert result.tier == RiskTier.BLOCKED
