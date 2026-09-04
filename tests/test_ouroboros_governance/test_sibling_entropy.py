"""Siblings must differ in LOGIC, and "differ" must be measured correctly.

Two failures produced this module, and both are pinned here.

**The measurement failure.** ``difflib.SequenceMatcher`` defaults to
``autojunk=True``: in a string longer than 200 chars, any character
appearing in more than 1% of it is treated as junk and skipped. An
``ast.dump`` is thousands of chars of ``(``, ``)``, ``=``, ``'`` and
``,``, so nearly everything is junk and the ratio collapses. Real pairs
from the live corpus measured 0.3186 with autojunk and 0.9595 without.
A session read the first number as "the siblings are 67% structurally
different", concluded the GRPO reward was broken for scoring them within
0.0003, and came within one step of refactoring a correct reward
function. The reward was right; the ruler was wrong.

**The generation failure.** Every sibling was drawn at the same
hardcoded ``temperature=0.2``, and dedup compared ``candidate_hash`` --
exact bytes. Near-deterministic sampling plus an exact-equality filter
means near-duplicates are generated AND accepted: 8 shipped rows in the
corpus carried 3 structurally distinct answers, and all three groups
collapsed to one fingerprint, so not one preference pair was
constructible.
"""

from __future__ import annotations

import difflib

import pytest

from backend.core.ouroboros.governance import sibling_entropy as se

_ENV_ON = "JARVIS_SIBLING_ENTROPY_ENABLED"
_ENV_THRESH = "JARVIS_SIBLING_DIVERSITY_THRESHOLD"
_ENV_RESAMPLE = "JARVIS_SIBLING_MAX_RESAMPLE"


# --------------------------------------------------------------------------
# The ruler
# --------------------------------------------------------------------------


def test_autojunk_is_the_bug_and_we_do_not_have_it() -> None:
    """THE regression this module exists to prevent re-introducing.

    Built from the shape that actually broke it: a long, highly
    repetitive string of AST-dump punctuation. With ``autojunk=True``
    difflib calls two nearly-identical strings wildly different; the
    module must never report that number.
    """
    a = "Module(body=[FunctionDef(name='run', args=arguments(args=[])), " * 40
    b = a.replace("name='run'", "name='go'", 1)

    naive = difflib.SequenceMatcher(None, a, b).ratio()
    honest = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

    # The trap is real on this input...
    assert naive < honest, "fixture no longer exercises the autojunk trap"
    # ...and the module reports the honest number, not the trapped one.
    assert se.structural_similarity(a, b) == pytest.approx(honest)
    assert se.structural_similarity(a, b) > 0.95


def test_identical_fingerprints_are_exactly_one() -> None:
    """Not 0.999. Callers compare against a threshold; drift here moves it."""
    assert se.structural_similarity("x" * 500, "x" * 500) == 1.0


def test_unparseable_is_dissimilar_not_duplicate() -> None:
    """A candidate that will not parse is a real (bad) answer.

    Treating None as "similar" would let the diversity filter silently
    discard every broken candidate, hiding a failure mode the verifier
    grades in its syntax band and the corpus needs to see.
    """
    assert se.structural_similarity(None, "anything") == 0.0
    assert se.structural_similarity(None, None) == 0.0
    redundant, _ = se.is_structurally_redundant([], ["fp"])
    assert redundant is False


# --------------------------------------------------------------------------
# Docstrings are presentation, not logic
# --------------------------------------------------------------------------


_DOC_A = '''
def run(x):
    """Return the doubled value including the offset."""
    return x * 2
'''

_DOC_B = '''
def run(x):
    """Return the doubled value with the offset."""
    return x * 2
'''

_LOGIC_C = '''
def run(x):
    total = 0
    for i in range(x):
        total += i
    return total
'''


