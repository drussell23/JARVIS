"""The instrument that counts tests which can only ever pass.

Every case below that names a real test in this repository is a regression pin
for a false positive the detector actually produced. The rate moved
**3.20% → 2.74% → 1.71%** across three corrections, and each correction was
found by reading flagged tests rather than by any checker — the same way the
`f_glyph(` artefact and the severed comment block were found.

The three false-positive classes, in the order they were made:

1. ``with PtySession(...) as s`` / ``with console.capture() as cap`` — binding
   EVERY context-manager target as source text. Runtime capture is not source.
2. bare ``.read()`` — a PTY drain, socket, or subprocess pipe is runtime
   OUTPUT, and asserting on it is behavioural.
3. ``state_file.read_text()`` — a JSON artefact the test itself wrote, parsed
   and asserted on. That observes an effect of the code under test.

The counterexamples matter as much as the examples: this file is pinned by the
two confirmed TRUE positives, so a future "fix" that suppresses the false
positives by suppressing everything fails here.
"""
from __future__ import annotations

import ast

import pytest

from backend.core.ouroboros.battle_test import source_assertion_audit as saa
from backend.core.ouroboros.battle_test.source_assertion_audit import TestKind


def _classify_src(body: str) -> TestKind:
    """Classify the first test function in a source snippet."""
    tree = ast.parse(body)
    fn = next(iter(saa._test_functions(tree)))
    v = saa.classify(fn, "snippet.py")
    assert v is not None
    return v.kind


class TestTheFourKinds:
    def test_a_pure_source_assertion_is_SOURCE_ONLY(self):
        assert _classify_src(
            'import inspect\n'
            'def test_x():\n'
            '    src = inspect.getsource(mod.fn)\n'
            '    assert "time.sleep" not in src\n'
        ) is TestKind.SOURCE_ONLY

    def test_a_behavioural_assertion_is_BEHAVIOURAL(self):
        assert _classify_src(
            'def test_x():\n'
            '    assert compute(2) == 4\n'
        ) is TestKind.BEHAVIOURAL

    def test_source_plus_behaviour_is_a_STRUCTURAL_PIN(self):
        """The legitimate use. A source assertion that BACKS a behavioural one
        is evidence about shape, not a substitute for evidence about effect."""
        assert _classify_src(
            'import inspect\n'
            'def test_x():\n'
            '    assert compute(2) == 4\n'
            '    src = inspect.getsource(mod.compute)\n'
            '    assert "cache" in src\n'
        ) is TestKind.STRUCTURAL_PIN

    def test_no_assertion_is_its_own_class(self):
        """A different defect. Counting it in the rate would inflate this one."""
        assert _classify_src(
            'def test_x():\n'
            '    do_something()\n'
        ) is TestKind.NO_ASSERTION


class TestTheFalsePositivesItActuallyMade:
    def test_a_context_manager_target_is_not_source(self):
        """FP class 1 — `with PtySession(...) as s` made every PTY test look
        like a source assertion."""
        assert _classify_src(
            'def test_x():\n'
            '    with PtySession(["cmd"]) as s:\n'
            '        assert s.wait_for("READY", timeout=20), s.output()\n'
        ) is TestKind.BEHAVIOURAL

    def test_a_rich_capture_is_not_source(self):
        """FP class 1, second shape — asserting on captured render output is
        as behavioural as a test gets."""
        assert _classify_src(
            'def test_x():\n'
            '    with console.capture() as cap:\n'
            '        console.print(r.render_frame())\n'
            '    assert "live" in cap.get().lower()\n'
        ) is TestKind.BEHAVIOURAL

    def test_a_bare_read_is_not_source(self):
        """FP class 2 — a pipe/socket/PTY drain is runtime output."""
        assert _classify_src(
            'def test_x():\n'
            '    out = proc.stdout.read()\n'
            '    assert "READY" in out\n'
        ) is TestKind.BEHAVIOURAL

    def test_reading_a_data_artifact_is_not_source(self):
        """FP class 3 — the test WROTE this file; reading it back observes an
        effect. `test_crash_recovery_persists_reset` was mis-flagged here."""
        assert _classify_src(
            'import json\n'
            'def test_x(state_file):\n'
            '    state_file.write_text(json.dumps({"state": "flow"}))\n'
            '    CognitiveFsm(state_file=state_file, crash_recovery=True)\n'
            '    data = json.loads(state_file.read_text())\n'
            '    assert data["state"] == "baseline"\n'
        ) is TestKind.BEHAVIOURAL

    def test_but_reading_a_MODULE_is_source(self):
        """The other side of FP class 3 — the discrimination has to keep the
        true positive. `test_no_static_label_dict_in_ui_pin` reads a `.py`
        path built from `__file__`."""
        assert _classify_src(
            'from pathlib import Path\n'
            'def test_x():\n'
            '    src = (Path(__file__).parents[2] / "backend/ui/wake.py").read_text()\n'
            '    assert "boot_governed_loop_service" not in src\n'
        ) is TestKind.SOURCE_ONLY

    def test_an_explicitly_opened_file_is_source(self):
        assert _classify_src(
            'def test_x():\n'
            '    with open("mod.py") as fh:\n'
            '        src = fh.read()\n'
            '    assert "TODO" not in src\n'
        ) is TestKind.SOURCE_ONLY


