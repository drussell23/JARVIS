"""P1.5 — Hypothesis ledger — graduation pin suite + integration regression.

Mirrors P0/P0.5/P1 graduation patterns. Closes Phase 2 P1.5.

Sections:
    (A) Master flag — JARVIS_HYPOTHESIS_PAIRING_ENABLED default true
    (B) Hot-revert — explicit false → engine emits proposal but no
        hypothesis (back-compat with pre-Slice-2 ProposalDraft shape)
    (C) Engine integration — paired hypothesis emitted on success;
        hypothesis_id carried on ProposalDraft; ledger row created
    (D) Validator — pure classify() + end-to-end validate_hypothesis()
        with token-overlap math
    (E) Schema invariants — ProposalDraft.hypothesis_id optional;
        Hypothesis.schema_version frozen
    (F) Authority invariants — engine no provider imports unchanged;
        validator no banned imports
    (G) Bounded safety — engine-side caps unchanged; hypothesis pairing
        does NOT bypass posture/cap/cost gates
    (H) Stats — ledger.stats() returns expected counts
    (I) End-to-end integration — engine emit → validator decide →
        ledger updated → stats reflected
    (J) Telemetry — INFO marker fires on engine-emitted hypothesis +
        on validator decision
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.hypothesis_ledger import (
    Hypothesis,
    HypothesisLedger,
    make_hypothesis_id,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.hypothesis_validator import (
    DEFAULT_OVERLAP_THRESHOLD,
    INVALIDATION_OVERLAP_THRESHOLD,
    classify,
    overlap_ratio,
    validate_hypothesis,
)
from backend.core.ouroboros.governance.postmortem_recall import PostmortemRecord
from backend.core.ouroboros.governance.posture import Posture
from backend.core.ouroboros.governance.self_goal_formation import (
    SelfGoalFormationEngine,
    hypothesis_pairing_enabled,
    reset_default_engine,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "JARVIS_SELF_GOAL_FORMATION_ENABLED",
        "JARVIS_HYPOTHESIS_PAIRING_ENABLED",
        "JARVIS_SELF_GOAL_PER_SESSION_CAP",
        "JARVIS_SELF_GOAL_COST_CAP_USD",
    ):
        monkeypatch.delenv(k, raising=False)
    reset_default_engine()
    reset_default_ledger()
    yield
    reset_default_engine()
    reset_default_ledger()


def _record(op_id, ts):
    return PostmortemRecord(
        op_id=op_id, session_id="s1",
        root_cause="all_providers_exhausted:fallback_failed",
        failed_phase="GENERATE",
        next_safe_action="retry_with_smaller_seed",
        target_files=("a.py",),
        timestamp_iso="2026-04-26T10:00:00",
        timestamp_unix=ts,
    )


def _three_records():
    return [_record(f"op{i}", 1_700_000_000.0 + i * 3600.0) for i in range(3)]


def _stub_with_hypothesis(
    description="Investigate provider exhaustion",
    rationale="3 ops failed with same root cause",
    claim="Provider fallback retry storm causes exhaustion",
    expected="After fix exhaustion rate drops below 5 percent",
    cost=0.05,
):
    payload = json.dumps({
        "description": description,
        "rationale": rationale,
        "claim": claim,
        "expected_outcome": expected,
    })
    return lambda p, m: (payload, cost)


def _stub_no_hypothesis(cost=0.05):
    """Pre-Slice-2 model output shape (no claim/expected_outcome)."""
    payload = json.dumps({
        "description": "Investigate provider exhaustion",
        "rationale": "3 ops failed",
    })
    return lambda p, m: (payload, cost)


def _engine(repo: Path) -> SelfGoalFormationEngine:
    return SelfGoalFormationEngine(
        project_root=repo,
        ledger_path=repo / ".jarvis" / "self_goal_formation_proposals.jsonl",
    )


# ===========================================================================
# A — Master flag
# ===========================================================================


def test_pairing_default_true_post_graduation(monkeypatch):
    monkeypatch.delenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", raising=False)
    assert hypothesis_pairing_enabled() is True


def test_pairing_explicit_false_hot_revert(monkeypatch):
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "false")
    assert hypothesis_pairing_enabled() is False


def test_pin_pairing_env_reader_default_true_literal():
    src = _read("backend/core/ouroboros/governance/self_goal_formation.py")
    assert (
        '"JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true"' in src
    ), (
        "Pairing flag default literal moved or changed. If P1.5 was rolled "
        "back, update both the source AND this pin."
    )


# ===========================================================================
# B — Hot-revert behavior
# ===========================================================================


def test_engine_with_pairing_off_emits_no_hypothesis(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "false")
    eng = _engine(tmp_path)
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(),
    )
    assert draft is not None
    assert draft.hypothesis_id is None  # no pairing → no hypothesis_id
    # Hypothesis ledger should be untouched.
    assert not (tmp_path / ".jarvis" / "hypothesis_ledger.jsonl").exists()


def test_engine_with_pairing_on_but_model_omits_fields(monkeypatch, tmp_path):
    """Defensive: pairing enabled but model didn't emit claim/expected
    (e.g. ignoring instructions). Engine still emits proposal; just no
    hypothesis_id."""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true")
    eng = _engine(tmp_path)
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_no_hypothesis(),
    )
    assert draft is not None
    assert draft.hypothesis_id is None


# ===========================================================================
# C — Engine integration
# ===========================================================================


def test_engine_emits_paired_hypothesis(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true")
    eng = _engine(tmp_path)
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(),
    )
    assert draft is not None
    assert draft.hypothesis_id is not None
    assert len(draft.hypothesis_id) == 12  # sha256[:12]

    hl = HypothesisLedger(project_root=tmp_path)
    rows = hl.load_all()
    assert len(rows) == 1
    h = rows[0]
    assert h.hypothesis_id == draft.hypothesis_id
    assert h.claim.startswith("Provider fallback retry storm")
    assert h.expected_outcome.startswith("After fix")
    assert h.is_open()  # validator hasn't run yet
    assert h.proposed_signature_hash == draft.signature_hash


def test_proposed_signature_hash_links_proposal_to_hypothesis(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    eng = _engine(tmp_path)
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(),
    )
    hl = HypothesisLedger(project_root=tmp_path)
    h = hl.find_by_id(draft.hypothesis_id)
    assert h.proposed_signature_hash == draft.signature_hash


def test_engine_persist_failure_does_not_break_proposal(monkeypatch, tmp_path):
    """If hypothesis ledger write fails, the proposal still emits — the
    hypothesis is observability + future validation, not load-bearing."""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true")
    eng = _engine(tmp_path)
    # Pre-create ledger as a directory to force write failure.
    (tmp_path / ".jarvis").mkdir()
    bad_ledger = tmp_path / ".jarvis" / "hypothesis_ledger.jsonl"
    bad_ledger.mkdir()  # path exists but is a directory → open(append) fails
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(),
    )
    # Proposal still emitted; hypothesis_id is None.
    assert draft is not None
    assert draft.hypothesis_id is None


# ===========================================================================
# D — Validator
# ===========================================================================


def test_overlap_ratio_basic():
    o = overlap_ratio(
        "exhaustion rate drops below 5 percent",
        "exhaustion rate dropped to 3 percent",
    )
    # tokens(expected) = {exhaustion, rate, drops, below, percent} (5 after stop-word removal)
    # Wait — `5` matches `[a-z0-9]{2,}` only if 2+ chars; "5" is length 1 → dropped.
    # tokens(actual) = {exhaustion, rate, dropped, percent} after stop words
    # matched = {exhaustion, rate, percent} = 3
    # ratio = 3/5 = 0.6
    assert 0.4 < o < 0.8


def test_overlap_ratio_empty_expected():
    assert overlap_ratio("", "actual") == 0.0


def test_overlap_ratio_strips_stop_words():
    """Pure stop-words → empty expected token set → ratio 0.0."""
    o = overlap_ratio("the of a", "in on at")
    assert o == 0.0


def test_classify_high_overlap_returns_true():
    assert classify(
        "exhaustion rate drops",
        "exhaustion rate drops",
    ) is True


def test_classify_low_overlap_returns_false():
    assert classify(
        "exhaustion rate drops below five percent",
        "completely unrelated outcome words zzzz",
    ) is False


def test_classify_middle_band_returns_none():
    """Overlap between invalidation and validation thresholds → None."""
    # Middle band: > 0.1 and < 0.5
    expected = "alpha beta gamma delta epsilon"  # 5 tokens
    actual = "alpha beta zeta eta"               # 2 match → ratio 0.4
    assert classify(expected, actual) is None


def test_default_thresholds_pinned():
    assert DEFAULT_OVERLAP_THRESHOLD == 0.5
    assert INVALIDATION_OVERLAP_THRESHOLD == 0.1


def test_validate_hypothesis_unknown_id_no_decision(tmp_path):
    hl = HypothesisLedger(project_root=tmp_path)
    r = validate_hypothesis("nonexistent", "anything", ledger=hl)
    assert r.validated is None


def test_validate_hypothesis_records_outcome_to_ledger(tmp_path):
    hl = HypothesisLedger(project_root=tmp_path)
    h = Hypothesis(
        hypothesis_id=make_hypothesis_id("op", "claim", 1.0),
        op_id="op", claim="claim",
        expected_outcome="exhaustion rate drops below five percent",
        created_unix=1.0,
    )
    hl.append(h)
    r = validate_hypothesis(
        h.hypothesis_id,
        "exhaustion rate dropped to three percent after fix",
        ledger=hl,
    )
    assert r.validated is True
    after = hl.find_by_id(h.hypothesis_id)
    assert after.is_validated()
    assert after.actual_outcome.startswith("exhaustion rate dropped")


def test_validate_hypothesis_undecidable_still_records_actual(tmp_path):
    hl = HypothesisLedger(project_root=tmp_path)
    h = Hypothesis(
        hypothesis_id=make_hypothesis_id("op", "claim", 1.0),
        op_id="op", claim="claim",
        expected_outcome="alpha beta gamma delta epsilon",
        created_unix=1.0,
    )
    hl.append(h)
    r = validate_hypothesis(h.hypothesis_id, "alpha beta zeta eta", ledger=hl)
    assert r.validated is None  # undecidable
    after = hl.find_by_id(h.hypothesis_id)
    assert after.actual_outcome == "alpha beta zeta eta"
    assert after.validated is None  # ledger reflects undecidable


# ===========================================================================
# E — Schema invariants
# ===========================================================================


def test_proposal_draft_hypothesis_id_optional(monkeypatch, tmp_path):
    """Pre-Slice-2 ProposalDrafts persisted without hypothesis_id must
    still load cleanly."""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "false")
    eng = _engine(tmp_path)
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_no_hypothesis(),
    )
    assert draft is not None
    d = draft.to_ledger_dict()
    assert "hypothesis_id" in d
    assert d["hypothesis_id"] is None


def test_hypothesis_schema_version_unchanged():
    from backend.core.ouroboros.governance.hypothesis_ledger import (
        HYPOTHESIS_SCHEMA_VERSION,
    )
    assert HYPOTHESIS_SCHEMA_VERSION == "hypothesis_ledger.1"


# ===========================================================================
# F — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
    "from backend.core.ouroboros.governance.providers",
    "from backend.core.ouroboros.governance.doubleword_provider",
]


def test_validator_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/hypothesis_validator.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_engine_still_no_authority_imports_post_slice2():
    src = _read("backend/core/ouroboros/governance/self_goal_formation.py")
    # The engine added a late import of hypothesis_ledger (allowed —
    # ledger is a sibling primitive). Pin: still no orchestrator/policy/
    # provider imports.
    for imp in _BANNED:
        assert imp not in src, (
            f"banned import in engine post-Slice-2: {imp}"
        )


# ===========================================================================
# G — Bounded safety unchanged by hypothesis pairing
# ===========================================================================


def test_hypothesis_pairing_does_not_bypass_posture_veto(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true")
    eng = _engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.HARDEN,  # vetoed
        model_caller=_stub_with_hypothesis(),
    )
    assert out is None
    # Hypothesis ledger NOT touched (engine short-circuited before
    # any model call).
    assert not (tmp_path / ".jarvis" / "hypothesis_ledger.jsonl").exists()


def test_hypothesis_pairing_does_not_bypass_per_session_cap(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SELF_GOAL_PER_SESSION_CAP", "1")
    eng = _engine(tmp_path)
    eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(),
    )
    second = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(),
    )
    assert second is None  # cap holds even with pairing on


# ===========================================================================
# H — Stats
# ===========================================================================


def test_stats_after_engine_emit_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    eng = _engine(tmp_path)
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(),
    )
    hl = HypothesisLedger(project_root=tmp_path)
    s_open = hl.stats()
    assert s_open == {"total": 1, "open": 1, "validated": 0, "invalidated": 0}

    # Run validator with a high-overlap actual outcome
    validate_hypothesis(
        draft.hypothesis_id,
        "exhaustion rate dropped to 3 percent after fix landed",
        ledger=hl,
    )
    s_validated = hl.stats()
    assert s_validated == {
        "total": 1, "open": 0, "validated": 1, "invalidated": 0,
    }


# ===========================================================================
# I — End-to-end integration (the load-bearing pin)
# ===========================================================================


def test_end_to_end_p1_5(tmp_path, monkeypatch):
    """The whole P1.5 chain in one test:
      1. Engine evaluates → ProposalDraft + paired Hypothesis
      2. Hypothesis persists to JSONL ledger
      3. Validator is invoked with a matching actual outcome
      4. Ledger updated; stats reflect a validated hypothesis
      5. REPL surface (optional spot-check) sees the row"""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true")

    eng = _engine(tmp_path)
    draft = eng.evaluate(
        postmortems=_three_records(), posture=Posture.EXPLORE,
        model_caller=_stub_with_hypothesis(
            claim="X causes Y in fallback path",
            expected="If we apply patch Z exhaustion rate drops",
        ),
    )
    assert draft is not None
    assert draft.hypothesis_id is not None

    hl = HypothesisLedger(project_root=tmp_path)
    assert hl.stats()["open"] == 1

    result = validate_hypothesis(
        draft.hypothesis_id,
        "After patch Z landed exhaustion rate dropped 60 percent",
        ledger=hl,
    )
    assert result.validated is True
    assert hl.stats()["validated"] == 1

    # REPL spot-check: validated subcommand surfaces this row.
    from backend.core.ouroboros.governance.hypothesis_repl import (
        dispatch_hypothesis_command as REPL,
    )
    r = REPL("/hypothesis ledger validated", project_root=tmp_path, ledger=hl)
    assert r.ok is True
    assert draft.hypothesis_id[:12] in r.text


# ===========================================================================
# J — Telemetry
# ===========================================================================


def test_engine_emits_pairing_telemetry_on_success(
    monkeypatch, tmp_path, caplog,
):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PAIRING_ENABLED", "true")
    eng = _engine(tmp_path)
    with caplog.at_level(logging.INFO):
        eng.evaluate(
            postmortems=_three_records(), posture=Posture.EXPLORE,
            model_caller=_stub_with_hypothesis(),
        )
    msgs = [r.getMessage() for r in caplog.records]
    pairing_msgs = [
        m for m in msgs
        if "[SelfGoalFormation] paired hypothesis emitted" in m
    ]
    assert pairing_msgs, f"pairing telemetry missing; got: {msgs}"


def test_validator_emits_decision_telemetry(tmp_path, caplog):
    hl = HypothesisLedger(project_root=tmp_path)
    h = Hypothesis(
        hypothesis_id=make_hypothesis_id("op", "claim", 1.0),
        op_id="op", claim="claim",
        expected_outcome="alpha beta gamma delta",
        created_unix=1.0,
    )
    hl.append(h)
    with caplog.at_level(logging.INFO):
        validate_hypothesis(
            h.hypothesis_id, "alpha beta gamma delta", ledger=hl,
        )
    msgs = [r.getMessage() for r in caplog.records]
    decision_msgs = [
        m for m in msgs if "[HypothesisValidator] op=engine validated" in m
    ]
    assert decision_msgs, f"validator telemetry missing; got: {msgs}"
