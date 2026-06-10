"""Slice 198 — Sovereign Ignition Protocol.

Slice 197 unlocked the M10 proposer autonomously, but verification found it
half-wired: graduated yet un-ignited (``cadence_enabled=False``) with the
M10 protection bundle dark (taste layer + orange-PR reviewer off). This slice
closes the gap — the moment the organism graduates, the cadence loop ignites
and the protection gates arm THEMSELVES via live assertions, with the same
non-negotiable invariants as 197:

  * Kill switch supreme — an explicit ``=0`` on any of the three sub-flags
    beats autonomous ignition, always.
  * Live assertions are HONEST and fail-closed — a substrate that cannot
    actually run (orange-PR with no gh/git) stays dark; it is never
    force-toggled.
  * governance_boundary_gate untouched (grep-pinned).

Pins:
  * cadence_enabled three-state: ignites with the autonomous unlock.
  * taste_layer_assertion_passes — synthetic micro-proposal responsiveness
    probe via the master-independent assess_file scorer.
  * orange_pr_assertion_passes — gh + git-work-tree + remote preflight; NO
    push, NO blocking CLI prompt.
  * taste/orange master predicates arm only when unlocked AND assertion
    passes; explicit env wins either way.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.m10.cadence_runner import cadence_enabled
from backend.core.ouroboros.governance.m10.primitives import (
    m10_arch_proposer_enabled,
)
from backend.core.ouroboros.governance.m10_autonomous_graduation import (
    _reset_for_tests,
    m10_cadence_ignited,
    orange_pr_armed,
    orange_pr_assertion_passes,
    taste_layer_armed,
    taste_layer_assertion_passes,
)
from backend.core.ouroboros.governance.observability_registry import (
    HEDGE_CONCURRENCY_DISPATCHES,
    _reset_singleton_for_tests,
    get_observability_registry,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "reg.bin"),
    )
    monkeypatch.setenv(
        "JARVIS_M10_GRADUATION_STATE_PATH", str(tmp_path / "m10_state.json"),
    )
    for var in (
        "JARVIS_OBSERVABILITY_REGISTRY_ENABLED",
        "JARVIS_M10_ARCH_PROPOSER_ENABLED",
        "JARVIS_M10_AUTONOMOUS_GRADUATION_ENABLED",
        "JARVIS_M10_CADENCE_ENABLED",
        "JARVIS_ARCHITECTURAL_TASTE_ENABLED",
        "JARVIS_ORANGE_PR_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset_singleton_for_tests()
    _reset_for_tests()
    yield
    _reset_singleton_for_tests()
    _reset_for_tests()


def _graduate():
    """Drive the organism to an autonomous unlock (healthy registry)."""
    get_observability_registry().incr(HEDGE_CONCURRENCY_DISPATCHES, 6)
    assert m10_arch_proposer_enabled() is True  # unlock persists


# ===========================================================================
# A — cadence ignition (the core deliverable, works everywhere)
# ===========================================================================

def test_cadence_dark_before_graduation():
    assert cadence_enabled() is False


def test_cadence_ignites_on_graduation():
    _graduate()
    assert m10_cadence_ignited() is True
    assert cadence_enabled() is True


def test_cadence_kill_switch_supreme(monkeypatch):
    _graduate()
    monkeypatch.setenv("JARVIS_M10_CADENCE_ENABLED", "0")
    assert cadence_enabled() is False


def test_cadence_explicit_on_without_graduation(monkeypatch):
    monkeypatch.setenv("JARVIS_M10_ARCH_PROPOSER_ENABLED", "1")
    monkeypatch.setenv("JARVIS_M10_CADENCE_ENABLED", "1")
    assert cadence_enabled() is True


# ===========================================================================
# B — taste layer live assertion (synthetic responsiveness probe)
# ===========================================================================

def test_taste_assertion_passes_on_responsive_scorer():
    """The synthetic micro-proposal scores cleanly → filter is responsive."""
    assert taste_layer_assertion_passes() is True


def test_taste_assertion_fails_closed_on_unresponsive_probe():
    assert taste_layer_assertion_passes(
        _assess_probe=lambda: None,  # scorer returned nothing
    ) is False


def test_taste_assertion_fails_closed_on_raising_probe():
    def _boom():
        raise RuntimeError("scorer exploded")
    assert taste_layer_assertion_passes(_assess_probe=_boom) is False


def test_taste_arms_only_when_unlocked_and_responsive():
    assert taste_layer_armed() is False  # not graduated yet
    _graduate()
    assert taste_layer_armed() is True


def test_taste_master_kill_switch_supreme(monkeypatch):
    from backend.core.ouroboros.governance.architectural_taste_layer import (
        master_enabled as taste_master,
    )
    _graduate()
    assert taste_master() is True  # armed via graduation
    monkeypatch.setenv("JARVIS_ARCHITECTURAL_TASTE_ENABLED", "0")
    assert taste_master() is False


# ===========================================================================
# C — orange PR live assertion (gh + git preflight, NO push)
# ===========================================================================

def test_orange_assertion_passes_when_gh_and_git_present():
    assert orange_pr_assertion_passes(
        _gh_probe=lambda: True, _git_probe=lambda: True,
    ) is True


def test_orange_assertion_fails_closed_without_gh():
    assert orange_pr_assertion_passes(
        _gh_probe=lambda: False, _git_probe=lambda: True,
    ) is False


def test_orange_assertion_fails_closed_without_git():
    assert orange_pr_assertion_passes(
        _gh_probe=lambda: True, _git_probe=lambda: False,
    ) is False


def test_orange_assertion_never_raises():
    def _boom():
        raise OSError("subprocess died")
    assert orange_pr_assertion_passes(_gh_probe=_boom, _git_probe=_boom) is False


def test_orange_arms_only_when_unlocked_and_preflight_passes(monkeypatch):
    _graduate()
    # Force the preflight to pass deterministically (CI has no gh auth).
    import backend.core.ouroboros.governance.m10_autonomous_graduation as mag
    monkeypatch.setattr(mag, "orange_pr_assertion_passes", lambda: True)
    assert orange_pr_armed() is True


def test_orange_master_kill_switch_supreme(monkeypatch):
    from backend.core.ouroboros.governance.orange_pr_reviewer import (
        is_orange_pr_enabled,
    )
    _graduate()
    monkeypatch.setenv("JARVIS_ORANGE_PR_ENABLED", "0")
    assert is_orange_pr_enabled() is False


def test_orange_explicit_on_wins(monkeypatch):
    from backend.core.ouroboros.governance.orange_pr_reviewer import (
        is_orange_pr_enabled,
    )
    monkeypatch.setenv("JARVIS_ORANGE_PR_ENABLED", "1")
    assert is_orange_pr_enabled() is True


# ===========================================================================
# D — end-to-end: graduation ignites cadence + arms taste, no env change
# ===========================================================================

def test_graduation_ignites_full_stack_no_env_change(monkeypatch):
    import backend.core.ouroboros.governance.m10_autonomous_graduation as mag
    monkeypatch.setattr(mag, "orange_pr_assertion_passes", lambda: True)
    # Cold: nothing on.
    assert cadence_enabled() is False
    assert taste_layer_armed() is False
    assert orange_pr_armed() is False
    # The organism graduates itself from registry metrics — zero env edits.
    _graduate()
    assert m10_arch_proposer_enabled() is True
    assert cadence_enabled() is True
    assert taste_layer_armed() is True
    assert orange_pr_armed() is True


# ===========================================================================
# E — wiring + doctrine pins
# ===========================================================================

def test_cadence_runner_consults_ignition():
    src = (_GOV / "m10" / "cadence_runner.py").read_text(encoding="utf-8")
    assert "m10_cadence_ignited" in src


def test_taste_master_consults_arming():
    src = (_GOV / "architectural_taste_layer.py").read_text(encoding="utf-8")
    assert "taste_layer_armed" in src


def test_orange_consults_arming():
    src = (_GOV / "orange_pr_reviewer.py").read_text(encoding="utf-8")
    assert "orange_pr_armed" in src


def test_boundary_gate_not_weakened():
    src = (_GOV / "governance_boundary_gate.py").read_text(encoding="utf-8")
    assert "APPROVAL_REQUIRED" in src
    assert "m10_autonomous_graduation" not in src
    assert "orange_pr_armed" not in src
