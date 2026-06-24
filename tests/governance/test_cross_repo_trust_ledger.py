"""G3 — Adaptive Trust Ledger tests (Sovereign Cross-Repo Mutator).

Proves the streak + AST-complexity-weighted trust math:
  * clean_merge accrues streak + trust by complexity weight;
  * rollback OR fracture resets BOTH streak and trust to zero (consecutive,
    zero-rollback — ANY failure resets);
  * 100 trivial (complexity ~0.1) clean merges do NOT graduate, but a
    sufficient complexity-weighted streak DOES;
  * adaptive_threshold scales with the attempted complexity;
  * is_graduated fail-CLOSED (error -> False, never graduate on error);
  * min-streak floor (a single huge PR cannot graduate alone);
  * unique-PR dedup; durable JSONL round-trip.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance import cross_repo_trust_ledger as ctl


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """Isolate every test on its own JSONL + a clean env."""
    path = tmp_path / "cross_repo_trust.jsonl"
    monkeypatch.setenv("JARVIS_CROSS_REPO_TRUST_PATH", str(path))
    # Wipe any tuning env that might leak from the host.
    for k in (
        "JARVIS_TRUST_BASE",
        "JARVIS_TRUST_MIN_STREAK",
        "JARVIS_TRUST_W_DEPENDENTS",
        "JARVIS_TRUST_W_AST",
        "JARVIS_TRUST_W_DEPTH",
        "JARVIS_TRUST_W_CHARS",
    ):
        monkeypatch.delenv(k, raising=False)
    yield path


def _ledger():
    return ctl.CrossRepoTrustLedger()


# ---------------------------------------------------------------------------
# complexity_weight
# ---------------------------------------------------------------------------


def test_complexity_weight_pure_and_monotonic():
    trivial = ctl.complexity_weight(
        blast_dependents=0, ast_node_count=1, boundary_depth=0, body_chars=10,
    )
    deep = ctl.complexity_weight(
        blast_dependents=20, ast_node_count=500, boundary_depth=3,
        body_chars=8000,
    )
    assert trivial >= 0.0
    assert deep > trivial
    # Pure: same inputs -> same output.
    again = ctl.complexity_weight(
        blast_dependents=0, ast_node_count=1, boundary_depth=0, body_chars=10,
    )
    assert again == trivial


def test_complexity_weight_env_weighted(monkeypatch):
    base = ctl.complexity_weight(
        blast_dependents=10, ast_node_count=0, boundary_depth=0, body_chars=0,
    )
    monkeypatch.setenv("JARVIS_TRUST_W_DEPENDENTS", "100.0")
    boosted = ctl.complexity_weight(
        blast_dependents=10, ast_node_count=0, boundary_depth=0, body_chars=0,
    )
    assert boosted > base


# ---------------------------------------------------------------------------
# record_outcome — accrual / reset
# ---------------------------------------------------------------------------


def test_clean_merge_accrues_streak_and_trust():
    led = _ledger()
    led.record_outcome(
        repo="jarvis", pr_id="pr-1", outcome="clean_merge", complexity=2.0,
    )
    st = led.trust_state("jarvis")
    assert st.streak == 1
    assert st.trust == pytest.approx(2.0)
    led.record_outcome(
        repo="jarvis", pr_id="pr-2", outcome="clean_merge", complexity=3.0,
    )
    st = led.trust_state("jarvis")
    assert st.streak == 2
    assert st.trust == pytest.approx(5.0)


@pytest.mark.parametrize("failure", ["rollback", "fracture"])
def test_failure_resets_streak_and_trust_to_zero(failure):
    led = _ledger()
    led.record_outcome(
        repo="jarvis", pr_id="pr-1", outcome="clean_merge", complexity=5.0,
    )
    led.record_outcome(
        repo="jarvis", pr_id="pr-2", outcome="clean_merge", complexity=5.0,
    )
    assert led.trust_state("jarvis").trust == pytest.approx(10.0)
    led.record_outcome(
        repo="jarvis", pr_id="pr-3", outcome=failure, complexity=5.0,
    )
    st = led.trust_state("jarvis")
    assert st.streak == 0
    assert st.trust == 0.0
    assert st.graduated is False


def test_must_re_earn_after_failure():
    led = _ledger()
    for i in range(3):
        led.record_outcome(
            repo="jarvis", pr_id=f"pr-{i}", outcome="clean_merge",
            complexity=5.0,
        )
    led.record_outcome(
        repo="jarvis", pr_id="boom", outcome="rollback", complexity=1.0,
    )
    # Trust wiped; must rebuild from scratch.
    led.record_outcome(
        repo="jarvis", pr_id="again-1", outcome="clean_merge", complexity=2.0,
    )
    assert led.trust_state("jarvis").trust == pytest.approx(2.0)
    assert led.trust_state("jarvis").streak == 1


# ---------------------------------------------------------------------------
# unique-PR dedup
# ---------------------------------------------------------------------------


def test_unique_pr_dedup():
    led = _ledger()
    led.record_outcome(
        repo="jarvis", pr_id="dup", outcome="clean_merge", complexity=3.0,
    )
    led.record_outcome(
        repo="jarvis", pr_id="dup", outcome="clean_merge", complexity=3.0,
    )
    st = led.trust_state("jarvis")
    assert st.streak == 1
    assert st.trust == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# adaptive_threshold — scales with attempted complexity
# ---------------------------------------------------------------------------


def test_adaptive_threshold_scales_with_complexity(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_BASE", "3.0")
    led = _ledger()
    # No streak yet -> max(observed, 1.0) == 1.0 -> threshold == base.
    assert led.adaptive_threshold("jarvis") == pytest.approx(3.0)
    led.record_outcome(
        repo="jarvis", pr_id="big", outcome="clean_merge", complexity=10.0,
    )
    # Now the attempted complexity is 10 -> threshold == 3.0 * 10 == 30.
    assert led.adaptive_threshold("jarvis") == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# The load-bearing graduation proof
# ---------------------------------------------------------------------------


def test_100_trivial_merges_do_not_graduate(monkeypatch):
    # The load-bearing magic-N rejection: 100 trivial (complexity ~0.1)
    # clean merges accumulate trust 10.0, but the dynamic threshold floor
    # keeps the bar above raw count. With base high enough that 100x0.1
    # stays under threshold, graduation by COUNT alone is impossible.
    monkeypatch.setenv("JARVIS_TRUST_BASE", "15.0")
    monkeypatch.setenv("JARVIS_TRUST_MIN_STREAK", "2")
    led = _ledger()
    for i in range(100):
        led.record_outcome(
            repo="jarvis", pr_id=f"trivial-{i}", outcome="clean_merge",
            complexity=0.1,
        )
    st = led.trust_state("jarvis")
    # trust == 100 * 0.1 == 10.0; threshold == 15.0 * max(0.1, 1.0) == 15.0.
    # 10.0 < 15.0 -> never graduates regardless of the 100 count.
    assert st.trust == pytest.approx(10.0)
    assert not led.is_graduated("jarvis"), (
        "100 trivial PRs must not graduate via raw count"
    )


def test_truly_trivial_never_clears_threshold(monkeypatch):
    # complexity 0.01 * 100 == 1.0 < base 3.0 -> never graduates.
    monkeypatch.setenv("JARVIS_TRUST_BASE", "3.0")
    monkeypatch.setenv("JARVIS_TRUST_MIN_STREAK", "2")
    led = _ledger()
    for i in range(100):
        led.record_outcome(
            repo="jarvis", pr_id=f"t-{i}", outcome="clean_merge",
            complexity=0.01,
        )
    assert led.trust_state("jarvis").trust == pytest.approx(1.0)
    assert not led.is_graduated("jarvis")


def test_complexity_weighted_streak_graduates(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_BASE", "3.0")
    monkeypatch.setenv("JARVIS_TRUST_MIN_STREAK", "2")
    led = _ledger()
    # Two clean merges at complexity 5 each -> trust 10; max observed 5;
    # threshold 3.0 * 5 == 15 -> NOT yet.
    led.record_outcome(
        repo="jarvis", pr_id="a", outcome="clean_merge", complexity=5.0,
    )
    led.record_outcome(
        repo="jarvis", pr_id="b", outcome="clean_merge", complexity=5.0,
    )
    assert not led.is_graduated("jarvis")
    # A third clean merge at complexity 5 -> trust 15 >= 15 AND streak 3.
    led.record_outcome(
        repo="jarvis", pr_id="c", outcome="clean_merge", complexity=5.0,
    )
    assert led.is_graduated("jarvis")


def test_min_streak_floor_blocks_single_huge_pr(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_BASE", "3.0")
    monkeypatch.setenv("JARVIS_TRUST_MIN_STREAK", "2")
    led = _ledger()
    # One enormous PR: trust 100; threshold 3.0 * 100 == 300 -> trust bar
    # NOT cleared anyway, but also streak == 1 < 2.
    led.record_outcome(
        repo="jarvis", pr_id="huge", outcome="clean_merge", complexity=100.0,
    )
    assert led.trust_state("jarvis").streak == 1
    assert not led.is_graduated("jarvis")


def test_is_graduated_reflected_in_state(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_BASE", "1.0")
    monkeypatch.setenv("JARVIS_TRUST_MIN_STREAK", "2")
    led = _ledger()
    led.record_outcome(
        repo="jarvis", pr_id="a", outcome="clean_merge", complexity=1.0,
    )
    led.record_outcome(
        repo="jarvis", pr_id="b", outcome="clean_merge", complexity=1.0,
    )
    # trust 2; threshold 1.0 * max(1.0,1.0) == 1.0; streak 2 -> graduated.
    st = led.trust_state("jarvis")
    assert st.graduated is True
    assert led.is_graduated("jarvis")


# ---------------------------------------------------------------------------
# fail-CLOSED
# ---------------------------------------------------------------------------


def test_is_graduated_fail_closed_on_error(monkeypatch):
    led = _ledger()

    def _boom(repo):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(led, "trust_state", _boom)
    assert led.is_graduated("jarvis") is False


# ---------------------------------------------------------------------------
# durable round-trip
# ---------------------------------------------------------------------------


def test_durable_round_trip(_clean_env):
    led1 = _ledger()
    led1.record_outcome(
        repo="jarvis", pr_id="x", outcome="clean_merge", complexity=4.0,
    )
    led1.record_outcome(
        repo="jarvis", pr_id="y", outcome="clean_merge", complexity=2.0,
    )
    # Fresh instance reading the same path.
    led2 = _ledger()
    st = led2.trust_state("jarvis")
    assert st.streak == 2
    assert st.trust == pytest.approx(6.0)


def test_to_dict_shape():
    led = _ledger()
    led.record_outcome(
        repo="jarvis", pr_id="x", outcome="clean_merge", complexity=4.0,
    )
    d = led.trust_state("jarvis").to_dict()
    for key in ("repo", "streak", "trust", "threshold", "graduated",
                "last_complexity"):
        assert key in d


def test_per_repo_isolation():
    led = _ledger()
    led.record_outcome(
        repo="jarvis", pr_id="j1", outcome="clean_merge", complexity=5.0,
    )
    led.record_outcome(
        repo="prime", pr_id="p1", outcome="clean_merge", complexity=2.0,
    )
    assert led.trust_state("jarvis").trust == pytest.approx(5.0)
    assert led.trust_state("prime").trust == pytest.approx(2.0)


def test_singleton():
    a = ctl.get_cross_repo_trust_ledger()
    b = ctl.get_cross_repo_trust_ledger()
    assert a is b