def test_a_reworded_docstring_is_not_a_new_answer() -> None:
    """The exact shape of the corpus duplicates.

    The measured siblings differed by words inside a docstring. If the
    fingerprint kept docstrings these would read as distinct and the
    group would keep collapsing.
    """
    fp_a, fp_b = se.structural_fingerprint(_DOC_A), se.structural_fingerprint(_DOC_B)
    assert fp_a is not None and fp_a == fp_b
    redundant, peak = se.is_structurally_redundant([fp_b], [fp_a])
    assert redundant is True and peak == 1.0


def test_different_control_flow_is_a_new_answer() -> None:
    fp_a, fp_c = se.structural_fingerprint(_DOC_A), se.structural_fingerprint(_LOGIC_C)
    redundant, peak = se.is_structurally_redundant([fp_c], [fp_a])
    assert redundant is False
    assert peak < se.diversity_threshold()


def test_syntax_error_fingerprints_as_none() -> None:
    assert se.structural_fingerprint("def broken(:\n") is None
    assert se.structural_fingerprint("   ") is None


def test_partial_overlap_in_a_multi_file_draw_survives() -> None:
    """Repeating one file while rewriting another is genuinely new.

    Redundancy requires EVERY file to be redundant, or a multi-file
    candidate would be thrown away for the file it legitimately shares.
    """
    fp_a = se.structural_fingerprint(_DOC_A)
    fp_c = se.structural_fingerprint(_LOGIC_C)
    redundant, _ = se.is_structurally_redundant([fp_a, fp_c], [fp_a])
    assert redundant is False


def test_distinct_structure_count_sees_through_docstrings() -> None:
    cands = [
        {"full_content": _DOC_A}, {"full_content": _DOC_B},
        {"full_content": _LOGIC_C},
    ]
    # Three rows, two answers -- the number the corpus actually has.
    assert se.distinct_structure_count(cands) == 2


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------


def test_first_draw_is_untouched_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """An op that never draws a sibling must be byte-identical.

    The first candidate is the one the op would have produced anyway;
    only the BONUS draws are allowed to explore.
    """
    monkeypatch.delenv(_ENV_ON, raising=False)
    s = se.sampling_for(1, op_id="op-1")
    assert s.is_legacy is True
    assert s.temperature == 0.2
    assert s.config_overrides() == {}


def test_later_draws_move_temperature_and_the_window_together() -> None:
    """Temperature alone re-weights a tail that top_k/top_p already cut."""
    second, third = se.sampling_for(2, op_id="o"), se.sampling_for(3, op_id="o")
    assert third.temperature > second.temperature
    assert third.top_k > second.top_k
    assert third.top_p < second.top_p
    for s in (second, third):
        assert not s.is_legacy
        assert set(s.config_overrides()) == {
            "top_p", "top_k", "repeat_penalty", "seed",
        }


def test_escalation_leaves_the_region_it_already_exhausted() -> None:
    """A redundant draw means THIS region is spent; sitting in it re-draws it."""
    base = se.sampling_for(2, escalation=0, op_id="o")
    esc1 = se.sampling_for(2, escalation=1, op_id="o")
    esc2 = se.sampling_for(2, escalation=2, op_id="o")
    assert base.temperature < esc1.temperature < esc2.temperature
    assert base.seed != esc1.seed != esc2.seed


