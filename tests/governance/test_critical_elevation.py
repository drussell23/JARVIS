"""G3 — CRITICAL_ELEVATION + the Immutable Orange Protocol (Sovereign Law).

Load-bearing proof: for prime/reactor targets the resolved floor is
``approval_required`` under EVERY combination of {critical_elevation flag
on/off, graduated True/False, trust high/low, env overrides} — the
un-disableable Sovereign Law (Mind/Nerves can NEVER auto-merge).

For jarvis (Body): not-graduated -> critical_elevation; graduated -> None
(may auto-merge). Non-crossing -> None. Fail-CLOSED on error. Plus the
risk_tier_floor composition seam.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance import critical_elevation as ce
from backend.core.ouroboros.governance import cross_repo_trust_ledger as ctl
from backend.core.ouroboros.governance import risk_tier_floor as rtf


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    path = tmp_path / "cross_repo_trust.jsonl"
    monkeypatch.setenv("JARVIS_CROSS_REPO_TRUST_PATH", str(path))
    ctl.reset_cross_repo_trust_ledger()
    for k in (
        "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED",
        "JARVIS_TRUST_BASE", "JARVIS_TRUST_MIN_STREAK",
        "JARVIS_TRUST_MIN_COMPLEXITY",
        "JARVIS_MIN_RISK_TIER", "JARVIS_PARANOIA_MODE",
        "JARVIS_AUTO_APPLY_QUIET_HOURS",
    ):
        monkeypatch.delenv(k, raising=False)
    yield
    ctl.reset_cross_repo_trust_ledger()


def _graduate(repo: str, monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_BASE", "1.0")
    monkeypatch.setenv("JARVIS_TRUST_MIN_STREAK", "2")
    led = ctl.get_cross_repo_trust_ledger()
    led.record_outcome(
        repo=repo, pr_id="g1", outcome="clean_merge", complexity=1.0,
    )
    led.record_outcome(
        repo=repo, pr_id="g2", outcome="clean_merge", complexity=1.0,
    )
    assert led.is_graduated(repo)


# ---------------------------------------------------------------------------
# IMMUTABLE ORANGE — the un-disableable Sovereign Law
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_repo", ["prime", "reactor"])
@pytest.mark.parametrize("flag_value", ["true", "false", "", "1", "0"])
@pytest.mark.parametrize("graduate", [True, False])
def test_immutable_orange_never_below_approval_required(
    target_repo, flag_value, graduate, monkeypatch,
):
    """Mind (prime) + Nerves (reactor): ALWAYS approval_required under every
    flag / graduation / trust combination. Un-disableable."""
    monkeypatch.setenv(
        "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED", flag_value,
    )
    if graduate:
        _graduate(target_repo, monkeypatch)
    floor = ce.cross_repo_elevation_floor(
        target_repo=target_repo, crosses_repo=True,
    )
    assert floor == "approval_required", (
        f"{target_repo} resolved to {floor!r} — Immutable Orange broken"
    )


def test_immutable_orange_with_min_tier_safe_auto_override(monkeypatch):
    """Even an operator trying to FORCE safe_auto cannot drop prime below
    approval_required (no env reads the immutable floor)."""
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "safe_auto")
    monkeypatch.setenv(
        "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED", "false",
    )
    _graduate("prime", monkeypatch)
    floor = ce.cross_repo_elevation_floor(
        target_repo="prime", crosses_repo=True,
    )
    assert floor == "approval_required"


def test_immutable_orange_flag_off_still_law(monkeypatch):
    """The master flag governs ONLY the jarvis hard-halt — prime/reactor
    stay >= approval_required even with the flag off."""
    monkeypatch.setenv(
        "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED", "false",
    )
    assert not ce.is_critical_elevation_enabled()
    for repo in ("prime", "reactor"):
        assert ce.cross_repo_elevation_floor(
            target_repo=repo, crosses_repo=True,
        ) == "approval_required"


# ---------------------------------------------------------------------------
# jarvis (Body) — CRITICAL_ELEVATION until graduated
# ---------------------------------------------------------------------------


def test_jarvis_not_graduated_critical_elevation(monkeypatch):
    floor = ce.cross_repo_elevation_floor(
        target_repo="jarvis", crosses_repo=True,
    )
    assert floor == "critical_elevation"


def test_jarvis_graduated_returns_none(monkeypatch):
    _graduate("jarvis", monkeypatch)
    floor = ce.cross_repo_elevation_floor(
        target_repo="jarvis", crosses_repo=True,
    )
    assert floor is None


def test_jarvis_flag_off_no_hard_halt(monkeypatch):
    """With the master flag off, the jarvis CRITICAL_ELEVATION hard-halt is
    NOT applied (the flag governs only the jarvis halt)."""
    monkeypatch.setenv(
        "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED", "false",
    )
    floor = ce.cross_repo_elevation_floor(
        target_repo="jarvis", crosses_repo=True,
    )
    assert floor is None


# ---------------------------------------------------------------------------
# non-crossing / fail-closed
# ---------------------------------------------------------------------------


def test_non_crossing_jarvis_returns_none():
    assert ce.cross_repo_elevation_floor(
        target_repo="jarvis", crosses_repo=False,
    ) is None


def test_immutable_orange_dominates_even_non_crossing():
    """Targeting prime/reactor is itself a cross into the Mind/Nerves — the
    Sovereign Law dominates and applies regardless of the crosses_repo
    hint (the law is evaluated FIRST, un-bypassable)."""
    assert ce.cross_repo_elevation_floor(
        target_repo="prime", crosses_repo=False,
    ) == "approval_required"
    assert ce.cross_repo_elevation_floor(
        target_repo="reactor", crosses_repo=False,
    ) == "approval_required"


def test_fail_closed_jarvis_critical_elevation(monkeypatch):
    """An error resolving graduation -> most restrictive for jarvis."""
    def _boom(repo):
        raise RuntimeError("ledger exploded")

    led = ctl.get_cross_repo_trust_ledger()
    monkeypatch.setattr(led, "is_graduated", _boom)
    floor = ce.cross_repo_elevation_floor(
        target_repo="jarvis", crosses_repo=True,
    )
    assert floor == "critical_elevation"


def test_fail_closed_prime_approval_required(monkeypatch):
    """Even on error, prime stays approval_required (the law dominates and
    is evaluated before any ledger lookup)."""
    def _boom(repo):
        raise RuntimeError("ledger exploded")

    led = ctl.get_cross_repo_trust_ledger()
    monkeypatch.setattr(led, "is_graduated", _boom)
    floor = ce.cross_repo_elevation_floor(
        target_repo="prime", crosses_repo=True,
    )
    assert floor == "approval_required"


def test_unknown_repo_fail_closed_critical_elevation(monkeypatch):
    """An unrecognised target repo that crosses a boundary is treated as
    the body-class hard-halt (fail-CLOSED, never relax)."""
    floor = ce.cross_repo_elevation_floor(
        target_repo="mystery", crosses_repo=True,
    )
    assert floor in ("critical_elevation", "approval_required")
    assert floor is not None


def test_garbage_flag_value_fails_closed_for_jarvis(monkeypatch):
    """JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED=GARBAGE/maybe/1.5 must
    NOT disable the jarvis hard-halt (fail-CLOSED: garbage -> enabled).
    Only explicit falsy tokens (0/false/no/off) disable the jarvis hard-halt.
    prime/reactor are unaffected (Immutable Orange never reads this flag)."""
    for garbage_value in ("GARBAGE", "maybe", "1.5", "TRUE_MAYBE", "nope"):
        monkeypatch.setenv(
            "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED", garbage_value,
        )
        # is_critical_elevation_enabled must be True for garbage values.
        assert ce.is_critical_elevation_enabled(), (
            f"garbage flag value {garbage_value!r} silently disabled the "
            "jarvis hard-halt (fails OPEN) — must fail-CLOSED (enabled)"
        )
        # The jarvis hard-halt must still fire (not graduated -> critical_elevation).
        floor = ce.cross_repo_elevation_floor(
            target_repo="jarvis", crosses_repo=True,
        )
        assert floor == "critical_elevation", (
            f"garbage flag {garbage_value!r}: jarvis floor={floor!r}, "
            "expected critical_elevation"
        )
        # prime/reactor: Immutable Orange stays approval_required regardless.
        for repo in ("prime", "reactor"):
            assert ce.cross_repo_elevation_floor(
                target_repo=repo, crosses_repo=True,
            ) == "approval_required", (
                f"Immutable Orange broken for {repo!r} with flag={garbage_value!r}"
            )

    # Confirm only explicit falsy tokens actually disable the jarvis halt.
    for falsy_value in ("0", "false", "no", "off", "False", "OFF", "NO"):
        monkeypatch.setenv(
            "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED", falsy_value,
        )
        assert not ce.is_critical_elevation_enabled(), (
            f"explicit falsy {falsy_value!r} should disable the jarvis halt"
        )
        # Even with the jarvis halt disabled, prime/reactor stay Orange.
        for repo in ("prime", "reactor"):
            assert ce.cross_repo_elevation_floor(
                target_repo=repo, crosses_repo=True,
            ) == "approval_required", (
                f"Immutable Orange broken for {repo!r} with flag={falsy_value!r}"
            )


def test_record_cross_repo_outcome_convenience(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_BASE", "1.0")
    monkeypatch.setenv("JARVIS_TRUST_MIN_STREAK", "1")
    ce.record_cross_repo_outcome(
        repo="jarvis", pr_id="p1", outcome="clean_merge", complexity=2.0,
    )
    led = ctl.get_cross_repo_trust_ledger()
    assert led.trust_state("jarvis").trust == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# risk_tier_floor composition seam
# ---------------------------------------------------------------------------


def test_critical_elevation_ordered_above_approval_required():
    order = rtf.get_active_tier_order()
    assert "critical_elevation" in order
    assert order["critical_elevation"] > order["approval_required"]
    assert order["critical_elevation"] < order["blocked"]


def test_existing_single_repo_callers_byte_identical(monkeypatch):
    """No target_repo / crosses_repo context -> no cross-repo floor.
    recommended_floor is byte-identical to before (None when nothing else
    fires)."""
    assert rtf.recommended_floor() is None
    # Existing signal_source path still works untouched.
    assert rtf.recommended_floor(signal_source="") is None


def test_cross_repo_prime_target_floors_to_approval_required(monkeypatch):
    """A cross-repo prime op composed into recommended_floor -> at least
    approval_required (the immutable law)."""
    floor = rtf.recommended_floor(
        target_repo="prime", crosses_repo=True,
    )
    assert floor == "approval_required"


def test_cross_repo_jarvis_target_floors_to_critical_elevation(monkeypatch):
    floor = rtf.recommended_floor(
        target_repo="jarvis", crosses_repo=True,
    )
    assert floor == "critical_elevation"


def test_cross_repo_composes_strictest_wins(monkeypatch):
    """critical_elevation (jarvis) beats a notify_apply paranoia floor."""
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    floor = rtf.recommended_floor(
        target_repo="jarvis", crosses_repo=True,
    )
    assert floor == "critical_elevation"


def test_apply_floor_to_name_cross_repo_prime(monkeypatch):
    effective, applied = rtf.apply_floor_to_name(
        "safe_auto", target_repo="prime", crosses_repo=True,
    )
    assert effective == "approval_required"
    assert applied == "approval_required"


def test_graduated_jarvis_no_cross_repo_floor(monkeypatch):
    _graduate("jarvis", monkeypatch)
    floor = rtf.recommended_floor(
        target_repo="jarvis", crosses_repo=True,
    )
    # Graduated Body -> no cross-repo floor (falls to normal flow).
    assert floor is None
