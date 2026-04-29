"""RR Pass C Slice 2 — SemanticGuardian POSTMORTEM-mined patterns regression suite.

Pins:
  * Module constants + master flag default-false-pre-graduation.
  * PostmortemEventLite + MinedPatternProposal frozen dataclasses.
  * Threshold + window-days env overrides.
  * Window filter: events older than cutoff dropped; epoch=0
    back-compat retained.
  * Group-by-(root_cause, failure_class) — empty fields skipped.
  * LCS pipeline: stdlib-only longest common substring; bounded;
    rejects too-short matches.
  * Existing-pattern check: candidate substring of existing OR vice
    versa = duplicate.
  * mine_patterns_from_events end-to-end paths:
    - empty input → empty output
    - all groups below threshold → empty output
    - group passes threshold + LCS yields short → no proposal
    - group passes threshold + LCS yields good pattern + no
      existing match → 1 proposal
    - group passes threshold + LCS matches existing → no proposal
  * propose_patterns_from_events:
    - master flag off → empty (no ledger writes)
    - master on → 1 OK per qualifying group
    - re-mining same events → DUPLICATE_PROPOSAL_ID (idempotent)
  * Surface validator (registered at import):
    - kind != add_pattern → reject
    - proposed_state_hash without sha256: prefix → reject
    - observation_count < threshold → reject
    - all valid → pass
  * Authority invariants (AST grep): no banned governance imports;
    no subprocess/network/env-mutation; substrate import only.
"""
from __future__ import annotations

import ast as _ast
import hashlib
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    MonotonicTighteningVerdict,
    OperatorDecisionStatus,
    ProposeStatus,
    get_surface_validator,
    register_surface_validator,
    reset_default_ledger,
    reset_surface_validators,
    validate_monotonic_tightening,
)
from backend.core.ouroboros.governance.adaptation.semantic_guardian_miner import (
    DEFAULT_PATTERN_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    MAX_EXCERPTS_PER_GROUP,
    MAX_SYNTHESIZED_PATTERN_CHARS,
    MIN_LCS_LENGTH,
    MIN_SYNTHESIZED_PATTERN_CHARS,
    MinedPatternProposal,
    PostmortemEventLite,
    _longest_common_substring,
    _pattern_already_exists,
    get_pattern_threshold,
    get_window_days,
    install_surface_validator,
    is_enabled,
    mine_patterns_from_events,
    propose_patterns_from_events,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "semantic_guardian_miner.py"
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"),
    )
    yield
    reset_default_ledger()
    # Re-install the surface validator after each test (other tests
    # may call reset_surface_validators).
    install_surface_validator()


def _ev(
    *,
    op_id="op-1",
    root_cause="generation_returned_no_candidates",
    failure_class="exhausted",
    error_type="",
    code_snippet_excerpt="",
    timestamp_unix=None,
):
    return PostmortemEventLite(
        op_id=op_id,
        root_cause=root_cause,
        failure_class=failure_class,
        error_type=error_type,
        code_snippet_excerpt=code_snippet_excerpt,
        timestamp_unix=(
            timestamp_unix if timestamp_unix is not None else time.time()
        ),
    )


def _ledger(tmp_path):
    return AdaptationLedger(tmp_path / "ledger.jsonl")


# ===========================================================================
# A — Module constants + dataclasses + master flag
# ===========================================================================


def test_default_pattern_threshold_pinned():
    assert DEFAULT_PATTERN_THRESHOLD == 3


def test_default_window_days_pinned():
    assert DEFAULT_WINDOW_DAYS == 7


def test_max_excerpts_per_group_pinned():
    assert MAX_EXCERPTS_PER_GROUP == 32


def test_max_synthesized_pattern_chars_pinned():
    assert MAX_SYNTHESIZED_PATTERN_CHARS == 256


def test_min_lcs_length_pinned():
    assert MIN_LCS_LENGTH == 8