def test_temperature_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the ceiling a coder model stops emitting parseable Python.

    An unparseable sibling is worth LESS than a redundant one: it cannot
    reach the substance tier at all.
    """
    monkeypatch.setenv("JARVIS_SIBLING_TEMP_CEILING", "0.9")
    for esc in range(0, 12):
        assert se.sampling_for(4, escalation=esc, op_id="o").temperature <= 0.9


def test_seeds_are_distinct_per_draw_but_reproducible() -> None:
    """An engine reusing one seed replays one trajectory at any temperature.

    Distinct or the knob looks wired and changes nothing; reproducible or
    a soak cannot be bisected.
    """
    seeds = [se.sampling_for(d, op_id="op-A").seed for d in range(2, 8)]
    assert len(set(seeds)) == len(seeds)
    assert seeds == [se.sampling_for(d, op_id="op-A").seed for d in range(2, 8)]
    assert se.sampling_for(2, op_id="op-A").seed != se.sampling_for(2, op_id="op-B").seed


def test_master_switch_restores_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_ON, "false")
    for draw in range(1, 6):
        assert se.sampling_for(draw, escalation=2, op_id="o").is_legacy is True


@pytest.mark.parametrize(
    ("raw", "expect"), [("0", 0), ("1", 1), ("3", 3), ("99", 3), ("-1", 0), ("x", 1)],
)
def test_resample_attempts_are_clamped(
    monkeypatch: pytest.MonkeyPatch, raw: str, expect: int,
) -> None:
    """Every re-draw costs a full generation out of the op's slack."""
    monkeypatch.setenv(_ENV_RESAMPLE, raw)
    assert se.max_resample_attempts() == expect


@pytest.mark.parametrize(("raw", "expect"), [("0.5", 0.5), ("2", 1.0), ("-1", 0.0)])
def test_threshold_is_clamped_to_a_ratio(
    monkeypatch: pytest.MonkeyPatch, raw: str, expect: float,
) -> None:
    monkeypatch.setenv(_ENV_THRESH, raw)
    assert se.diversity_threshold() == expect


def test_candidate_source_reads_the_envelope_fields() -> None:
    assert se.candidate_source({"full_content": "a"}) == "a"
    assert se.candidate_source({"diff": "d"}) == "d"
    assert se.candidate_source({"candidate_hash": "h"}) == ""
    assert se.candidate_source("not a dict") == ""


# --------------------------------------------------------------------------
# The escalation multiplier: leave an exhausted region, do not step in it
# --------------------------------------------------------------------------


def test_multiplier_off_is_byte_identical(monkeypatch) -> None:
    """Default 1.0: a collapse streak changes NOTHING about the schedule."""
    monkeypatch.delenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", raising=False)
    assert se.escalation_multiplier() == 1.0
    for streak in (0, 1, 2, 5):
        assert se.collapse_bump(streak) == 0
        assert se.sampling_for(3, op_id="o", collapse_streak=streak) == \
            se.sampling_for(3, op_id="o")


def test_multiplier_climbs_exponentially_with_the_streak(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", "2.0")
    assert [se.collapse_bump(k) for k in (0, 1, 2, 3)] == [0, 1, 3, 7]
    base = se.sampling_for(2, op_id="o")
    one = se.sampling_for(2, op_id="o", collapse_streak=1)
    two = se.sampling_for(2, op_id="o", collapse_streak=2)
    assert base.temperature < one.temperature <= two.temperature
    assert len({base.seed, one.seed, two.seed}) == 3, "a jump must move the seed"


def test_multiplier_is_bounded_by_the_ceiling(monkeypatch) -> None:
    """Exponential in rungs, never in temperature: the ceiling holds."""
    monkeypatch.setenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", "4.0")
    monkeypatch.setenv("JARVIS_SIBLING_TEMP_CEILING", "1.15")
    for streak in (1, 2, 3, 10, 100):
        assert se.sampling_for(2, op_id="o", collapse_streak=streak).temperature <= 1.15


def test_multiplier_is_clamped(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", "0.2")
    assert se.escalation_multiplier() == 1.0
    monkeypatch.setenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", "99")
    assert se.escalation_multiplier() == 4.0
    monkeypatch.setenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", "banana")
    assert se.escalation_multiplier() == 1.0


def test_multiplier_never_touches_draw_one_or_entropy_off(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SIBLING_ESCALATION_MULTIPLIER", "3.0")
    assert se.sampling_for(1, op_id="o", collapse_streak=4).is_legacy
    monkeypatch.setenv("JARVIS_SIBLING_ENTROPY_ENABLED", "false")
    assert se.sampling_for(3, op_id="o", collapse_streak=4).is_legacy
