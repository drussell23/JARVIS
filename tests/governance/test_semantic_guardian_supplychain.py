"""Supply-chain & diff-entropy SemanticGuardian patterns.

Root cause under test (soak bt-2026-07-22-054947): a ``requirements.txt``
op that stripped 76 pinned dependencies rated SAFE_AUTO and promoted
onto the operator tree because the risk plane weighed file IDENTITY,
not diff PAYLOAD. These content-driven detectors revoke SAFE_AUTO on the
gutting signature.

Mandated edge cases:
1. Safely ADDING a single pinned dependency → stays SAFE_AUTO (no finding).
2. UNPINNING an existing dependency → NOTIFY_APPLY floor.
3. Massive line deletion on a mock config → the entropy cap escalates.
Plus the exact soak-payload regression (165 pins → 89 loose ranges).
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.semantic_guardian import (
    SemanticGuardian,
    recommend_tier_floor,
)


def _findings(path: str, old: str, new: str):
    return SemanticGuardian().inspect(
        file_path=path, old_content=old, new_content=new,
    )


def _patterns(findings):
    return {f.pattern for f in findings}


# ---------------------------------------------------------------------------
# Edge case 1 — adding a single pinned dep stays SAFE_AUTO
# ---------------------------------------------------------------------------


def test_adding_pinned_dependency_stays_safe_auto() -> None:
    old = "torch==2.5.1\nnumpy==1.26.4\nrequests==2.32.3\n"
    new = old + "rich==13.9.4\n"
    findings = _findings("requirements.txt", old, new)
    assert "dependency_pin_weakened" not in _patterns(findings)
    assert "high_entropy_gutting" not in _patterns(findings)
    # No supply-chain finding → no tier floor from these patterns.
    assert recommend_tier_floor(findings) is None


def test_pin_version_bump_stays_safe_auto() -> None:
    """A same-strength bump (==A → ==B) is not a weakening."""
    old = "torch==2.5.1\nnumpy==1.26.4\n"
    new = "torch==2.6.0\nnumpy==1.26.4\n"
    findings = _findings("requirements.txt", old, new)
    assert "dependency_pin_weakened" not in _patterns(findings)


# ---------------------------------------------------------------------------
# Edge case 2 — unpinning triggers NOTIFY_APPLY
# ---------------------------------------------------------------------------


def test_unpinning_dependency_triggers_notify_apply() -> None:
    old = "torch==2.5.1\nnumpy==1.26.4\nrequests==2.32.3\n"
    new = "torch>=2.5.1\nnumpy==1.26.4\nrequests==2.32.3\n"  # torch unpinned
    findings = _findings("requirements.txt", old, new)
    assert "dependency_pin_weakened" in _patterns(findings)
    hit = next(f for f in findings if f.pattern == "dependency_pin_weakened")
    assert hit.severity == "soft"
    assert "torch" in hit.message
    assert recommend_tier_floor(findings) == "notify_apply"


def test_removing_pinned_dependency_triggers_notify_apply() -> None:
    old = "torch==2.5.1\nnumpy==1.26.4\n"
    new = "numpy==1.26.4\n"  # torch pin removed entirely
    findings = _findings("requirements.txt", old, new)
    assert "dependency_pin_weakened" in _patterns(findings)
    assert recommend_tier_floor(findings) == "notify_apply"


def test_loosen_to_compatible_release_triggers_notify_apply() -> None:
    old = "cryptography==42.0.0\n"
    new = "cryptography~=42.0\n"
    assert "dependency_pin_weakened" in _patterns(
        _findings("requirements.txt", old, new),
    )


def test_non_dependency_file_ignored_by_pin_invariant() -> None:
    """The pin invariant is applicability-scoped (Gate 3 reuse), not a
    global rule — a .py file with ``==`` lines never fires it."""
    old = "x == 1\ny == 2\nz == 3\n"
    new = "y == 2\n"
    assert "dependency_pin_weakened" not in _patterns(
        _findings("backend/mod.py", old, new),
    )


# ---------------------------------------------------------------------------
# Edge case 3 — massive deletion trips the entropy cap
# ---------------------------------------------------------------------------


def test_massive_deletion_trips_entropy_cap() -> None:
    old = "\n".join(f"KEY_{i} = value_{i}" for i in range(100)) + "\n"
    new = "\n".join(f"KEY_{i} = value_{i}" for i in range(20)) + "\n"  # -80%
    findings = _findings("config/settings.ini", old, new)
    assert "high_entropy_gutting" in _patterns(findings)
    hit = next(f for f in findings if f.pattern == "high_entropy_gutting")
    assert hit.severity == "soft"
    assert recommend_tier_floor(findings) == "notify_apply"


def test_balanced_refactor_does_not_trip_entropy_cap() -> None:
    """Deletions ≈ insertions (a rewrite, not a gutting) → no finding."""
    old = "\n".join(f"old_line_{i}" for i in range(100)) + "\n"
    new = "\n".join(f"new_line_{i}" for i in range(100)) + "\n"
    assert "high_entropy_gutting" not in _patterns(
        _findings("backend/mod.py", old, new),
    )


def test_tiny_file_shrink_below_min_lines_ignored() -> None:
    old = "a\nb\nc\n"
    new = "a\n"
    assert "high_entropy_gutting" not in _patterns(
        _findings("small.txt", old, new),
    )


def test_entropy_threshold_env_tunable(monkeypatch) -> None:
    old = "\n".join(str(i) for i in range(100)) + "\n"
    new = "\n".join(str(i) for i in range(70)) + "\n"  # -30%
    # Default 0.20 → fires.
    assert "high_entropy_gutting" in _patterns(
        _findings("c.cfg", old, new),
    )
    # Raise the cap above the observed ratio → no longer fires.
    monkeypatch.setenv("JARVIS_SEMGUARD_ENTROPY_MAX_DESTRUCTION", "0.50")
    assert "high_entropy_gutting" not in _patterns(
        _findings("c.cfg", old, new),
    )


# ---------------------------------------------------------------------------
# The exact soak payload — 165 pins → 89 loose ranges
# ---------------------------------------------------------------------------


def test_soak_lockfile_gutting_payload_no_longer_safe_auto() -> None:
    """The bt-2026-07-22-054947 shape: a large pinned lockfile rewritten
    to fewer loose ranges. BOTH detectors fire; SAFE_AUTO is revoked."""
    old = "\n".join(f"pkg{i}=={i}.0.0" for i in range(165)) + "\n"
    new = "\n".join(f"pkg{i}>=0" for i in range(89)) + "\n"
    findings = _findings("requirements.txt", old, new)
    pats = _patterns(findings)
    assert "dependency_pin_weakened" in pats
    assert "high_entropy_gutting" in pats
    # The op that promoted onto the operator tree would now be floored.
    assert recommend_tier_floor(findings) == "notify_apply"


def test_per_pattern_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SEMGUARD_DEPENDENCY_PIN_WEAKENED_ENABLED", "0")
    old = "torch==2.5.1\n"
    new = "torch>=2.5.1\n"
    assert "dependency_pin_weakened" not in _patterns(
        _findings("requirements.txt", old, new),
    )