def test_min_synthesized_pattern_chars_pinned():
    assert MIN_SYNTHESIZED_PATTERN_CHARS == 8


def test_master_flag_default_true_post_graduation(monkeypatch):
    """Graduated 2026-04-29 (Move 1 Pass C cadence) — empty/unset env
    returns True. Asymmetric semantics: explicit falsy hot-reverts."""
    monkeypatch.delenv(
        "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", raising=False,
    )
    assert is_enabled() is True


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", val)
        assert is_enabled() is True


def test_master_flag_falsy_variants(monkeypatch):
    # Post-graduation: empty/whitespace = unset = graduated default-true.
    # Only explicit falsy tokens hot-revert.
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", val)
        assert is_enabled() is False


def test_master_flag_empty_string_post_graduation(monkeypatch):
    """Asymmetric env semantics — explicit empty string is treated as
    unset and returns the graduated default-true."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "")
    assert is_enabled() is True


def test_postmortem_event_lite_is_frozen():
    e = _ev()
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.op_id = "x"  # type: ignore[misc]


def test_mined_pattern_proposal_is_frozen():
    p = MinedPatternProposal(
        group_key=("rc", "fc"), proposed_pattern="xxxxxxxxx",
        excerpt_count=3, source_event_ids=("op-1",), summary="s",
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.excerpt_count = 5  # type: ignore[misc]


# ===========================================================================
# B — Threshold + window env overrides
# ===========================================================================


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_PATTERN_THRESHOLD", "10")
    assert get_pattern_threshold() == 10


def test_threshold_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_PATTERN_THRESHOLD", "garbage")
    assert get_pattern_threshold() == DEFAULT_PATTERN_THRESHOLD


def test_threshold_env_zero_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_PATTERN_THRESHOLD", "0")
    assert get_pattern_threshold() == DEFAULT_PATTERN_THRESHOLD


def test_window_days_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_WINDOW_DAYS", "30")
    assert get_window_days() == 30


def test_window_days_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_WINDOW_DAYS", "x")
    assert get_window_days() == DEFAULT_WINDOW_DAYS


# ===========================================================================
# C — LCS algorithm
# ===========================================================================


def test_lcs_empty_input():
    assert _longest_common_substring([]) == ""


def test_lcs_single_input_returns_full():
    assert _longest_common_substring(["foobarbaz"]) == "foobarbaz"


def test_lcs_no_overlap_returns_empty():
    assert _longest_common_substring(["foo", "bar"]) == ""


def test_lcs_finds_shared_substring():
    inputs = [
        "alpha XYZcommonXYZ beta",
        "gamma XYZcommonXYZ delta",
        "epsilon XYZcommonXYZ zeta",
    ]
    out = _longest_common_substring(inputs)
    assert "XYZcommonXYZ" in out


def test_lcs_filters_falsy_inputs():
    assert _longest_common_substring(["", "abc", ""]) == "abc"


def test_lcs_bounded_by_max_chars():
    """Pin: LCS output respects MAX_SYNTHESIZED_PATTERN_CHARS."""
    big = "x" * (MAX_SYNTHESIZED_PATTERN_CHARS + 100)
    out = _longest_common_substring([big])
    assert len(out) <= MAX_SYNTHESIZED_PATTERN_CHARS


# ===========================================================================
# D — Existing-pattern duplicate check
# ===========================================================================


def test_pattern_exists_substring_of_existing():
    assert _pattern_already_exists(
        "foobar", ["XYZfoobarABC"],
    ) is True


def test_pattern_exists_existing_substring_of_candidate():
    assert _pattern_already_exists(
        "XYZfoobarABC", ["foobar"],
    ) is True


def test_pattern_does_not_exist_when_disjoint():
    assert _pattern_already_exists(
        "foobar", ["alpha", "beta"],
    ) is False


def test_empty_candidate_treated_as_existing():
    assert _pattern_already_exists("", ["anything"]) is True


def test_empty_existing_list_returns_false():
    assert _pattern_already_exists("abcdefgh", []) is False


# ===========================================================================
# E — mine_patterns_from_events end-to-end
# ===========================================================================


def test_mine_empty_input_returns_empty():
    assert mine_patterns_from_events([]) == []


def test_mine_below_threshold_returns_empty():
    """Single event for a group → below default threshold (3)."""
    out = mine_patterns_from_events([_ev()])
    assert out == []


def test_mine_at_threshold_with_short_lcs_returns_empty():
    """3 events but the LCS of their excerpts is below MIN_LCS_LENGTH."""
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt=f"short {i}")
        for i in range(3)
    ]
    out = mine_patterns_from_events(events)
    assert out == []


def test_mine_at_threshold_with_good_lcs_proposes():
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt=f"prefix CRITICAL_EXCEPT_PATTERN_X{i}suffix")
        for i in range(3)
    ]
    out = mine_patterns_from_events(events)
    assert len(out) == 1
    assert "CRITICAL_EXCEPT_PATTERN_X" in out[0].proposed_pattern
    assert out[0].excerpt_count == 3
    assert out[0].source_event_ids == ("op-0", "op-1", "op-2")


def test_mine_skips_groups_with_existing_pattern_match():
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt=f"prefix CRITICAL_EXCEPT_PATTERN_X{i}suffix")
        for i in range(3)
    ]
    out = mine_patterns_from_events(
        events,
        existing_patterns=("CRITICAL_EXCEPT_PATTERN",),
    )
    assert out == []


def test_mine_skips_empty_root_cause_or_failure_class():
    events = [
        _ev(op_id="op-1", root_cause="", code_snippet_excerpt="X" * 50),
        _ev(op_id="op-2", failure_class="", code_snippet_excerpt="X" * 50),
        _ev(op_id="op-3", code_snippet_excerpt="X" * 50),
        _ev(op_id="op-4", code_snippet_excerpt="X" * 50),
        _ev(op_id="op-5", code_snippet_excerpt="X" * 50),
    ]
    out = mine_patterns_from_events(events)
    # Only op-3, op-4, op-5 have both fields → 1 group of 3, qualifies.
    assert len(out) == 1
    assert out[0].excerpt_count == 3


def test_mine_window_filter_drops_old_events():
    now = time.time()
    old = _ev(op_id="op-old",
              code_snippet_excerpt="CRITICAL_X" * 5,
              timestamp_unix=now - (8 * 86_400))  # 8 days old
    # 3 fresh + 1 old = should still propose (1 group of 3 fresh)
    fresh = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt="CRITICAL_X" * 5,
            timestamp_unix=now - (i * 60))
        for i in range(3)
    ]
    out = mine_patterns_from_events(
        [old] + fresh, window_days=7, now_unix=now,
    )
    # Old event filtered → 3 fresh form a group
    assert len(out) == 1
    assert "op-old" not in out[0].source_event_ids


def test_mine_custom_threshold():
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt="DETECTOR_PATTERN_XYZABC" + str(i))
        for i in range(2)
    ]
    out = mine_patterns_from_events(events, threshold=2)
    assert len(out) == 1


def test_mine_separate_groups_processed_independently():
    """Two groups, each with 3 events → 2 proposals."""
    events = []
    for i in range(3):
        events.append(_ev(
            op_id=f"op-A-{i}",
            root_cause="root_A", failure_class="cls_A",
            code_snippet_excerpt=f"PATTERN_A_XYZABC{i}",
        ))
    for i in range(3):
        events.append(_ev(
            op_id=f"op-B-{i}",
            root_cause="root_B", failure_class="cls_B",
            code_snippet_excerpt=f"PATTERN_B_XYZABC{i}",
        ))
    out = mine_patterns_from_events(events)
    assert len(out) == 2
    keys = {p.group_key for p in out}
    assert keys == {("root_A", "cls_A"), ("root_B", "cls_B")}


# ===========================================================================
# F — propose_patterns_from_events: ledger integration
# ===========================================================================


def test_propose_master_off_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "0")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt="CRITICAL_PATTERN_XYZABC" + str(i))
        for i in range(3)
    ]
    out = propose_patterns_from_events(events, ledger=led)
    assert out == []
    # Ledger never written (master gate of THIS surface, not substrate).
    assert not (tmp_path / "ledger.jsonl").exists()


def test_propose_master_on_writes_proposals(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt="CRITICAL_PATTERN_XYZABC" + str(i))
        for i in range(3)
    ]
    out = propose_patterns_from_events(events, ledger=led)
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    assert (tmp_path / "ledger.jsonl").exists()
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.surface is AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS
    assert p.proposal_kind == "add_pattern"
    assert p.evidence.observation_count == 3


def test_propose_idempotent_on_same_events(monkeypatch, tmp_path):
    """Re-mining the same events yields DUPLICATE_PROPOSAL_ID (the
    proposal_id is a stable hash of group + pattern)."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt="CRITICAL_PATTERN_XYZABC" + str(i))
        for i in range(3)
    ]
    propose_patterns_from_events(events, ledger=led)
    second = propose_patterns_from_events(events, ledger=led)
    assert len(second) == 1
    assert second[0].status is ProposeStatus.DUPLICATE_PROPOSAL_ID


