"""Test Reality Gate (bt-2026-10-05-012717, landing c985ccaee4).

The landed ``tests/test_hardware_control.py`` passed VALIDATE ("1 passed", even
under ``-W error::RuntimeWarning``) while executing nothing and never importing
its subject. These pin the three structural invariants that reject it before
pytest runs, and the scoping that keeps them from judging legacy debt.
"""
from __future__ import annotations

import textwrap

from backend.core.ouroboros.governance.test_reality import (
    FAILURE_CLASS,
    analyze_test_source,
)

SUBJECT = "backend.autonomy.hardware_control"

# The landed file, verbatim (minus its O+V header).
LANDED_HOLLOW = textwrap.dedent('''
    import unittest
    from unittest.mock import patch, AsyncMock

    class TestHardwareControl(unittest.TestCase):

        async def test_control_camera_valid_actions(self):
            api_key = 'test_api_key'
            with patch('src.hardware_control.HardwareControlSystem') as mock_system_class:
                mock_system = AsyncMock()
                mock_system_class.return_value = mock_system
                mock_system.control_camera.return_value = {'status': 'success', 'action': 'start'}
                result = await mock_system.control_camera('start')
                self.assertEqual(result['status'], 'success')
                mock_system.control_camera.assert_called_once_with('start')
''')


def _rules(verdict):
    return sorted({v.rule for v in verdict.violations})


def test_the_landed_hollow_test_is_rejected_for_both_real_defects():
    v = analyze_test_source(LANDED_HOLLOW, subject=SUBJECT)
    assert v.hollow and _rules(v) == ["cannot_execute", "subject_not_imported"]
    assert v.summary().startswith(FAILURE_CLASS)
    fix = v.correction()
    assert "never awaits it" in fix and "IsolatedAsyncioTestCase" in fix
    assert f"never imports its subject `{SUBJECT}`" in fix


def test_a_real_test_passes():
    src = textwrap.dedent('''
        import pytest
        from backend.autonomy.hardware_control import HardwareControlSystem

        async def test_camera_start():
            system = HardwareControlSystem(api_key="k")
            result = await system.control_camera("start")
            assert result["status"] == "success"

        def test_bad_action_raises():
            with pytest.raises(ValueError):
                HardwareControlSystem(api_key="k").validate("nope")
    ''')
    v = analyze_test_source(src, subject=SUBJECT)
    assert not v.hollow and v.judged == ("test_camera_start", "test_bad_action_raises")


def test_every_importable_spelling_of_the_subject_counts():
    for imp, use in (
        ("from backend.autonomy.hardware_control import H", "H()"),
        ("from autonomy.hardware_control import H", "H()"),           # pythonpath = . backend
        ("from backend.autonomy import hardware_control", "hardware_control.H()"),
        ("import backend.autonomy.hardware_control as hc", "hc.H()"),
        ("import importlib\nhc = importlib.import_module('backend.autonomy.hardware_control')", "hc.H()"),
    ):
        src = f"{imp}\n\ndef test_x():\n    x = {use}\n    assert x\n"
        assert not analyze_test_source(src, subject=SUBJECT).hollow, imp


def test_imported_but_never_used_is_still_hollow():
    src = "from backend.autonomy.hardware_control import H\n\ndef test_x():\n    assert 1 == 1\n"
    v = analyze_test_source(src, subject=SUBJECT)
    assert _rules(v) == ["subject_not_imported"] and "is ever used" in v.correction()


def test_a_bare_module_name_is_not_an_import_of_the_subject():
    src = "from hardware_control import H\n\ndef test_x():\n    assert H()\n"
    assert _rules(analyze_test_source(src, subject=SUBJECT)) == ["subject_not_imported"]


def test_verification_forms_that_count():
    for body in (
        "assert f()",
        "self.assertTrue(f())",
        "m = f()\n    m.assert_called_once_with(1)",
        "import pytest\n    with pytest.raises(ValueError):\n        f()",
        "if f() != 2:\n        raise RuntimeError('bad')",
        "if not f():\n        raise AssertionError('bad')",
        "_check(f())",  # helper below verifies
    ):
        src = textwrap.dedent('''
            from backend.autonomy.hardware_control import f

            def _check(v):
                assert v

            def test_x():
                {body}
        ''').replace("{body}", body)
        v = analyze_test_source(src, subject=SUBJECT)
        assert v is not None and not v.hollow, body


def test_doing_without_checking_is_hollow():
    src = textwrap.dedent('''
        from backend.autonomy.hardware_control import f

        def test_clear():
            # Just check that it doesn't raise an exception
            f()
            print("ok")
    ''')
    v = analyze_test_source(src, subject=SUBJECT)
    assert _rules(v) == ["verifies_nothing"] and "`test_clear` verifies nothing" in v.correction()


def test_helper_recursion_is_cycle_safe():
    src = textwrap.dedent('''
        from backend.autonomy.hardware_control import f

        def a():
            b()

        def b():
            a()

        def test_x():
            f(); a()
    ''')
    assert _rules(analyze_test_source(src, subject=SUBJECT)) == ["verifies_nothing"]


def test_isolated_asyncio_test_case_can_execute():
    src = textwrap.dedent('''
        import unittest
        from backend.autonomy.hardware_control import f

        class T(unittest.IsolatedAsyncioTestCase):
            async def test_x(self):
                self.assertTrue(await f())
    ''')
    assert not analyze_test_source(src, subject=SUBJECT).hollow


