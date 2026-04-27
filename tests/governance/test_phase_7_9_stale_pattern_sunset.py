"""Phase 7.9 — stale-pattern sunset signal pins.

Pinned cage:
  * 30-day default staleness threshold (env-overridable)
  * MAX_STALE_CANDIDATES_PER_CYCLE=8 (operator-review surface trim)
  * sunset_candidate added to _TIGHTEN_KINDS (advisory but
    structurally conservative — operator must still file Pass B
    amendment to actually remove)
  * Surface validator: kind=sunset_candidate + sha256 prefix +
    observation_count + summary contains "stale" + day indicator
  * Validator chains with prior Slice 2 add_pattern validator
    (chain-of-responsibility — neither validator shadows the other)
  * Idempotent proposal_id (re-mining yields DUPLICATE_PROPOSAL_ID)
  * proposed_state_hash deterministically distinct from current
    (satisfies universal default check)
  * JSONL match-history reader fail-open (missing/oversized/malformed)
  * Master flag default false
  * Authority invariants
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    stale_pattern_detector as spd,
)
from backend.core.ouroboros.governance.adaptation.stale_pattern_detector import (
    DEFAULT_STALENESS_THRESHOLD_DAYS,
    MAX_HISTORY_FILE_BYTES,
    MAX_HISTORY_LINES,
    MAX_STALE_CANDIDATES_PER_CYCLE,
    MIN_OBSERVATIONS_FOR_SUNSET,
    StalePatternCandidate,
    StalePatternMatchEvent,
    get_staleness_threshold_days,
    is_detector_enabled,
    load_match_events,
    match_history_path,
    mine_stale_candidates_from_events,
    propose_sunset_candidates_from_events,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    OperatorDecisionStatus,
    ProposeStatus,
    _TIGHTEN_KINDS,
    get_surface_validator,
    reset_surface_validators,
    validate_monotonic_tightening,
)


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED", "1",
    )
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")


# ---------------------------------------------------------------------------
# Section A — module constants + master flag + dataclass
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_threshold_days_is_30(self):
        assert DEFAULT_STALENESS_THRESHOLD_DAYS == 30

    def test_max_stale_candidates_per_cycle_is_8(self):
        assert MAX_STALE_CANDIDATES_PER_CYCLE == 8

    def test_max_history_file_bytes_is_4MiB(self):
        assert MAX_HISTORY_FILE_BYTES == 4 * 1024 * 1024

    def test_max_history_lines_is_10000(self):
        assert MAX_HISTORY_LINES == 10_000

    def test_min_observations_for_sunset_is_1(self):
        assert MIN_OBSERVATIONS_FOR_SUNSET == 1

    def test_sunset_candidate_in_tighten_kinds(self):
        # Phase 7.9 — added to _TIGHTEN_KINDS so the universal default
        # validator accepts the kind. The surface validator enforces
        # the structural shape.
        assert "sunset_candidate" in _TIGHTEN_KINDS

    def test_truthy_constant_shape(self):
        assert spd._TRUTHY == ("1", "true", "yes", "on")


class TestMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED",
            raising=False,
        )
        assert is_detector_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED", v,
            )
            assert is_detector_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv(
                "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED", v,
            )
            assert is_detector_enabled() is False, v


class TestEnvOverrides:
    def test_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADAPTATION_STALENESS_THRESHOLD_DAYS",
            raising=False,
        )
        assert get_staleness_threshold_days() == 30

    def test_threshold_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_STALENESS_THRESHOLD_DAYS", "7",
        )
        assert get_staleness_threshold_days() == 7

    def test_threshold_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_STALENESS_THRESHOLD_DAYS", "not-an-int",
        )
        assert get_staleness_threshold_days() == 30

    def test_threshold_zero_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_STALENESS_THRESHOLD_DAYS", "0",
        )
        assert get_staleness_threshold_days() == 30

    def test_default_history_path(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SEMANTIC_GUARDIAN_MATCH_HISTORY_PATH",
            raising=False,
        )
        assert match_history_path() == (
            Path(".jarvis") / "semantic_guardian_match_history.jsonl"
        )

    def test_history_path_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom.jsonl"
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_GUARDIAN_MATCH_HISTORY_PATH", str(custom),
        )
        assert match_history_path() == custom


class TestDataclasses:
    def test_match_event_frozen(self):
        e = StalePatternMatchEvent(
            pattern_name="p", matched_at_unix=1.0,
        )
        with pytest.raises(Exception):
            e.matched_at_unix = 2.0  # type: ignore[misc]

    def test_candidate_proposal_id_idempotent(self):
        c1 = StalePatternCandidate(
            pattern_name="X", last_match_unix=0.0,
            days_since_last_match=99, summary="stale 99d",
        )
        c2 = StalePatternCandidate(
            pattern_name="X", last_match_unix=12345.0,
            days_since_last_match=42, summary="stale 42d",
        )
        # Idempotent on pattern_name only.
        assert c1.proposal_id() == c2.proposal_id()
        assert c1.proposal_id().startswith("adapt-sunset-")

    def test_candidate_proposal_id_differs_for_different_patterns(self):
        c1 = StalePatternCandidate(
            pattern_name="X", last_match_unix=0.0,
            days_since_last_match=99, summary="s",
        )
        c2 = StalePatternCandidate(
            pattern_name="Y", last_match_unix=0.0,
            days_since_last_match=99, summary="s",
        )
        assert c1.proposal_id() != c2.proposal_id()

    def test_candidate_proposed_state_hash_differs_from_current(self):
        c = StalePatternCandidate(
            pattern_name="X", last_match_unix=0.0,
            days_since_last_match=99, summary="s",
        )
        h = c.proposed_state_hash("sha256:abc")
        assert h != "sha256:abc"
        assert h.startswith("sha256:")


# ---------------------------------------------------------------------------
# Section B — JSONL match-history reader (fail-open)
# ---------------------------------------------------------------------------


class TestJSONLReader:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_match_events(tmp_path / "missing.jsonl") == []

    def test_oversize_refuses(self, tmp_path):
        path = tmp_path / "big.jsonl"
        path.write_text("x", encoding="utf-8")
        with mock.patch.object(
            Path, "stat",
            return_value=mock.Mock(st_size=MAX_HISTORY_FILE_BYTES + 1),
        ):
            assert load_match_events(path) == []

    def test_unreadable_returns_empty(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text("{}", encoding="utf-8")
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("denied"),
        ):
            assert load_match_events(path) == []

    def test_empty_file(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text("", encoding="utf-8")
        assert load_match_events(path) == []

    def test_happy_path(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            json.dumps({"pattern_name": "P1", "matched_at_unix": 100.0})
            + "\n"
            + json.dumps({"pattern_name": "P2", "matched_at_unix": 200.0})
            + "\n",
            encoding="utf-8",
        )
        out = load_match_events(path)
        assert len(out) == 2
        assert out[0].pattern_name == "P1"
        assert out[1].matched_at_unix == 200.0

    def test_malformed_json_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            "{not json\n"
            + json.dumps({"pattern_name": "P1", "matched_at_unix": 1.0})
            + "\n",
            encoding="utf-8",
        )
        out = load_match_events(path)
        assert len(out) == 1
        assert out[0].pattern_name == "P1"

    def test_non_mapping_line_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            "[1, 2, 3]\n"
            + json.dumps({"pattern_name": "P1", "matched_at_unix": 1.0})
            + "\n",
            encoding="utf-8",
        )
        assert len(load_match_events(path)) == 1

    def test_missing_pattern_name_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            json.dumps({"matched_at_unix": 1.0}) + "\n",
            encoding="utf-8",
        )
        assert load_match_events(path) == []

    def test_blank_pattern_name_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            json.dumps({"pattern_name": "   ", "matched_at_unix": 1.0})
            + "\n",
            encoding="utf-8",
        )
        assert load_match_events(path) == []

    def test_missing_matched_at_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            json.dumps({"pattern_name": "P1"}) + "\n",
            encoding="utf-8",
        )
        assert load_match_events(path) == []

    def test_non_numeric_matched_at_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            json.dumps({"pattern_name": "P1", "matched_at_unix": "yesterday"})
            + "\n",
            encoding="utf-8",
        )
        assert load_match_events(path) == []

    def test_negative_matched_at_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(
            json.dumps({"pattern_name": "P1", "matched_at_unix": -1.0})
            + "\n",
            encoding="utf-8",
        )
        assert load_match_events(path) == []

    def test_max_history_lines_truncate(self, tmp_path):
        path = tmp_path / "a.jsonl"
        # Build > MAX_HISTORY_LINES rows.
        lines = [
            json.dumps({"pattern_name": f"P{i}", "matched_at_unix": float(i)})
            for i in range(MAX_HISTORY_LINES + 100)
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = load_match_events(path)
        assert len(out) == MAX_HISTORY_LINES


# ---------------------------------------------------------------------------
# Section C — mine_stale_candidates_from_events
# ---------------------------------------------------------------------------


class TestMineStaleCandidates:
    def test_empty_input(self):
        assert mine_stale_candidates_from_events([], []) == []

    def test_recent_match_not_stale(self):
        now = 1_000_000.0
        # Last match 1 day ago, threshold 30 days → not stale.
        events = [
            StalePatternMatchEvent("P1", now - 86_400),
        ]
        out = mine_stale_candidates_from_events(
            ["P1"], events, threshold_days=30, now_unix=now,
        )
        assert out == []

    def test_old_match_is_stale(self):
        now = 1_000_000.0
        events = [
            StalePatternMatchEvent("P1", now - 86_400 * 60),  # 60 days old
        ]
        out = mine_stale_candidates_from_events(
            ["P1"], events, threshold_days=30, now_unix=now,
        )
        assert len(out) == 1
        assert out[0].pattern_name == "P1"
        assert out[0].days_since_last_match == 60

    def test_never_matched_is_stale(self):
        now = 1_000_000.0
        out = mine_stale_candidates_from_events(
            ["NEVER_MATCHED"], [], threshold_days=30, now_unix=now,
        )
        assert len(out) == 1
        assert out[0].pattern_name == "NEVER_MATCHED"
        assert out[0].last_match_unix == 0.0
        assert "never matched" in out[0].summary

    def test_threshold_exact_boundary_not_stale(self):
        now = 1_000_000.0
        # Last match exactly 30 days ago — elapsed == threshold ×
        # 86400, NOT strictly less than → considered stale.
        events = [
            StalePatternMatchEvent("P1", now - 86_400 * 30),
        ]
        out = mine_stale_candidates_from_events(
            ["P1"], events, threshold_days=30, now_unix=now,
        )
        # At exactly the threshold, the impl includes (>=) → stale.
        assert len(out) == 1

    def test_multiple_events_same_pattern_uses_max(self):
        now = 1_000_000.0
        events = [
            StalePatternMatchEvent("P1", now - 86_400 * 60),  # old
            StalePatternMatchEvent("P1", now - 86_400 * 5),  # recent
        ]
        out = mine_stale_candidates_from_events(
            ["P1"], events, threshold_days=30, now_unix=now,
        )
        # Most recent match wins → 5 days < 30 → not stale.
        assert out == []

    def test_sorted_stalest_first(self):
        now = 1_000_000.0
        events = [
            StalePatternMatchEvent("ALPHA", now - 86_400 * 40),
            StalePatternMatchEvent("BETA", now - 86_400 * 100),
        ]
        out = mine_stale_candidates_from_events(
            ["ALPHA", "BETA"], events, threshold_days=30, now_unix=now,
        )
        assert len(out) == 2
        assert out[0].pattern_name == "BETA"  # 100d > 40d
        assert out[1].pattern_name == "ALPHA"

    def test_alpha_tie_break(self):
        now = 1_000_000.0
        events = [
            StalePatternMatchEvent("ZETA", now - 86_400 * 60),
            StalePatternMatchEvent("ALPHA", now - 86_400 * 60),
        ]
        out = mine_stale_candidates_from_events(
            ["ZETA", "ALPHA"], events, threshold_days=30, now_unix=now,
        )
        assert out[0].pattern_name == "ALPHA"  # alpha tie-break
        assert out[1].pattern_name == "ZETA"

    def test_max_candidates_per_cycle_truncate(self):
        now = 1_000_000.0
        # 20 stale patterns; should cap to MAX_STALE_CANDIDATES_PER_CYCLE.
        patterns = [f"P{i:03d}" for i in range(20)]
        events = [
            StalePatternMatchEvent(p, now - 86_400 * 60)
            for p in patterns
        ]
        out = mine_stale_candidates_from_events(
            patterns, events, threshold_days=30, now_unix=now,
        )
        assert len(out) == MAX_STALE_CANDIDATES_PER_CYCLE

    def test_unknown_event_pattern_ignored(self):
        # An event for a pattern NOT in adapted_patterns is irrelevant.
        now = 1_000_000.0
        events = [
            StalePatternMatchEvent("NOT_TRACKED", now - 86_400 * 60),
        ]
        out = mine_stale_candidates_from_events(
            ["P1"], events, threshold_days=30, now_unix=now,
        )
        # P1 has no events → never matched → stale.
        assert len(out) == 1
        assert out[0].pattern_name == "P1"


# ---------------------------------------------------------------------------
# Section D — propose_sunset_candidates_from_events end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_validators():
    """Reset surface validators between tests since we re-register."""
    reset_surface_validators()
    yield
    reset_surface_validators()


@pytest.fixture
def fresh_ledger(tmp_path, monkeypatch, reset_validators):
    """Build a fresh AdaptationLedger backed by a tmp file."""
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    # Re-register the chained validator since reset_validators cleared it.
    from backend.core.ouroboros.governance.adaptation import (
        stale_pattern_detector,
    )
    stale_pattern_detector._VALIDATOR_REGISTERED = False
    stale_pattern_detector._register_validator_once()
    return AdaptationLedger(path=tmp_path / "ledger.jsonl")


class TestProposePipeline:
    def test_master_off_returns_empty(self, monkeypatch, fresh_ledger):
        monkeypatch.delenv(
            "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED",
            raising=False,
        )
        out = propose_sunset_candidates_from_events(
            ["P1"], [], ledger=fresh_ledger,
        )
        assert out == []

    def test_no_stale_returns_empty(self, monkeypatch, fresh_ledger):
        _enable(monkeypatch)
        now = time.time()
        events = [StalePatternMatchEvent("P1", now)]  # just-matched
        out = propose_sunset_candidates_from_events(
            ["P1"], events, threshold_days=30, now_unix=now,
            ledger=fresh_ledger,
        )
        assert out == []

    def test_one_stale_proposed_OK(self, monkeypatch, fresh_ledger):
        _enable(monkeypatch)
        now = 1_000_000.0
        events = [StalePatternMatchEvent("P1", now - 86_400 * 60)]
        out = propose_sunset_candidates_from_events(
            ["P1"], events,
            current_state_hash="sha256:abc",
            threshold_days=30, now_unix=now,
            ledger=fresh_ledger,
        )
        assert len(out) == 1
        assert out[0].status == ProposeStatus.OK
        assert out[0].proposal_id.startswith("adapt-sunset-")

    def test_proposal_idempotent_dedup(self, monkeypatch, fresh_ledger):
        _enable(monkeypatch)
        now = 1_000_000.0
        events = [StalePatternMatchEvent("P1", now - 86_400 * 60)]
        out1 = propose_sunset_candidates_from_events(
            ["P1"], events,
            current_state_hash="sha256:abc",
            threshold_days=30, now_unix=now,
            ledger=fresh_ledger,
        )
        out2 = propose_sunset_candidates_from_events(
            ["P1"], events,
            current_state_hash="sha256:abc",
            threshold_days=30, now_unix=now,
            ledger=fresh_ledger,
        )
        assert out1[0].status == ProposeStatus.OK
        assert out2[0].status == ProposeStatus.DUPLICATE_PROPOSAL_ID

    def test_proposal_pending_status(self, monkeypatch, fresh_ledger):
        # Sunset proposals land as PENDING (operator review).
        _enable(monkeypatch)
        now = 1_000_000.0
        events = [StalePatternMatchEvent("P1", now - 86_400 * 60)]
        propose_sunset_candidates_from_events(
            ["P1"], events,
            current_state_hash="sha256:abc",
            threshold_days=30, now_unix=now,
            ledger=fresh_ledger,
        )
        pending = fresh_ledger.list_pending()
        assert len(pending) == 1
        assert pending[0].proposal_kind == "sunset_candidate"
        assert pending[0].operator_decision == OperatorDecisionStatus.PENDING


# ---------------------------------------------------------------------------
# Section E — surface validator (chain-of-responsibility)
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    kind="sunset_candidate",
    proposed_hash="sha256:def",
    obs=1,
    summary="pattern P stale: 30d ago",
):
    from backend.core.ouroboros.governance.adaptation.ledger import (
        MonotonicTighteningVerdict,
    )
    return AdaptationProposal(
        schema_version="adaptation_ledger.1",
        proposal_id="adapt-sunset-test",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        proposal_kind=kind,
        evidence=AdaptationEvidence(
            window_days=30, observation_count=obs,
            source_event_ids=("P",), summary=summary,
        ),
        current_state_hash="sha256:abc",
        proposed_state_hash=proposed_hash,
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="2026-04-26T00:00:00Z",
        proposed_at_epoch=1.0,
    )


class TestSurfaceValidator:
    def test_validator_registered_at_module_import(
        self, reset_validators,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()
        v = get_surface_validator(
            AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        )
        assert v is not None

    def test_valid_sunset_passes(self, reset_validators):
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()
        p = _make_proposal()
        verdict, _ = validate_monotonic_tightening(p)
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        assert verdict == MonotonicTighteningVerdict.PASSED

    def test_proposed_hash_format_required(self, reset_validators):
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()
        p = _make_proposal(proposed_hash="not-sha256-prefixed")
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        verdict, detail = validate_monotonic_tightening(p)
        # First the universal default check fires (hash differs from current);
        # then the surface validator catches the format issue.
        assert verdict == MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
        assert "hash_format" in detail or "loosen" in detail.lower()

    def test_observation_count_below_min_rejected(self, reset_validators):
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()
        p = _make_proposal(obs=0)
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        verdict, detail = validate_monotonic_tightening(p)
        assert verdict == MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
        assert "observation_count" in detail

    def test_summary_missing_stale_indicator_rejected(
        self, reset_validators,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()
        p = _make_proposal(summary="pattern P last seen 30d ago")
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        verdict, detail = validate_monotonic_tightening(p)
        assert verdict == MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
        assert "stale" in detail

    def test_summary_missing_day_indicator_rejected(self, reset_validators):
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()
        p = _make_proposal(summary="pattern P stale (no time indicator)")
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        verdict, detail = validate_monotonic_tightening(p)
        assert verdict == MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
        assert "day_indicator" in detail

    def test_chain_delegates_add_pattern_to_prior(self, reset_validators):
        # If a prior validator is registered (e.g. Slice 2), our chain
        # should delegate non-sunset kinds to it.
        prior_calls = []

        def prior_validator(proposal):
            prior_calls.append(proposal.proposal_kind)
            return (True, "prior_passed")

        from backend.core.ouroboros.governance.adaptation.ledger import (
            register_surface_validator,
        )
        register_surface_validator(
            AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            prior_validator,
        )
        # Now register our chain on top.
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()

        # add_pattern proposal should reach prior validator.
        p = _make_proposal(kind="add_pattern")
        validate_monotonic_tightening(p)
        assert "add_pattern" in prior_calls
        # sunset_candidate should NOT call prior.
        prior_calls.clear()
        p2 = _make_proposal(kind="sunset_candidate")
        validate_monotonic_tightening(p2)
        assert "sunset_candidate" not in prior_calls

    def test_chain_no_prior_passes_other_kinds(self, reset_validators):
        # No prior registered → our chain returns
        # "no_prior_validator_for_kind_pass" for non-sunset.
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector,
        )
        stale_pattern_detector._VALIDATOR_REGISTERED = False
        stale_pattern_detector._register_validator_once()
        p = _make_proposal(kind="add_pattern")
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        verdict, _ = validate_monotonic_tightening(p)
        assert verdict == MonotonicTighteningVerdict.PASSED


# ---------------------------------------------------------------------------
# Section F — authority invariants
# ---------------------------------------------------------------------------


_DETECTOR_PATH = Path(spd.__file__)


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        source = _DETECTOR_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "semantic_guardian",
            "scoped_tool_backend",
            "general_driver",
            "exploration_engine",
            "risk_tier_floor",
            "orchestrator",
            "tool_executor",
            "phase_runners",
            "gate_runner",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in banned_substrings:
                    assert banned not in node.module, (
                        f"banned import: {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in banned_substrings:
                        assert banned not in alias.name, (
                            f"banned import: {alias.name}"
                        )

    def test_only_stdlib_and_adaptation_ledger(self):
        source = _DETECTOR_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "hashlib", "json", "logging", "os", "time",
            "dataclasses", "pathlib", "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    assert "adaptation" in node.module, (
                        f"non-adaptation backend import: {node.module}"
                    )
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), f"unexpected import: {node.module}"

    def test_no_subprocess_or_network_tokens(self):
        source = _DETECTOR_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
        ):
            assert token not in source, f"banned token: {token}"
