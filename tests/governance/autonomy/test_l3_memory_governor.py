from __future__ import annotations

from backend.core.ouroboros.governance.autonomy.l3_memory_governor import (
    GovernorDecision,
    compute_worktree_cap,
    governor_enabled,
)


def test_ram_is_the_binding_constraint():
    # 4500MB available, 1500MB/worktree -> ram_cap=3; level_cap=8 -> allow 3
    d = compute_worktree_cap(
        requested=8, avail_mb=4500.0, budget_mb=1500, level_cap=8,
    )
    assert isinstance(d, GovernorDecision)
    assert d.ram_cap == 3
    assert d.n_allowed == 3
    assert d.disposition == "clamp"


def test_level_cap_is_the_binding_constraint():
    # 12000MB -> ram_cap=8; but level_cap=3 (HIGH) -> allow 3, strictest wins
    d = compute_worktree_cap(
        requested=8, avail_mb=12000.0, budget_mb=1500, level_cap=3,
    )
    assert d.ram_cap == 8
    assert d.n_allowed == 3
    assert d.disposition == "clamp"


def test_floor_never_below_one():
    # Only 800MB available, 1500MB budget -> floor would be 0; clamp to >=1
    d = compute_worktree_cap(
        requested=4, avail_mb=800.0, budget_mb=1500, level_cap=8,
    )
    assert d.ram_cap == 1
    assert d.n_allowed == 1


def test_no_clamp_when_everything_fits():
    d = compute_worktree_cap(
        requested=2, avail_mb=16000.0, budget_mb=1500, level_cap=8,
    )
    assert d.n_allowed == 2
    assert d.disposition == "allow"


def test_requested_zero_grants_zero_and_does_not_clamp():
    # A degenerate request (caller asked for nothing) yields 0 and is NOT
    # reported as a clamp, since nothing was withheld.
    d = compute_worktree_cap(
        requested=0, avail_mb=16000.0, budget_mb=1500, level_cap=8,
    )
    assert d.n_allowed == 0
    assert d.disposition == "allow"


def test_nonpositive_avail_fails_safe_to_one():
    # A garbage probe reading (<= 0) must not yield 0/negative worktrees;
    # the floor guard clamps ram_cap to 1 (conservative fail-safe).
    d = compute_worktree_cap(
        requested=4, avail_mb=0.0, budget_mb=1500, level_cap=8,
    )
    assert d.ram_cap == 1
    assert d.n_allowed == 1
    assert d.disposition == "clamp"

    d_neg = compute_worktree_cap(
        requested=4, avail_mb=-500.0, budget_mb=1500, level_cap=8,
    )
    assert d_neg.ram_cap == 1
    assert d_neg.n_allowed == 1


def test_no_clamp_branch_populates_all_fields():
    # 16000/1500 = 10 -> ram_cap=10; level_cap=8; requested=2 -> allow 2.
    d = compute_worktree_cap(
        requested=2, avail_mb=16000.0, budget_mb=1500, level_cap=8,
    )
    assert d.ram_cap == 10
    assert d.level_cap == 8
    assert d.requested == 2
    assert d.n_allowed == 2
    assert d.disposition == "allow"


def test_governor_enabled_default_true_and_explicit_false(monkeypatch):
    monkeypatch.delenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", raising=False)
    assert governor_enabled() is True
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "false")
    assert governor_enabled() is False
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "true")
    assert governor_enabled() is True
