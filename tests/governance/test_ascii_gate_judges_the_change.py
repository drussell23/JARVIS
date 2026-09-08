"""The ASCII gate judges what the MODEL wrote, against the on-disk original.

The first landed production goal (3a7d155218, 2026-09-08) was a 2b.1-diff:
three hunks applied onto the original file. The gate then auto-repaired the
WHOLE candidate and rewrote 50 em-dash / section-sign / arrow lines the file
had carried for months. With the original in hand, only lines the original
does not contain are repaired or judged.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.ascii_strict_gate import AsciiStrictGate, repair_content

ORIGINAL = (
    '"""DW Capacity Probe — Slice 34 substrate (Phase 0).\n'
    'operator binding §48.7.2: *"Isolate the variable — is it the harness?"*\n'
    '"""\n'
    "def tracer_enabled():\n"
    "    return True\n"
)


def test_repair_leaves_the_originals_unicode_alone_and_heals_the_models():
    candidate = ORIGINAL + "def _tracer_auth_recheck_s():\n    # twenty times the timeout — derived\n    return 20.0\n"
    fixed, n = repair_content(candidate, ORIGINAL)
    assert n == 1
    assert fixed.startswith(ORIGINAL), "every original line is byte-identical"
    assert "# twenty times the timeout - derived" in fixed
    legacy, n_legacy = repair_content(candidate)
    assert n_legacy == 4 and "Slice 34" in legacy and "—" not in legacy


def test_check_with_the_original_passes_and_repairs_only_the_new_line():
    gate = AsciiStrictGate(enabled=True, auto_repair=True)
    cand = {"file_path": "probe.py", "full_content": ORIGINAL + "def f():\n    return 'a — b'\n"}
    ok, reason, offenders = gate.check(cand, original=ORIGINAL)
    assert ok and reason is None and offenders == []
    assert cand["full_content"].startswith(ORIGINAL)
    assert "return 'a - b'" in cand["full_content"]
    assert cand["_ascii_repair_count"] == 1


def test_a_unicode_identifier_the_model_introduced_is_still_rejected():
    gate = AsciiStrictGate(enabled=True, auto_repair=True)
    cand = {"file_path": "probe.py", "full_content": ORIGINAL + "import rapid\u0641uzz\n"}
    ok, reason, offenders = gate.check(cand, original=ORIGINAL)
    assert not ok and offenders and "ascii_corruption" in (reason or "")


def test_without_an_original_the_gate_behaves_as_before():
    gate = AsciiStrictGate(enabled=True, auto_repair=True)
    cand = {"file_path": "probe.py", "full_content": "x = 1  # a — b\n"}
    ok, _r, _o = gate.check(cand)
    assert ok and cand["full_content"] == "x = 1  # a - b\n"


def test_the_orchestrator_hands_the_gate_the_original():
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    src = inspect.getsource(orchestrator)
    assert "_ascii_gate.check(\n                                _cand, original=self._original_text_for(_cand)," in src