def test_only_added_or_changed_tests_are_judged():
    legacy = "def test_old():\n    print('smoke')\n"
    edited = legacy + "\ndef test_new():\n    assert 2 == 2\n"
    v = analyze_test_source(edited, baseline_source=legacy)
    assert v.judged == ("test_new",) and not v.hollow
    changed = "def test_old():\n    print('smoke, edited')\n"
    assert _rules(analyze_test_source(changed, baseline_source=legacy)) == ["verifies_nothing"]


def test_no_subject_means_no_import_rule():
    assert not analyze_test_source("def test_x():\n    assert 1\n").hollow


def test_unparseable_or_testless_sources_yield_no_verdict():
    assert analyze_test_source("def broken(:\n") is None
    assert analyze_test_source("X = 1\n") is None


# ── wiring: one entry point, three consumers ─────────────────────────────

from pathlib import Path  # noqa: E402

from backend.core.ouroboros.governance.structured_critique import CritiqueBuilder  # noqa: E402
from backend.core.ouroboros.governance.test_reality import hollow_test_in_candidate  # noqa: E402

_GOV = Path(__file__).resolve().parents[2] / "backend/core/ouroboros/governance"
_DESC = "`backend/autonomy/hardware_control.py` has no corresponding test module."


def _repo(tmp_path):
    (tmp_path / "backend/autonomy").mkdir(parents=True)
    (tmp_path / "backend/autonomy/hardware_control.py").write_text(
        "class HardwareControlSystem:\n    def control_camera(self, action):\n        return {'status': 'ok'}\n"
    )
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_entry_point_rejects_the_landed_file_for_a_new_test(tmp_path):
    root = _repo(tmp_path)
    got = hollow_test_in_candidate(
        [("tests/test_hardware_control.py", LANDED_HOLLOW)],
        target_files=["tests/test_hardware_control.py"], description=_DESC, repo_root=root,
    )
    assert got is not None
    path, verdict = got
    assert path == "tests/test_hardware_control.py"
    assert {v.rule for v in verdict.violations} == {"cannot_execute", "subject_not_imported"}
    assert verdict.subject == "backend.autonomy.hardware_control"


def test_entry_point_admits_a_real_test(tmp_path):
    root = _repo(tmp_path)
    real = ("from backend.autonomy.hardware_control import HardwareControlSystem\n\n"
            "def test_camera():\n    assert HardwareControlSystem().control_camera('x')['status'] == 'ok'\n")
    assert hollow_test_in_candidate(
        [("tests/test_hardware_control.py", real)],
        target_files=["tests/test_hardware_control.py"], description=_DESC, repo_root=root,
    ) is None


def test_editing_an_existing_test_file_never_judges_its_legacy_debt(tmp_path):
    root = _repo(tmp_path)
    legacy = "def test_smoke():\n    print('runs')\n"
    (root / "tests/test_old.py").write_text(legacy)
    edited = legacy + "\ndef test_new():\n    assert True is True\n"
    assert hollow_test_in_candidate(
        [("tests/test_old.py", edited), ("backend/autonomy/hardware_control.py", "x = 1\n")],
        target_files=["tests/test_old.py"], description="TODO in tests/test_old.py", repo_root=root,
    ) is None


def test_the_retry_critique_keeps_every_correction_whole(tmp_path):
    root = _repo(tmp_path)
    _p, verdict = hollow_test_in_candidate(
        [("tests/test_hardware_control.py", LANDED_HOLLOW)],
        target_files=["tests/test_hardware_control.py"], description=_DESC, repo_root=root,
    )
    report = CritiqueBuilder.from_validation_output(
        file_path="tests/test_hardware_control.py", failure_class=FAILURE_CLASS,
        error_text=verdict.correction(),
    )
    assert len(report.critiques) == 2
    assert all(len(c.what_failed) > 100 and not c.what_failed.endswith("...") for c in report.critiques)
    assert any("IsolatedAsyncioTestCase" in c.what_failed for c in report.critiques)


def test_validate_runs_the_gate_before_any_test_runner():
    src = (_GOV / "orchestrator.py").read_text(encoding="utf-8")
    core = src[src.index("async def _run_validation_core("):]
    assert core.index("hollow_test_in_candidate") < core.index("_tree_runner.run(")
    assert core.index("_ast_preflight(") < core.index("hollow_test_in_candidate")


def test_the_single_validate_owner_feeds_hollow_test_to_retry_memory():
    """VALIDATE has one implementation (validate-single-owner deleted the
    orchestrator's inline twin), so the retry-memory admission lives once."""
    runner = (_GOV / "phase_runners/validate_runner.py").read_text(encoding="utf-8")
    assert f'in ("test", "build", "{FAILURE_CLASS}")' in runner
    orch = (_GOV / "orchestrator.py").read_text(encoding="utf-8")
    assert 'validation.failure_class in ("test", "build"' not in orch  # no second ladder


def test_l2_runs_the_gate_before_its_sandbox_with_no_fall_through():
    src = (_GOV / "repair_engine.py").read_text(encoding="utf-8")
    inner = src[src.index("async def _run_inner("):]
    gate = inner.index("await self._hollow_test(")
    assert gate < inner.index("await self._structural_validate(")
    block = inner[gate:inner.index("await self._structural_validate(")]
    assert "continue  # regenerate with the structural correction" in block
    assert "_max_struct_rejects" not in block and "MAX_REJECTS" not in block  # no escape to the sandbox
