"""Regression spine for path-scoped operator rules.

Two halves, and the second is the one with teeth:

* **Delivery** — the injection `user_preference_memory`'s docstring described
  ("StrategicDirection accepts a ``user_prefs`` param... filtered by
  relevance to the op") and which never existed. Operator rules could BLOCK a
  write and be LEARNED from a rejection, but could never GUIDE a generation.
* **The widening invariant** — ``matches_path`` is a SECURITY path consulted
  before every mutating tool call. Glob support is a UNION with the legacy
  substring test, never a replacement, because replacing it would have
  quietly unprotected every entry written in the old style.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.operator_rules import (
    RuleMatch,
    _pattern_specificity,
    compose_for_op,
    match_pattern,
    render_rule,
    score_rule,
    select_rules,
)
from backend.core.ouroboros.governance.user_preference_memory import (
    MemoryType,
    UserMemory,
)


def _mem(name="r", paths=(), mtype=MemoryType.FEEDBACK, tags=(),
         desc="a rule", why="", how=""):
    return UserMemory(
        id=f"{mtype.value}_{name}", type=mtype, name=name, description=desc,
        content="body", why=why, how_to_apply=how, tags=tuple(tags),
        paths=tuple(paths),
    )


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,path,expected", [
    # Directory-prefix shorthand — what operators actually write.
    ("backend/voice", "backend/voice/x.py", True),
    ("backend/voice", "backend/voice", True),
    ("backend/voice", "backend/voices/x.py", False),
    ("backend/voice", "backend/other/x.py", False),
    # Recursive globs.
    ("backend/**/*.py", "backend/core/a.py", True),
    ("backend/**/*.py", "backend/a.py", True),
    ("backend/**/*.py", "backend/core/a.ts", False),
    ("backend/**", "backend/anything/at/all", True),
    # Plain fnmatch.
    ("*.md", "README.md", True),
    ("*.md", "docs/README.md", False),
    ("docs/*.md", "docs/README.md", True),
])
def test_pattern_matching(pattern, path, expected):
    assert match_pattern(pattern, path) is expected


def test_matching_is_case_sensitive_because_git_is():
    """macOS is case-insensitive; the repository is not, and it is the
    authority everywhere else in this arc."""
    assert match_pattern("backend/Voice", "backend/voice/x.py") is False


def test_empty_inputs_never_match():
    assert match_pattern("", "a.py") is False
    assert match_pattern("a.py", "") is False


def test_specificity_rises_with_committed_segments():
    assert (_pattern_specificity("**")
            < _pattern_specificity("backend/**")
            < _pattern_specificity("backend/core/**")
            < _pattern_specificity("backend/core/ouroboros/governance"))


# ---------------------------------------------------------------------------
# The security-widening invariant
# ---------------------------------------------------------------------------


def test_legacy_substring_semantics_still_protect():
    """The load-bearing test.

    Pure fnmatch would make ``backend/voice`` stop matching
    ``backend/voice/x.py`` for anyone who wrote it the old way. A guard may
    be widened; it must never be narrowed as a side effect of improving it.
    """
    mem = _mem(paths=("backend/voice",), mtype=MemoryType.FORBIDDEN_PATH)
    assert mem.matches_path("backend/voice/x.py") is True
    assert mem.matches_path("/abs/repo/backend/voice/deep/y.py") is True


def test_glob_now_also_protects():
    mem = _mem(paths=("backend/**/*.key",), mtype=MemoryType.FORBIDDEN_PATH)
    assert mem.matches_path("backend/secrets/prod.key") is True


def test_widening_is_a_strict_superset():
    """Anything the substring test matched must still match."""
    for pattern, path in (
        ("voice", "backend/voice/x.py"),
        ("core/ouroboros", "backend/core/ouroboros/a.py"),
        (".env", "config/.env.local"),
    ):
        assert _mem(paths=(pattern,)).matches_path(path) is True


def test_matches_path_never_raises_on_garbage():
    assert _mem(paths=("[",)).matches_path("a.py") in (True, False)
    assert _mem(paths=()).matches_path("a.py") is False
    assert _mem(paths=("a",)).matches_path("") is False


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def test_a_rule_outside_the_op_does_not_apply():
    """The whole point. A voice rule in an orchestrator prompt is noise
    indistinguishable from the ghost topics this arc removed."""
    rule = _mem(paths=("backend/voice/**",))
    assert score_rule(rule, ["backend/core/ouroboros/orchestrator.py"]) is None


def test_a_rule_on_the_op_applies():
    rule = _mem(paths=("backend/core/ouroboros/**",))
    match = score_rule(rule, ["backend/core/ouroboros/orchestrator.py"])
    assert match is not None
    assert match.matched_files == ("backend/core/ouroboros/orchestrator.py",)


def test_unscoped_rule_is_global_but_outranked_by_a_scoped_one():
    """CC semantics: a rule with no paths applies everywhere. It should still
    lose the budget to a rule that demonstrably knows about THIS op."""
    glob_rule = _mem(name="global")
    scoped = _mem(name="scoped", paths=("backend/core/**",))
    files = ["backend/core/a.py"]
    g = score_rule(glob_rule, files)
    s = score_rule(scoped, files)
    assert g is not None and g.is_global is True
    assert s is not None and s.is_global is False
    assert s.score > g.score


def test_global_rules_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATOR_RULES_GLOBAL", "0")
    assert score_rule(_mem(), ["a.py"]) is None


def test_negation_excludes_a_subtree():
    """`src/**` except the vendored tree — without negation an operator has
    to enumerate the complement by hand and keep it current."""
    rule = _mem(paths=("backend/**", "!backend/vendor/**"))
    assert score_rule(rule, ["backend/core/a.py"]) is not None
    assert score_rule(rule, ["backend/vendor/lib.py"]) is None


def test_absolute_target_files_are_relativised(tmp_path):
    """The same rule must fire whether a caller spelled the path absolute
    or relative."""
    (tmp_path / "backend").mkdir()
    target = tmp_path / "backend" / "a.py"
    target.write_text("x=1")
    rule = _mem(paths=("backend/**",))
    assert score_rule(rule, [str(target)], project_root=tmp_path) is not None


def test_deeper_scope_outranks_shallower():
    files = ["backend/core/ouroboros/governance/orchestrator.py"]
    broad = score_rule(_mem(name="b", paths=("backend/**",)), files)
    tight = score_rule(
        _mem(name="t", paths=("backend/core/ouroboros/governance/**",)), files)
    assert tight.score > broad.score


def test_op_with_no_target_files_gets_only_global_rules():
    assert score_rule(_mem(paths=("backend/**",)), []) is None
    assert score_rule(_mem(), []) is not None


def test_type_weight_orders_actionable_kinds_first():
    files = ["a.py"]
    feedback = score_rule(_mem(name="f", mtype=MemoryType.FEEDBACK), files)
    reference = score_rule(_mem(name="r", mtype=MemoryType.REFERENCE), files)
    assert feedback.score > reference.score


# ---------------------------------------------------------------------------
# Selection, budget, and staleness
# ---------------------------------------------------------------------------


def test_selection_is_bounded_and_records_why(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATOR_RULES_MAX", "2")
    rules = [_mem(name=f"r{i}", paths=("backend/**",)) for i in range(5)]
    sel = select_rules(rules, ["backend/a.py"])
    assert len(sel.selected) == 2
    assert len(sel.withheld) == 3
    assert all(why == "max_rules" for _, why in sel.withheld)


def test_char_budget_withholds_rather_than_truncating():
    big = _mem(name="big", paths=("backend/**",), desc="x" * 400)
    rules = [big, _mem(name="b2", paths=("backend/**",), desc="y" * 400)]
    sel = select_rules(rules, ["backend/a.py"], char_budget=300)
    assert len(sel.selected) == 1  # first always admitted, second withheld
    assert sel.withheld[0][1] == "budget"


def test_orphaned_rule_is_withheld_not_silently_obeyed():
    """A rule scoped to paths that no longer exist describes a repo shape
    that is gone — the same fact Drift.ORPHANED reports for a topic."""
    rule = _mem(paths=("backend/deleted_thing/**",))
    sel = select_rules(rule and [rule], ["backend/deleted_thing/a.py"],
                       repo_has=lambda p: False)
    assert sel.selected == ()
    assert sel.withheld[0][1] == "orphaned"


def test_repo_probe_failure_fails_open():
    """An unreadable tree must not discard a live rule as stale."""
    def boom(_):
        raise OSError("nope")
    rule = _mem(paths=("backend/**",))
    match = score_rule(rule, ["backend/a.py"], repo_has=boom)
    assert match is not None and match.orphaned is False


def test_selection_is_deterministic():
    rules = [_mem(name=f"r{i}", paths=("backend/**",)) for i in range(6)]
    a = select_rules(rules, ["backend/a.py"])
    b = select_rules(list(reversed(rules)), ["backend/a.py"])
    assert [m.memory.name for m in a.selected] == [
        m.memory.name for m in b.selected]


def test_disabled_returns_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATOR_RULES_ENABLED", "0")
    assert select_rules([_mem()], ["a.py"]).section == ""


def test_render_carries_the_reason_not_just_the_order():
    """A rule without its reason is an order, and an order the model cannot
    evaluate is one it misapplies at the first unforeseen edge case."""
    out = render_rule(_mem(paths=("backend/**",), why="it broke prod",
                           how="prefer the async path"))
    assert "it broke prod" in out and "prefer the async path" in out
    assert "backend/**" in out


def test_section_is_absent_when_nothing_applies():
    sel = select_rules([_mem(paths=("other/**",))], ["backend/a.py"])
    assert sel.section == ""


# ---------------------------------------------------------------------------
# The delivery that never existed
# ---------------------------------------------------------------------------


def test_selection_lands_on_the_shared_admission_ledger():
    """Rules ride the ledger topics use. An operator asking "what was in that
    prompt" must not have to know which of two surfaces to consult."""
    from backend.core.ouroboros.governance.memory_admission import (
        latest_record, reset_default_registry,
    )
    from backend.core.ouroboros.governance.operator_rules import (
        record_selection,
    )
    reset_default_registry()
    sel = select_rules(
        [_mem(name="in", paths=("backend/**",)),
         _mem(name="out", paths=("other/**",))],
        ["backend/a.py"])
    record_selection(sel, op_id="op-rules", query="q")
    rec = latest_record()
    assert rec is not None and rec.op_id == "op-rules"
    assert rec.corpus_provenance == "operator_rules"
    assert rec.admitted_count == 1
    assert rec.rows[0].uri == "rule:in"


