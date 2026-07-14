"""Task #7 — close the two live SemanticGuardian bypasses (F4 + F8).

F8: test_assertion_inverted only caught NEGATION flips (assert x → assert not x),
    so a VALUE rewrite (assert x == 5 → assert x == 6) — the working attack shape,
    weaken a test's expected value — was invisible. Closed by
    test_assertion_weakened (assertion removal/rewrite in an existing test).

F4: inspect_batch looped inspect() per file, so a COMPOSED attack (file A removes
    a construct, file B depends on it) was invisible — each file benign alone.
    Closed by _inspect_cross_file (whole-candidate cross-file dangling removal).
"""
from __future__ import annotations

from backend.core.ouroboros.governance.semantic_guardian import (
    SemanticGuardian,
    recommend_tier_floor,
)


def _g() -> SemanticGuardian:
    return SemanticGuardian()


# ── F8: assertion weakening (value-rewrite / removal) ─────────────────

def test_value_rewrite_of_existing_assertion_fires():
    """THE F8 attack shape: change a test's expected value so a bad change
    passes. Previously invisible; now HARD → approval_required."""
    old = "def test_x():\n    assert compute() == 5\n"
    new = "def test_x():\n    assert compute() == 6\n"
    f = _g().inspect(file_path="tests/test_x.py", old_content=old, new_content=new)
    assert any(d.pattern == "test_assertion_weakened" and d.severity == "hard" for d in f)
    assert recommend_tier_floor(f) == "approval_required"


def test_assertion_deletion_fires():
    old = "def test_x():\n    assert a == 1\n    assert b == 2\n"
    new = "def test_x():\n    assert a == 1\n"  # dropped the b check
    f = _g().inspect(file_path="tests/test_x.py", old_content=old, new_content=new)
    assert any(d.pattern == "test_assertion_weakened" for d in f)


def test_negation_flip_still_caught():
    """The weakened detector subsumes the negation case (removal of the old
    polarity signature)."""
    old = "def test_x():\n    assert ok\n"
    new = "def test_x():\n    assert not ok\n"
    f = _g().inspect(file_path="tests/test_x.py", old_content=old, new_content=new)
    assert any(
        d.pattern in ("test_assertion_weakened", "test_assertion_inverted")
        for d in f
    )


def test_adding_assertions_does_not_fire():
    """Strengthening a test (adding checks) must NEVER escalate."""
    old = "def test_y():\n    assert a == 1\n"
    new = "def test_y():\n    assert a == 1\n    assert b == 2\n"
    f = _g().inspect(file_path="tests/test_y.py", old_content=old, new_content=new)
    assert not any(d.pattern == "test_assertion_weakened" for d in f)


def test_non_test_file_never_fires():
    old = "def helper():\n    assert x == 5\n"
    new = "def helper():\n    assert x == 6\n"
    f = _g().inspect(file_path="src/helper.py", old_content=old, new_content=new)
    assert not any(d.pattern == "test_assertion_weakened" for d in f)


def test_reformatting_an_assertion_does_not_fire():
    """ast.unparse normalizes formatting, so a whitespace-only change to the
    same assertion must not FP."""
    old = "def test_x():\n    assert x==5\n"
    new = "def test_x():\n    assert x == 5\n"
    f = _g().inspect(file_path="tests/test_x.py", old_content=old, new_content=new)
    assert not any(d.pattern == "test_assertion_weakened" for d in f)


# ── F4: cross-file composition ────────────────────────────────────────

def test_cross_file_dangling_removal_fires():
    """THE F4 attack shape: file A removes a guard, file B still calls it.
    Each file benign alone; the composition is HARD → approval_required."""
    a_old = (
        "def _require_auth():\n    assert authorized\n\n"
        "def handler():\n    _require_auth()\n    return 1\n"
    )
    a_new = "def handler():\n    return 1\n"  # _require_auth removed
    b_new = "def run():\n    _require_auth()\n    return 0\n"  # still calls it, unbound
    cands = [
        ("mod_a.py", a_old, a_new),
        ("mod_b.py", "def run():\n    return 0\n", b_new),
    ]
    f = _g().inspect_batch(cands)
    assert any(
        d.pattern == "cross_file_dangling_removal" and d.severity == "hard"
        for d in f
    )
    assert recommend_tier_floor(f) == "approval_required"


def test_single_file_removal_no_cross_file_fp():
    """A single-file removal is the per-file detector's job — the cross-file
    pass must not fire (needs ≥2 files)."""
    a_old = "def g():\n    return 1\n\ndef h():\n    return g()\n"
    a_new = "def h():\n    return g()\n"  # g removed, still used same-file
    f = _g().inspect_batch([("mod_a.py", a_old, a_new)])
    assert not any(d.pattern == "cross_file_dangling_removal" for d in f)


def test_no_fp_when_symbol_locally_bound_in_referencing_file():
    """If file B defines/imports the symbol itself, the reference resolves
    locally — not a dangling cross-file removal."""
    a_old = "def shared():\n    return 1\n"
    a_new = "x = 1\n"  # shared removed in A
    b_new = "def shared():\n    return 2\n\ndef run():\n    return shared()\n"  # B has its own
    cands = [("mod_a.py", a_old, a_new), ("mod_b.py", "y = 0\n", b_new)]
    f = _g().inspect_batch(cands)
    assert not any(d.pattern == "cross_file_dangling_removal" for d in f)


def test_no_fp_when_symbol_readded_in_another_file():
    """Relocating a def across files (removed in A, re-added in B) must not fire
    when B binds it and references resolve."""
    a_old = "def moved():\n    return 1\n"
    a_new = "z = 1\n"
    b_new = "def moved():\n    return 1\n\ndef use():\n    return moved()\n"
    cands = [("mod_a.py", a_old, a_new), ("mod_b.py", "q = 0\n", b_new)]
    f = _g().inspect_batch(cands)
    assert not any(d.pattern == "cross_file_dangling_removal" for d in f)


def test_cross_file_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_SEMGUARD_CROSS_FILE_DANGLING_REMOVAL_ENABLED", "0")
    a_old = "def _g():\n    return 1\n"
    a_new = "x = 1\n"
    b_new = "def run():\n    return _g()\n"
    cands = [("mod_a.py", a_old, a_new), ("mod_b.py", "y=0\n", b_new)]
    f = _g().inspect_batch(cands)
    assert not any(d.pattern == "cross_file_dangling_removal" for d in f)