def test_propose_writes_evidence_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"op-{i}",
            root_cause="my_root", failure_class="my_class",
            code_snippet_excerpt="CRITICAL_PATTERN_XYZABC" + str(i))
        for i in range(3)
    ]
    out = propose_patterns_from_events(events, ledger=led)
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert "my_root" in p.evidence.summary
    assert "my_class" in p.evidence.summary
    assert p.evidence.window_days == DEFAULT_WINDOW_DAYS


def test_propose_existing_pattern_skipped_no_ledger_write(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt="CRITICAL_PATTERN_XYZABC" + str(i))
        for i in range(3)
    ]
    out = propose_patterns_from_events(
        events, ledger=led,
        existing_patterns=("CRITICAL_PATTERN",),
    )
    assert out == []
    assert not (tmp_path / "ledger.jsonl").exists()


# ===========================================================================
# G — Surface validator (registered at import)
# ===========================================================================


def test_surface_validator_registered_at_import():
    """The miner module's `install_surface_validator()` must run at
    import — substrate sees it without test setup."""
    v = get_surface_validator(AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS)
    assert v is not None


def _build_proposal(
    *,
    kind="add_pattern",
    proposed_hash="sha256:abc123",
    observation_count=3,
):
    return AdaptationProposal(
        schema_version="1.0", proposal_id="p-test",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        proposal_kind=kind,
        evidence=AdaptationEvidence(
            window_days=7, observation_count=observation_count,
        ),
        current_state_hash="sha256:current",
        proposed_state_hash=proposed_hash,
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="t", proposed_at_epoch=1.0,
        operator_decision=OperatorDecisionStatus.PENDING,
    )


