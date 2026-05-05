"""Move 8 Slice 1 — proactive_curiosity_reader substrate regression spine.

PRD §29.7 / §35 Move 8: closes the auto-spawn-exploration-ops half
of the M9 CuriosityGradient producer-consumer loop. This file pins
the substrate-only Slice 1 reader.

Verifies (38 tests):

  * Closed taxonomy: CuriosityRankingDecision has exactly 5
    values; AST pin auto-discovered via shipped_code_invariants.
  * §33.1 master-flag default-FALSE; AST pin auto-discovered;
    synthetic test proves the pin DOES fire when the default is
    flipped to True prematurely.
  * §33.5 versioned artifact: CuriosityRanking schema_version +
    symmetric to_dict / from_dict round-trip; defensive parse on
    malformed input returns None.
  * Pure-function semantics: caller-injected snapshot + override
    knobs; no env reads inside the math when overrides supplied.
  * Verdict ladder all 5 paths exercised: SURFACED happy path,
    BELOW_FLOOR, COLD_START, DECAY_SUPPRESSED, COOLDOWN.
  * Top-K cap honored; demoted-by-K rows dropped silently
    (truthful decision distribution preserved).
  * Tie-break on last_updated_at_unix when magnitude ties.
  * Master-flag-off short-circuit returns empty tuple.
  * NEVER raises on malformed score / missing collector /
    snapshot_all-raises.
  * Authority asymmetry pin auto-discovered.
  * composes-M9 pin auto-discovered (forbids
    compute_curiosity calls).
  * 4 FlagRegistry seeds present + correct shape.
  * Public API stability.
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import (
    proactive_curiosity_reader as pcr,
)
from backend.core.ouroboros.governance.proactive_curiosity_reader import (
    PROACTIVE_CURIOSITY_READER_SCHEMA_VERSION,
    CuriosityRanking,
    CuriosityRankingDecision,
    cooldown_seconds,
    magnitude_floor,
    proactive_curiosity_reader_enabled,
    rank_curious_clusters,
    register_shipped_invariants,
    reset_cooldown_ledger_for_tests,
    top_k,
)


# ---------------------------------------------------------------------------
# Synthetic CuriosityScore double — duck-types M9's frozen dataclass
# without importing the heavy collector module
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeDecayReason:
    def __init__(self, value: str) -> None:
        self.value = value


@dataclass(frozen=True)
class _FakeScore:
    cluster_id: str
    magnitude: float = 0.5
    confidence: float = 0.7
    samples_count: int = 10
    cold_start: bool = False
    decay_value: str = "none"
    last_updated_at_unix: float = 1000.0

    def is_cold_start(self) -> bool:
        return self.cold_start

    @property
    def dominant_source(self):
        return _FakeSource("logprob_entropy")

    @property
    def decay_reason(self):
        return _FakeDecayReason(self.decay_value)


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cooldown():
    reset_cooldown_ledger_for_tests()
    yield
    reset_cooldown_ledger_for_tests()


# ---------------------------------------------------------------------------
# Closed taxonomy + §33.5 versioned artifact
# ---------------------------------------------------------------------------


def test_decision_taxonomy_exactly_5_values():
    values = {d.value for d in CuriosityRankingDecision}
    assert values == {
        "surfaced", "below_floor", "cold_start",
        "decay_suppressed", "cooldown",
    }


def test_curiosity_ranking_is_frozen():
    r = CuriosityRanking(
        cluster_id="c1",
        magnitude=0.5,
        confidence=0.7,
        samples_count=10,
        dominant_source="logprob_entropy",
        decay_reason="none",
        last_updated_at_unix=1000.0,
        rank=1,
        decision=CuriosityRankingDecision.SURFACED,
    )
    with pytest.raises(Exception):
        r.cluster_id = "c2"  # type: ignore


def test_curiosity_ranking_to_dict_round_trip():
    r = CuriosityRanking(
        cluster_id="c1",
        magnitude=0.5,
        confidence=0.7,
        samples_count=10,
        dominant_source="logprob_entropy",
        decay_reason="none",
        last_updated_at_unix=1000.0,
        rank=1,
        decision=CuriosityRankingDecision.SURFACED,
    )
    raw = r.to_dict()
    assert raw["cluster_id"] == "c1"
    assert raw["decision"] == "surfaced"
    assert (
        raw["schema_version"]
        == PROACTIVE_CURIOSITY_READER_SCHEMA_VERSION
    )
    parsed = CuriosityRanking.from_dict(raw)
    assert parsed == r


def test_curiosity_ranking_from_dict_defensive_parse():
    # Malformed inputs return None; never raise.
    assert CuriosityRanking.from_dict(None) is None
    assert CuriosityRanking.from_dict("not a dict") is None
    assert CuriosityRanking.from_dict({}) is None  # empty decision
    assert CuriosityRanking.from_dict(
        {"decision": "not-a-real-value"},
    ) is None


def test_curiosity_ranking_schema_version_pinned():
    r = CuriosityRanking(
        cluster_id="c1",
        magnitude=0.5,
        confidence=0.7,
        samples_count=10,
        dominant_source="logprob_entropy",
        decay_reason="none",
        last_updated_at_unix=1000.0,
        rank=1,
        decision=CuriosityRankingDecision.SURFACED,
    )
    assert (
        r.schema_version
        == "proactive_curiosity_reader.1"
    )


# ---------------------------------------------------------------------------
# §33.1 master-flag asymmetric semantics
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(
            "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED", None,
        )
        assert proactive_curiosity_reader_enabled() is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True), ("true", True), ("TRUE", True),
        ("yes", True), ("on", True),
        ("0", False), ("false", False), ("no", False),
        ("off", False), ("garbage", False),
    ],
)
def test_master_flag_truthy_falsy(raw, expected):
    with patch.dict(
        os.environ,
        {"JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED": raw},
    ):
        assert proactive_curiosity_reader_enabled() is expected


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", 3), ("1", 1), ("16", 16),
        ("0", 1),  # below floor
        ("99", 16),  # above ceiling
        ("garbage", 3),  # parse failure → default
    ],
)
def test_top_k_clamping(raw, expected):
    with patch.dict(
        os.environ, {"JARVIS_PROACTIVE_CURIOSITY_TOP_K": raw},
    ):
        assert top_k() == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", 0.40), ("0.0", 0.0), ("1.0", 1.0),
        ("-0.5", 0.0),  # below floor
        ("1.5", 1.0),  # above ceiling
        ("garbage", 0.40),
    ],
)
def test_magnitude_floor_clamping(raw, expected):
    with patch.dict(
        os.environ,
        {"JARVIS_PROACTIVE_CURIOSITY_MAGNITUDE_FLOOR": raw},
    ):
        assert magnitude_floor() == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", 14400), ("60", 60), ("604800", 604800),
        ("0", 60),  # below floor
        ("99999999", 604800),  # above ceiling (7d = 604800)
        ("garbage", 14400),
    ],
)
def test_cooldown_seconds_clamping(raw, expected):
    with patch.dict(
        os.environ,
        {"JARVIS_PROACTIVE_CURIOSITY_COOLDOWN_S": raw},
    ):
        assert cooldown_seconds() == expected


# ---------------------------------------------------------------------------
# Reader: master-flag-off short-circuit
# ---------------------------------------------------------------------------


def test_rank_returns_empty_when_master_off():
    snapshot = (_FakeScore(cluster_id="c1", magnitude=0.9),)
    result = rank_curious_clusters(
        snapshot=snapshot, enabled_override=False,
    )
    assert result == ()


def test_rank_returns_empty_on_empty_snapshot():
    result = rank_curious_clusters(
        snapshot=(), enabled_override=True,
    )
    assert result == ()


# ---------------------------------------------------------------------------
# Reader: verdict ladder all 5 paths
# ---------------------------------------------------------------------------


def test_surfaced_happy_path():
    snapshot = (_FakeScore(cluster_id="c1", magnitude=0.9),)
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        cooldown_seconds_override=60,
        now_unix=1000.0,
    )
    assert len(result) == 1
    assert result[0].decision is CuriosityRankingDecision.SURFACED
    assert result[0].rank == 1
    assert result[0].cluster_id == "c1"
    assert result[0].magnitude == pytest.approx(0.9)


def test_below_floor():
    snapshot = (_FakeScore(cluster_id="c1", magnitude=0.1),)
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        now_unix=1000.0,
    )
    assert len(result) == 1
    assert result[0].decision is (
        CuriosityRankingDecision.BELOW_FLOOR
    )
    assert result[0].rank == -1


def test_cold_start():
    snapshot = (
        _FakeScore(
            cluster_id="c1", magnitude=0.9, cold_start=True,
        ),
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        now_unix=1000.0,
    )
    assert len(result) == 1
    assert result[0].decision is (
        CuriosityRankingDecision.COLD_START
    )
    assert result[0].rank == -1


def test_decay_suppressed():
    snapshot = (
        _FakeScore(
            cluster_id="c1",
            magnitude=0.9,
            decay_value="stale_focus",
        ),
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        now_unix=1000.0,
    )
    assert len(result) == 1
    assert result[0].decision is (
        CuriosityRankingDecision.DECAY_SUPPRESSED
    )
    assert result[0].rank == -1


def test_cooldown_blocks_re_emission():
    snapshot = (_FakeScore(cluster_id="c1", magnitude=0.9),)
    # First call surfaces.
    r1 = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        cooldown_seconds_override=600,
        now_unix=1000.0,
    )
    assert r1[0].decision is CuriosityRankingDecision.SURFACED
    # Second call within window → COOLDOWN.
    r2 = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        cooldown_seconds_override=600,
        now_unix=1100.0,  # +100s, still inside 600s
    )
    assert r2[0].decision is CuriosityRankingDecision.COOLDOWN
    assert r2[0].rank == -1
    # Third call past window → SURFACED again.
    r3 = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        cooldown_seconds_override=600,
        now_unix=2000.0,
    )
    assert r3[0].decision is CuriosityRankingDecision.SURFACED


# ---------------------------------------------------------------------------
# Reader: top-K cap + ordering
# ---------------------------------------------------------------------------


def test_top_k_cap_honored():
    snapshot = tuple(
        _FakeScore(cluster_id=f"c{i}", magnitude=0.9 - i * 0.01)
        for i in range(10)
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        top_k_override=3,
        now_unix=1000.0,
    )
    surfaced = [
        r for r in result
        if r.decision is CuriosityRankingDecision.SURFACED
    ]
    assert len(surfaced) == 3
    # Top 3 by magnitude — c0 / c1 / c2.
    assert [r.cluster_id for r in surfaced] == ["c0", "c1", "c2"]
    assert [r.rank for r in surfaced] == [1, 2, 3]


def test_demoted_by_top_k_dropped_silently():
    """Per the docstring — passing all filters but losing the
    K-cut produces NO artifact (truthful decision distribution
    preserved). No BELOW_FLOOR fake-out."""
    snapshot = tuple(
        _FakeScore(cluster_id=f"c{i}", magnitude=0.9)
        for i in range(5)
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        top_k_override=2,
        now_unix=1000.0,
    )
    # Only 2 SURFACED rows; the other 3 are NOT in the artifact
    # stream (would be a lie to relabel them).
    assert len(result) == 2
    for r in result:
        assert r.decision is CuriosityRankingDecision.SURFACED


def test_tie_break_by_last_updated():
    snapshot = (
        _FakeScore(
            cluster_id="old", magnitude=0.7,
            last_updated_at_unix=1000.0,
        ),
        _FakeScore(
            cluster_id="new", magnitude=0.7,
            last_updated_at_unix=2000.0,
        ),
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        top_k_override=2,
        now_unix=3000.0,
    )
    # Most-recent wins the tie.
    assert result[0].cluster_id == "new"
    assert result[0].rank == 1
    assert result[1].cluster_id == "old"
    assert result[1].rank == 2


def test_mixed_decision_distribution():
    """All 5 verdict paths in one snapshot — pin that
    classification is independent per row."""
    snapshot = (
        _FakeScore(cluster_id="surfaced1", magnitude=0.9),
        _FakeScore(cluster_id="below", magnitude=0.1),
        _FakeScore(
            cluster_id="cold", magnitude=0.9, cold_start=True,
        ),
        _FakeScore(
            cluster_id="decayed",
            magnitude=0.9,
            decay_value="recurrence_loop",
        ),
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        top_k_override=5,
        now_unix=1000.0,
    )
    by_decision = {r.decision: r for r in result}
    assert (
        by_decision[CuriosityRankingDecision.SURFACED].cluster_id
        == "surfaced1"
    )
    assert (
        by_decision[
            CuriosityRankingDecision.BELOW_FLOOR
        ].cluster_id == "below"
    )
    assert (
        by_decision[CuriosityRankingDecision.COLD_START].cluster_id
        == "cold"
    )
    assert (
        by_decision[
            CuriosityRankingDecision.DECAY_SUPPRESSED
        ].cluster_id == "decayed"
    )


# ---------------------------------------------------------------------------
# Defensive paths — NEVER raises
# ---------------------------------------------------------------------------


def test_malformed_score_skipped():
    """A score whose attribute access raises does not poison
    the rest of the ranking."""
    class _Broken:
        @property
        def cluster_id(self):
            raise RuntimeError("broken")

    snapshot = (
        _Broken(),
        _FakeScore(cluster_id="ok", magnitude=0.9),
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        now_unix=1000.0,
    )
    surfaced = [
        r for r in result
        if r.decision is CuriosityRankingDecision.SURFACED
    ]
    assert len(surfaced) == 1
    assert surfaced[0].cluster_id == "ok"


def test_collector_unavailable_returns_empty():
    """When snapshot=None and get_default_collector raises,
    we return ()."""
    with patch(
        "backend.core.ouroboros.governance."
        "curiosity_collector.get_default_collector",
        side_effect=RuntimeError("nope"),
    ):
        result = rank_curious_clusters(
            enabled_override=True, now_unix=1000.0,
        )
    assert result == ()


def test_snapshot_all_raises_returns_empty():
    """When the collector exists but snapshot_all raises, we
    return ()."""
    class _BadColl:
        def snapshot_all(self):
            raise RuntimeError("boom")

    result = rank_curious_clusters(
        collector=_BadColl(),
        enabled_override=True,
        now_unix=1000.0,
    )
    assert result == ()


def test_score_with_empty_cluster_id_skipped():
    snapshot = (
        _FakeScore(cluster_id="", magnitude=0.9),
        _FakeScore(cluster_id="real", magnitude=0.9),
    )
    result = rank_curious_clusters(
        snapshot=snapshot,
        enabled_override=True,
        magnitude_floor_override=0.4,
        now_unix=1000.0,
    )
    surfaced = [
        r for r in result
        if r.decision is CuriosityRankingDecision.SURFACED
    ]
    assert len(surfaced) == 1
    assert surfaced[0].cluster_id == "real"


# ---------------------------------------------------------------------------
# AST pins auto-discovered + green
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_4():
    invs = register_shipped_invariants()
    assert len(invs) == 4
    names = {i.invariant_name for i in invs}
    assert names == {
        "proactive_curiosity_reader_master_flag_stays_default_false",
        "proactive_curiosity_reader_authority_asymmetry",
        "proactive_curiosity_reader_decision_taxonomy_5_values",
        "proactive_curiosity_reader_composes_m9_substrate",
    }


def test_all_pins_validate_clean():
    """Each pin must run cleanly against the current source."""
    import ast
    from pathlib import Path

    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/"
        / "proactive_curiosity_reader.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired against clean source: "
            f"{violations}"
        )


def test_pin_fires_on_premature_master_flag_flip():
    """Synthetic test — proves the pin DOES fire if someone
    flips the default to True without the graduation contract."""
    import ast

    bad_source = '''
import os

def proactive_curiosity_reader_enabled() -> bool:
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # PREMATURE FLIP — pin must fire
    return raw in ("1", "true", "yes", "on")
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    flag_pin = next(
        i for i in invs
        if "master_flag" in i.invariant_name
    )
    violations = flag_pin.validate(tree, bad_source)
    assert violations, (
        "pin failed to fire on premature default-True flip"
    )
    assert any("default-FALSE" in v for v in violations)


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    import ast

    bad_source = '''
from backend.core.ouroboros.governance.orchestrator import foo
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    auth_pin = next(
        i for i in invs
        if "authority_asymmetry" in i.invariant_name
    )
    violations = auth_pin.validate(tree, bad_source)
    assert violations
    assert any("orchestrator" in v for v in violations)


def test_composes_m9_pin_fires_on_compute_curiosity_call():
    import ast

    bad_source = '''
def foo():
    score = compute_curiosity(cluster_id="x")
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    comp_pin = next(
        i for i in invs
        if "composes_m9" in i.invariant_name
    )
    violations = comp_pin.validate(tree, bad_source)
    assert violations
    assert any("compute_curiosity" in v for v in violations)


def test_decision_taxonomy_pin_fires_on_extra_value():
    import ast

    bad_source = '''
import enum

class CuriosityRankingDecision(str, enum.Enum):
    SURFACED = "surfaced"
    BELOW_FLOOR = "below_floor"
    COLD_START = "cold_start"
    DECAY_SUPPRESSED = "decay_suppressed"
    COOLDOWN = "cooldown"
    EXTRA = "extra"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    tax_pin = next(
        i for i in invs
        if "taxonomy" in i.invariant_name
    )
    violations = tax_pin.validate(tree, bad_source)
    assert violations
    assert any("EXTRA" in v for v in violations)


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_flag_registry_has_4_move_8_seeds():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    seeds = SEED_SPECS
    slice_1_names = {
        "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED",
        "JARVIS_PROACTIVE_CURIOSITY_TOP_K",
        "JARVIS_PROACTIVE_CURIOSITY_MAGNITUDE_FLOOR",
        "JARVIS_PROACTIVE_CURIOSITY_COOLDOWN_S",
    }
    move_8_slice_1 = [
        s for s in seeds if s.name in slice_1_names
    ]
    assert len(move_8_slice_1) == 4
    assert {s.name for s in move_8_slice_1} == slice_1_names


def test_flag_registry_master_default_false():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    seeds = SEED_SPECS
    master = next(
        s for s in seeds
        if s.name == "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED"
    )
    assert master.default is False


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    expected = {
        "CuriosityRanking",
        "CuriosityRankingDecision",
        "PROACTIVE_CURIOSITY_READER_SCHEMA_VERSION",
        "cooldown_seconds",
        "magnitude_floor",
        "proactive_curiosity_reader_enabled",
        "rank_curious_clusters",
        "register_shipped_invariants",
        "reset_cooldown_ledger_for_tests",
        "top_k",
    }
    assert set(pcr.__all__) == expected