def test_compose_for_op_is_the_one_entry_point(tmp_path, monkeypatch):
    """Composition records as a side effect, so a caller cannot inject rules
    without the injection being observable."""
    from backend.core.ouroboros.governance.memory_admission import (
        latest_record, reset_default_registry,
    )
    from backend.core.ouroboros.governance import user_preference_memory as upm

    reset_default_registry()
    upm.reset_default_store()
    # The directory must exist: the orphan probe is real, and a rule scoped
    # to a path absent from the repo is correctly withheld as stale. Building
    # the fixture without it would have tested the wrong branch.
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "a.py").write_text("x = 1\n")
    store = upm.get_default_store(tmp_path)
    store.add(memory_type=MemoryType.FEEDBACK, name="async-first",
              description="no blocking calls on the loop",
              content="body", paths=("backend/**",))
    try:
        section = compose_for_op(tmp_path, ["backend/a.py"], "fix the loop",
                                 op_id="op-compose")
        assert "## Operator Rules" in section
        assert "async-first" in section
        assert latest_record().op_id == "op-compose"
    finally:
        upm.reset_default_store()


def test_compose_is_fail_soft_on_a_broken_store(tmp_path):
    assert compose_for_op(tmp_path / "nonexistent", ["a.py"], "q") in ("",)


def test_injection_is_wired_on_both_context_expansion_paths():
    """The inline orchestrator twin and the extracted runner are duplicated
    by design; a capability on one is a capability that vanishes when the
    kill-switch flips."""
    root = Path(__file__).resolve().parents[2] / "backend/core/ouroboros/governance"
    runner = (root / "phase_runners" / "context_expansion_runner.py").read_text()
    inline = (root / "orchestrator.py").read_text()
    assert "operator_rules" in runner
    assert "operator_rules" in inline