class TestLaundering:
    def test_a_nested_helper_cannot_launder_a_source_only_body(self):
        """Attributing a nested function's assertions to its parent would let
        one behavioural helper reclassify a SOURCE_ONLY test."""
        assert _classify_src(
            'import inspect\n'
            'def test_x():\n'
            '    def _check(v):\n'
            '        assert v == 4\n'
            '    src = inspect.getsource(mod.fn)\n'
            '    assert "cache" in src\n'
        ) is TestKind.SOURCE_ONLY

    def test_flag_literals_are_surfaced(self):
        """The `/narrate verbose` class: a test naming a CONTRACT it never
        exercises."""
        tree = ast.parse(
            'import inspect\n'
            'def test_x():\n'
            '    src = inspect.getsource(handler)\n'
            '    assert "JARVIS_NARRATIVE_THINKING_VERBOSE" in src\n'
        )
        v = saa.classify(next(iter(saa._test_functions(tree))), "s.py")
        assert v is not None
        assert v.kind is TestKind.SOURCE_ONLY
        assert "JARVIS_NARRATIVE_THINKING_VERBOSE" in v.flag_literals

    def test_a_lowercase_string_is_not_a_flag(self):
        tree = ast.parse(
            'import inspect\n'
            'def test_x():\n'
            '    src = inspect.getsource(handler)\n'
            '    assert "hello world" in src\n'
        )
        v = saa.classify(next(iter(saa._test_functions(tree))), "s.py")
        assert v is not None and v.flag_literals == ()


class TestItSurvivesHostileInput:
    @pytest.mark.parametrize("body", [
        'def test_x():\n    pass\n',
        'def not_a_test():\n    assert 1\n',
        'async def test_x():\n    assert await f()\n',
        'class C:\n    def test_x(self):\n        assert self.f()\n',
    ])
    def test_it_never_raises(self, body):
        tree = ast.parse(body)
        for fn in saa._test_functions(tree):
            saa.classify(fn, "s.py")   # must not raise

    def test_a_non_test_function_is_ignored(self):
        tree = ast.parse('def helper():\n    assert 1\n')
        assert list(saa._test_functions(tree)) == []

    def test_the_master_switch_yields_an_empty_reading(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SOURCE_ASSERTION_AUDIT_ENABLED", "0")
        r = saa.audit()
        assert r.total == 0 and r.scanned_files == 0


class TestTheRateItself:
    """A ratchet, in the idiom the colour and glyph audits already use."""

    #: Measured 2026-08-02 at 1.71% (930 SOURCE_ONLY of 54,375 asserting).
    #: LOWER this as tests are converted to behaviour; never raise it.
    CEILING = 0.025

    @pytest.fixture(scope="class")
    def reading(self):
        return saa.audit()

    def test_the_rate_only_falls(self, reading):
        assert reading.rate <= self.CEILING, (
            f"source-assertion rate {reading.rate:.2%} exceeds the "
            f"{self.CEILING:.2%} ceiling — {len(reading.source_only)} tests "
            f"can only ever pass, which is how a dead feature stays green")

    def test_the_audit_is_actually_measuring_something(self, reading):
        """The guard that keeps the ratchet honest: a refactor that emptied the
        scan would turn this green by measuring nothing, which is the exact
        failure mode the instrument reports."""
        assert reading.scanned_files > 1000
        assert reading.total > 10000
        assert reading.of_kind(TestKind.BEHAVIOURAL), "no behavioural tests?"

    def test_known_true_positives_are_still_caught(self, reading):
        """Anchored on real tests. If a future precision fix suppresses these,
        it suppressed the signal along with the noise."""
        names = {v.name for v in reading.source_only}
        assert "test_source_has_no_time_sleep" in names
        assert "test_no_static_label_dict_in_ui_pin" in names

    def test_known_false_positives_stay_out(self, reading):
        names = {v.name for v in reading.source_only}
        for fp in ("test_pty_makes_isatty_true_in_the_child",
                   "test_frame_shows_live_when_complete",
                   "test_crash_recovery_persists_reset"):
            assert fp not in names, f"{fp} regressed to SOURCE_ONLY"

    def test_render_never_raises(self, reading):
        assert saa.render(reading, limit=3)