def test_validator_rejects_kind_other_than_add_pattern():
    p = _build_proposal(kind="raise_floor")
    verdict, detail = validate_monotonic_tightening(p)
    # raise_floor is in the universal allowlist BUT this surface's
    # validator restricts to add_pattern only.
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "kind_must_be_add_pattern" in detail


def test_validator_rejects_non_sha256_proposed_hash():
    p = _build_proposal(proposed_hash="plain_string_no_prefix")
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "proposed_hash_format" in detail


def test_validator_rejects_observation_count_below_threshold(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTATION_PATTERN_THRESHOLD", "5")
    p = _build_proposal(observation_count=2)
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "observation_count_below_threshold" in detail


def test_validator_passes_with_all_valid():
    p = _build_proposal()
    verdict, _ = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.PASSED


def test_install_surface_validator_idempotent():
    """Re-installing replaces the registration but doesn't crash."""
    install_surface_validator()
    install_surface_validator()
    v = get_surface_validator(AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS)
    assert v is not None


# ===========================================================================
# H — Authority invariants (AST grep on module source)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "semantic_guardian",
        "semantic_firewall",
        "scoped_tool_backend",
        ".gate.",
        "phase_runners",
        "providers",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
        elif isinstance(node, _ast.Import):
            for n in node.names:
                for sub in banned_substrings:
                    if sub in n.name:
                        found_banned.append((n.name, sub))
    assert not found_banned, (
        f"semantic_guardian_miner.py contains banned imports: "
        f"{found_banned}"
    )


def test_module_imports_only_substrate_and_stdlib():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    stdlib_prefixes = (
        "__future__",
        "hashlib", "logging", "os", "dataclasses", "typing",
    )
    allowed_governance = (
        "backend.core.ouroboros.governance.adaptation.ledger",
    )
    for node in tree.body:
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            ok = (
                any(mod == p or mod.startswith(p + ".") for p in stdlib_prefixes)
                or mod in allowed_governance
            )
            assert ok, f"unauthorized import {mod!r}"
        elif isinstance(node, _ast.Import):
            for n in node.names:
                ok = (
                    any(n.name == p or n.name.startswith(p + ".")
                        for p in stdlib_prefixes)
                    or n.name in allowed_governance
                )
                assert ok, f"unauthorized import {n.name!r}"


def test_module_does_not_call_subprocess_or_network():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found


def test_module_does_not_call_llm():
    """Cage check (Pass C §4.4): zero LLM in the cage."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    # The miner must NOT mention any provider / Anthropic / Claude
    # construction. Docstrings can mention them as background; we
    # source-grep for active usage tokens.
    forbidden_tokens = (
        "messages.create(",
        ".generate_completion(",
        "anthropic.Anthropic(",
        "ClaudeProvider(",
        "from openai",
    )
    found = [tok for tok in forbidden_tokens if tok in src]
    assert not found, (
        f"semantic_guardian_miner.py contains LLM-call tokens: {found}"
    )


# ===========================================================================
# I — End-to-end integration with the substrate
# ===========================================================================


def test_full_pipeline_proposal_passes_substrate_validator(
    monkeypatch, tmp_path,
):
    """The substrate's universal validator MUST accept what the
    miner produces — the surface validator + universal default work
    together."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1")
    led = _ledger(tmp_path)
    events = [
        _ev(op_id=f"op-{i}",
            code_snippet_excerpt="CRITICAL_PATTERN_XYZABC" + str(i))
        for i in range(3)
    ]
    out = propose_patterns_from_events(events, ledger=led)
    assert len(out) == 1
    assert out[0].status is ProposeStatus.OK
    p = led.get(out[0].proposal_id)
    assert p is not None
    assert p.monotonic_tightening_verdict is MonotonicTighteningVerdict.PASSED


def test_proposal_id_stable_across_calls():
    """Same group + same pattern → same proposal_id (idempotency
    foundation)."""
    p1 = MinedPatternProposal(
        group_key=("rc", "fc"), proposed_pattern="ABCDEFGH",
        excerpt_count=3, source_event_ids=("op-1",), summary="s",
    )
    p2 = MinedPatternProposal(
        group_key=("rc", "fc"), proposed_pattern="ABCDEFGH",
        excerpt_count=99, source_event_ids=("op-x",), summary="different",
    )
    assert p1.proposal_id() == p2.proposal_id()


def test_proposal_id_differs_for_different_pattern():
    p1 = MinedPatternProposal(
        group_key=("rc", "fc"), proposed_pattern="ABCDEFGH",
        excerpt_count=3, source_event_ids=(), summary="",
    )
    p2 = MinedPatternProposal(
        group_key=("rc", "fc"), proposed_pattern="ABCDEFGI",
        excerpt_count=3, source_event_ids=(), summary="",
    )
    assert p1.proposal_id() != p2.proposal_id()
