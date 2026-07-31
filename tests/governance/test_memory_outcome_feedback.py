"""Regression spine for the outcome-feedback loop (memory_utility).

Selection was open-loop: a topic in forty verified ops and one in twelve
failed ops ranked identically forever. These pin the closed loop's MATH and,
more importantly, its refusals — the cases where it must decline to learn.

The invariants that matter most, because each one is a way a naive
implementation silently corrupts the corpus:

* cold start is neutral, never negative (absence of evidence is not a verdict)
* neutral is the DECAYED CORPUS MEAN, so a good or bad week cannot re-rank
  everything together
* a zero-total VERIFY proves nothing and must not be credited as a pass
* repo-wide health is not this op's result (false attribution, purest form)
* near-duplicate propagation is NEGATIVE-only and similarity-scaled
* an edited topic starts over, because its hash is its identity
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import memory_utility as mu
from backend.core.ouroboros.governance.memory_utility import (
    Observation,
    UtilityStore,
    observe_op_outcome,
    reading_for,
    utility_for,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    """Bind the store to a tmp root so no test touches the real ledger."""
    monkeypatch.delenv("JARVIS_MEMORY_UTILITY_ENABLED", raising=False)
    mu.reset_for_tests(tmp_path)
    yield
    mu.reset_for_tests(None)


def _store(tmp_path: Path) -> UtilityStore:
    return UtilityStore(tmp_path / ".jarvis" / "memory_utility.jsonl")


def _obs(h: str, w: float, *, age_days: float = 0.0,
         credit: float = 1.0) -> Observation:
    return Observation(content_hash=h, weight=w,
                       at=time.time() - age_days * 86400.0, credit=credit)


# ---------------------------------------------------------------------------
# Cold start and refusals
# ---------------------------------------------------------------------------


def test_cold_start_is_exactly_neutral() -> None:
    reading = reading_for("never-seen")
    assert reading.multiplier == 1.0
    assert reading.cold is True
    assert reading.polarity is None


def test_disabled_is_a_byte_identical_rollback(monkeypatch, tmp_path) -> None:
    store = _store(tmp_path)
    store.add([_obs("a", 0.0) for _ in range(20)])
    monkeypatch.setenv("JARVIS_MEMORY_UTILITY_ENABLED", "0")
    assert store.reading("a").multiplier == 1.0


def test_single_hash_corpus_cannot_move_itself(tmp_path: Path) -> None:
    """With one topic, it IS the corpus mean — divergence is zero by identity.

    A hardcoded midpoint would score this topic as terrible (polarity 0.0 vs
    a constant 0.5) purely because it is the only thing measured.
    """
    store = _store(tmp_path)
    store.add([_obs("solo", 0.0) for _ in range(10)])
    assert store.reading("solo").multiplier == pytest.approx(1.0)


def test_zero_total_verify_is_not_a_pass() -> None:
    """A run that proved nothing must not be credited as success."""
    assert observe_op_outcome("op", passed=0, total=0,
                              admitted_hashes=["h"]) == 0
    assert reading_for("h").cold is True


def test_repo_wide_health_is_not_attributed_to_this_op() -> None:
    listener = mu._OutcomeListener()
    listener.on_verify_completed(op_id="op", passed=0, total=9,
                                 scoped_to_applied_op=False)
    assert mu.get_store().hashes() == ()


def test_op_that_routed_no_memory_records_nothing() -> None:
    from backend.core.ouroboros.governance.memory_admission import (
        reset_default_registry,
    )
    reset_default_registry()
    assert observe_op_outcome("never-routed", passed=5, total=5) == 0


# ---------------------------------------------------------------------------
# Mathematical correctness
# ---------------------------------------------------------------------------


def test_above_corpus_promotes_and_below_demotes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add([_obs("good", 1.0) for _ in range(8)]
              + [_obs("bad", 0.0) for _ in range(8)])
    good = store.reading("good")
    bad = store.reading("bad")
    assert good.multiplier > 1.0 > bad.multiplier
    # Symmetric around the corpus mean: equal mass, opposite polarity.
    assert (good.multiplier - 1.0) == pytest.approx(1.0 - bad.multiplier,
                                                    abs=1e-9)
    assert good.corpus_polarity == pytest.approx(0.5)


def test_a_good_week_does_not_rerank_the_corpus(tmp_path: Path) -> None:
    """The reason neutral is the corpus mean rather than a constant.

    Every topic succeeding equally means no topic outperformed any other, so
    nothing should move. Against a fixed midpoint they would ALL promote.
    """
    store = _store(tmp_path)
    store.add([_obs(h, 1.0) for h in ("a", "b", "c") for _ in range(5)])
    for h in ("a", "b", "c"):
        assert store.reading(h).multiplier == pytest.approx(1.0)


def test_confidence_saturates_so_one_outcome_barely_moves(
    tmp_path: Path,
) -> None:
    """The false-attribution guard: one coincidence must cost almost nothing."""
    one = _store(tmp_path / "one")
    one.add([_obs("t", 1.0), _obs("other", 0.0)])
    many = _store(tmp_path / "many")
    many.add([_obs("t", 1.0) for _ in range(30)]
             + [_obs("other", 0.0) for _ in range(30)])
    assert one.reading("t").confidence < many.reading("t").confidence
    assert (one.reading("t").multiplier - 1.0) < \
        (many.reading("t").multiplier - 1.0)


def test_exponential_decay_lets_a_topic_recover(tmp_path: Path) -> None:
    """Legacy failures must not suppress a topic forever after a code fix."""
    hl = mu._halflife_days()
    fresh = _store(tmp_path / "fresh")
    fresh.add([_obs("t", 0.0), _obs("peer", 1.0)])
    stale = _store(tmp_path / "stale")
    stale.add([_obs("t", 0.0, age_days=hl * 8), _obs("peer", 1.0)])
    assert stale.reading("t").multiplier > fresh.reading("t").multiplier
    assert stale.reading("t").mass < fresh.reading("t").mass


def test_fully_decayed_history_returns_to_neutral(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add([_obs("ancient", 0.0, age_days=mu._halflife_days() * 400)])
    reading = store.reading("ancient")
    assert reading.multiplier == 1.0
    assert reading.mass == pytest.approx(0.0, abs=1e-6)
    # Observed, but with no usable mass — distinct from never-seen.
    assert reading.observations == 1


def test_multiplier_is_clamped_at_both_ends(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_UTILITY_GAIN", "2.0")
    monkeypatch.setenv("JARVIS_MEMORY_UTILITY_MIN", "0.5")
    monkeypatch.setenv("JARVIS_MEMORY_UTILITY_MAX", "1.2")
    store = _store(tmp_path)
    store.add([_obs("good", 1.0) for _ in range(60)]
              + [_obs("bad", 0.0) for _ in range(60)])
    assert store.reading("good").multiplier <= 1.2
    assert store.reading("bad").multiplier >= 0.5


def test_zero_gain_disables_the_effect_but_keeps_the_reading(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_UTILITY_GAIN", "0")
    store = _store(tmp_path)
    store.add([_obs("a", 1.0) for _ in range(9)] + [_obs("b", 0.0)])
    reading = store.reading("a")
    assert reading.multiplier == pytest.approx(1.0)
    assert reading.polarity == pytest.approx(1.0)  # still measured


def test_negative_credit_cannot_produce_negative_mass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add([_obs("t", 1.0, credit=-5.0)])
    assert store.reading("t").mass >= 0.0
    assert store.reading("t").multiplier == 1.0


# ---------------------------------------------------------------------------
# Identity and the join
# ---------------------------------------------------------------------------


def test_edited_topic_starts_neutral_because_the_hash_is_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.add([_obs("hash-v1", 0.0) for _ in range(20)]
              + [_obs("peer", 1.0) for _ in range(20)])
    assert store.reading("hash-v1").multiplier < 1.0
    # The topic is rewritten -> new payload -> new hash. The old evidence was
    # about words that are no longer in any prompt.
    assert store.reading("hash-v2").multiplier == 1.0


def test_join_reads_admitted_hashes_from_the_admission_ledger() -> None:
    from backend.core.ouroboros.governance.memory_admission import (
        AdmissionDecision, AdmissionReason, AdmissionRecord, AdmissionRow,
        MemoryConsumer, record_admission, reset_default_registry,
    )
    reset_default_registry()

    def row(uri, h, admitted):
        return AdmissionRow(
            source_id=uri, uri=uri, content_hash=h,
            decision=(AdmissionDecision.ADMITTED if admitted
                      else AdmissionDecision.WITHHELD),
            reason=(AdmissionReason.SEMANTIC if admitted
                    else AdmissionReason.RANK_BELOW_CUTOFF),
            score=1.0, chars=10)

    record_admission(AdmissionRecord.of(
        op_id="joined", consumer=MemoryConsumer.MAIN,
        rows=[row("in.md", "hash-in", True), row("out.md", "hash-out", False)],
        corpus_size=2, corpus_provenance="git_tracked", corpus_excluded=0,
        char_budget=100,
    ))
    assert observe_op_outcome("joined", passed=4, total=4) == 1
    # Only what was IN the prompt earns credit. A withheld topic was never
    # shown to the model and cannot have influenced the outcome.
    assert "hash-in" in mu.get_store().hashes()
    assert "hash-out" not in mu.get_store().hashes()


@pytest.mark.parametrize("passed,total,expect_order", [
    (5, 5, "high"), (3, 5, "mid"), (0, 5, "low"),
])
def test_verify_result_maps_onto_the_shared_polarity_scale(
    passed, total, expect_order,
) -> None:
    p = mu._polarity_for(passed, total)
    assert p is not None
    lookup = {"high": 1.0, "mid": 0.7, "low": 0.5}
    assert p == pytest.approx(lookup[expect_order])


# ---------------------------------------------------------------------------
# Near-duplicate propagation
# ---------------------------------------------------------------------------


def test_failure_propagates_to_a_near_duplicate_scaled_by_similarity(
    tmp_path: Path, monkeypatch,
) -> None:
    """Without this the router swaps one copy of bad advice for its twin."""
    from backend.core.ouroboros.governance import module_routing as mr
    monkeypatch.setitem(mr._emb_cache, "orig", [1.0, 0.0, 0.0])
    monkeypatch.setitem(mr._emb_cache, "twin", [0.999, 0.0447, 0.0])
    monkeypatch.setitem(mr._emb_cache, "unrelated", [0.0, 1.0, 0.0])

    store = mu.get_store()
    # Seed a corpus mean above the failing polarity so the failure registers
    # as below-average, which is what gates propagation.
    store.add([_obs("baseline", 1.0) for _ in range(4)]
              + [_obs("twin", 0.5), _obs("unrelated", 0.5)])

    observe_op_outcome("failing", passed=0, total=3, admitted_hashes=["orig"])

    twin_obs = store._by_hash["twin"]
    inherited = [o for o in twin_obs if o.source == "near_dup"]
    assert inherited, "near-duplicate did not inherit the failure"
    assert 0.9 < inherited[0].credit < 1.0, "credit must be similarity-scaled"
    assert not [o for o in store._by_hash["unrelated"]
                if o.source == "near_dup"]


def test_success_does_not_propagate_to_near_duplicates(
    tmp_path: Path, monkeypatch,
) -> None:
    """Asymmetric by design: spreading praise ranks a redundant corpus
    above a concise one, and a twin gains nothing it earned."""
    from backend.core.ouroboros.governance import module_routing as mr
    monkeypatch.setitem(mr._emb_cache, "orig", [1.0, 0.0])
    monkeypatch.setitem(mr._emb_cache, "twin", [1.0, 0.0])
    store = mu.get_store()
    store.add([_obs("baseline", 0.0) for _ in range(4)] + [_obs("twin", 0.5)])
    observe_op_outcome("winning", passed=3, total=3, admitted_hashes=["orig"])
    assert not [o for o in store._by_hash["twin"] if o.source == "near_dup"]


def test_propagation_is_disableable(monkeypatch) -> None:
    from backend.core.ouroboros.governance import module_routing as mr
    monkeypatch.setenv("JARVIS_MEMORY_NEAR_DUP_PROPAGATION", "0")
    monkeypatch.setitem(mr._emb_cache, "a", [1.0, 0.0])
    monkeypatch.setitem(mr._emb_cache, "b", [1.0, 0.0])
    assert mu._near_duplicates("a", ["b"]) == []


def test_missing_embedding_is_skipped_not_guessed() -> None:
    assert mu._near_duplicates("absent-from-cache", ["also-absent"]) == []


# ---------------------------------------------------------------------------
# Persistence, bounds, wiring, speed
# ---------------------------------------------------------------------------


def test_observations_survive_a_process_restart(tmp_path: Path) -> None:
    first = _store(tmp_path)
    first.add([_obs("t", 1.0), _obs("peer", 0.0)])
    second = _store(tmp_path)  # fresh index over the same log
    assert second.reading("t").observations == 1
    assert second.reading("t").multiplier > 1.0


def test_corrupt_log_lines_are_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / ".jarvis" / "memory_utility.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"h":"ok","w":1.0,"t":%f}\nNOT JSON\n{"h":"bad"}\n'
                    % time.time())
    assert UtilityStore(path).reading("ok").observations == 1


def test_log_is_bounded_and_compaction_keeps_the_newest(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_UTILITY_MAX_OBS", "100")
    store = _store(tmp_path)
    for i in range(300):
        store.add([_obs(f"h{i}", 1.0, age_days=300 - i)])
    assert store._count <= 100
    assert "h299" in store.hashes()
    assert "h0" not in store.hashes()


def test_listener_arms_additively_without_displacing_the_primary() -> None:
    from backend.core.ouroboros.governance.ops_digest_observer import (
        get_ops_digest_observer, register_ops_digest_observer,
        reset_ops_digest_observer,
    )

    class Primary:
        def __init__(self):
            self.seen = []

        def on_apply_succeeded(self, **kw):
            return

        def on_verify_completed(self, **kw):
            self.seen.append(kw["op_id"])

        def on_commit_succeeded(self, **kw):
            return

    reset_ops_digest_observer()
    primary = Primary()
    register_ops_digest_observer(primary)
    # Identity is preserved while nothing else is subscribed.
    assert get_ops_digest_observer() is primary

    try:
        assert mu.arm_outcome_listener() is True
        get_ops_digest_observer().on_verify_completed(
            op_id="fan", passed=1, total=1, scoped_to_applied_op=True)
        assert primary.seen == ["fan"], "primary observer was displaced"
    finally:
        reset_ops_digest_observer()


def test_a_raising_listener_cannot_starve_the_primary() -> None:
    from backend.core.ouroboros.governance.ops_digest_observer import (
        add_ops_digest_listener, get_ops_digest_observer,
        register_ops_digest_observer, reset_ops_digest_observer,
    )

    class Boom:
        def on_apply_succeeded(self, **kw):
            raise RuntimeError("boom")

        def on_verify_completed(self, **kw):
            raise RuntimeError("boom")

        def on_commit_succeeded(self, **kw):
            raise RuntimeError("boom")

    class Primary(Boom):
        def __init__(self):
            self.seen = []

        def on_verify_completed(self, **kw):
            self.seen.append(kw["op_id"])

    reset_ops_digest_observer()
    primary = Primary()
    register_ops_digest_observer(primary)
    add_ops_digest_listener(Boom())
    try:
        get_ops_digest_observer().on_verify_completed(
            op_id="x", passed=1, total=1)
        assert primary.seen == ["x"]
    finally:
        reset_ops_digest_observer()


def test_listener_registration_is_idempotent() -> None:
    from backend.core.ouroboros.governance.ops_digest_observer import (
        _LISTENERS, reset_ops_digest_observer,
    )
    reset_ops_digest_observer()
    try:
        mu.arm_outcome_listener()
        mu.arm_outcome_listener()
        mu.arm_outcome_listener()
        from backend.core.ouroboros.governance import ops_digest_observer as odo
        assert len(odo._LISTENERS) == 1
    finally:
        reset_ops_digest_observer()


def test_router_consults_utility_and_fails_open() -> None:
    from backend.core.ouroboros.governance.module_routing import (
        _utility_multiplier,
    )
    assert _utility_multiplier("cold") == 1.0
    assert _utility_multiplier("") == 1.0


def test_read_path_is_fast_enough_for_a_ranking_hot_path(
    tmp_path: Path,
) -> None:
    """The ranker calls this once per topic per op; it must not be a scan."""
    store = _store(tmp_path)
    store.add([_obs(f"h{i}", (i % 3) / 2.0) for i in range(2000)])
    start = time.perf_counter()
    for i in range(400):
        store.reading(f"h{i}")
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"400 readings took {elapsed:.2f}s"
