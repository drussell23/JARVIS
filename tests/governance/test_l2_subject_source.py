"""The repair prompt shows what the code under test actually DOES.

After ``021aae0468`` the L2 model sees the failing assertion -- what the test
EXPECTED. For a test-synthesis op (7/7 of bt-2026-09-21-235603's L2 runs),
``assert False is True`` on ``should_use_lite_mode()`` is only repairable by
someone who can read ``should_use_lite_mode``: the signature anchor gives its
API, never its body. ``exercised_source_block`` supplies the bodies of exactly
the symbols the failure and the failing tests name.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.ast_signature_anchor import exercised_source_block
from backend.core.ouroboros.governance.repair_engine import RepairBudget, RepairEngine
from backend.core.ouroboros.governance.repair_sandbox import SandboxValidationResult

SUBJECT = '''\
import os


def should_use_lite_mode():
    """Lite mode only when memory is scarce."""
    return os.environ.get("LITE") == "1"


def unrelated_heavy_path():
    total = 0
    for i in range(10):
        total += i
    return total


class Manager:
    def __init__(self):
        self.ready = False

    def start(self):
        self.ready = True
        return self.ready
'''

TEST = '''\
from backend.lite import should_use_lite_mode, Manager


def test_lite_default():
    assert should_use_lite_mode() is True


def test_manager():
    assert Manager().start() is True
'''

EVIDENCE = (
    "tests/test_lite.py:5: in test_lite_default\n"
    "    assert should_use_lite_mode() is True\n"
    "E   assert False is True\n"
    "E    +  where False = should_use_lite_mode()\n"
)
DESCRIPTION = "Write tests for backend/lite.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_REPAIR_SUBJECT_SOURCE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_REPAIR_SUBJECT_MAX_CHARS", raising=False)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/lite.py").write_text(SUBJECT)
    (tmp_path / "tests").mkdir()
    return tmp_path


def _block(repo, **kw):
    args = dict(
        evidence_text=EVIDENCE, test_source=TEST,
        failing_tests=("tests/test_lite.py::test_lite_default",),
        exclude=("tests/test_lite.py",),
    )
    args.update(kw)
    return exercised_source_block(("tests/test_lite.py",), DESCRIPTION, repo, **args)


def test_the_body_of_the_function_the_failure_names_is_shown(repo):
    block = _block(repo)
    assert "SOURCE UNDER TEST" in block
    assert 'return os.environ.get("LITE") == "1"' in block
    assert "unrelated_heavy_path" not in block, "only what the failure exercises"


def test_only_the_failing_tests_bodies_select_symbols(repo):
    """test_manager passes; its Manager must not crowd the prompt."""
    assert "def start" not in _block(repo)


def test_a_file_the_op_is_editing_is_never_shown_as_read_only(repo):
    assert _block(repo, exclude=("backend/lite.py",)) == ""


def test_evidence_named_symbols_win_a_tight_budget(repo, monkeypatch):
    both = ("tests/test_lite.py::test_lite_default", "tests/test_lite.py::test_manager")
    full = _block(repo, failing_tests=both)
    assert "def start" in full and "def should_use_lite_mode" in full
    lite_only = _block(repo, failing_tests=("tests/test_lite.py::test_lite_default",))
    # Room for exactly one slice (the budget counts slices, not the header).
    one_slice = lite_only[lite_only.index("### "):]
    monkeypatch.setenv("JARVIS_REPAIR_SUBJECT_MAX_CHARS", str(len(one_slice)))
    tight = _block(repo, failing_tests=both)
    assert "def should_use_lite_mode" in tight, "the function the evidence names came first"
    assert "def start" not in tight


def test_a_body_is_never_cut_in_half(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_REPAIR_SUBJECT_MAX_CHARS", "40")
    assert _block(repo) == ""


def test_a_class_does_not_repeat_the_method_already_shown(repo):
    block = _block(
        repo, evidence_text="E   AssertionError: Manager.start returned None",
        failing_tests=("tests/test_lite.py::test_manager",),
    )
    assert block.count("def start") == 1
    assert "def __init__" not in block, "the method slice won; the class was dropped"


def test_a_call_through_an_instance_still_selects_what_it_calls(repo):
    """Found on the real smart_startup_manager op: the evidence names the
    method QUALIFIED (``manager.should_use_lite_mode()``), the only bare name
    is a class too big for the budget, and the block came back empty."""
    filler = "\n".join(f"    def helper_{i}(self):\n        return {i}\n" for i in range(400))
    (repo / "backend/big.py").write_text(
        "class Huge:\n"
        "    def should_go(self):\n"
        "        return self.level > 3\n\n" + filler
    )
    evidence = (
        "E   assert False is True\n"
        "E    +  where False = <bound method Huge.should_go of <Huge>>()\n"
    )
    test = "from backend.big import Huge\n\ndef test_go():\n    h = Huge()\n    assert h.should_go() is True\n"
    block = exercised_source_block(
        ("tests/test_big.py",), "Write tests for backend/big.py", repo,
        evidence_text=evidence, test_source=test,
        failing_tests=("tests/test_big.py::test_go",), exclude=("tests/test_big.py",),
    )
    assert "return self.level > 3" in block
    assert "helper_399" not in block, "the whole class does not fit and is not shown"


def test_the_kill_switch(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_REPAIR_SUBJECT_SOURCE_ENABLED", "false")
    assert _block(repo) == ""


def test_an_unparseable_candidate_still_resolves_through_the_evidence(repo):
    assert "def should_use_lite_mode" in _block(repo, test_source="def broken(:\n")


# ---------------------------------------------------------------------------
# End to end through the L2 loop and the real prompt builder
# ---------------------------------------------------------------------------

PYTEST_OUT = (
    "=================================== FAILURES ===================================\n"
    "______________________________ test_lite_default _______________________________\n"
    + EVIDENCE +
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_lite.py::test_lite_default - assert False is True\n"
    "============================== 1 failed in 0.01s ===============================\n"
)


class _Provider:
    def __init__(self) -> None:
        self.contexts: List[Any] = []

    async def generate(self, ctx, deadline, *, repair_context=None,
                       hypothesis_seed=None, temperature=None):
        self.contexts.append(repair_context)
        n = len(self.contexts)

        class _R:
            candidates = [{"file_path": "tests/test_lite.py", "full_content": TEST + f"# {n}\n"}]
            model_id = "stub"
            provider_name = "stub"
        return _R()


class _Sandbox:
    def __init__(self, repo_root, test_timeout_s):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def sandbox_root(self):
        return Path("/nonexistent-subject-source-root")

    async def apply_full_content(self, content, file_path):
        return None

    async def apply_patch(self, unified_diff, file_path):
        return None

    async def run_tests(self, test_targets, timeout_s):
        return SandboxValidationResult(
            passed=False, stdout=PYTEST_OUT, stderr="", returncode=1, duration_s=0.1,
        )


class _Ctx:
    op_id = "op-subject-source"
    target_files = ("tests/test_lite.py",)
    description = DESCRIPTION

    class generation:  # noqa: N801
        candidates = [{"file_path": "tests/test_lite.py", "full_content": TEST}]


def test_every_repair_iteration_carries_the_code_under_test(repo, monkeypatch):
    for var in ("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED", "JARVIS_L2_MULTIFILE_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    provider = _Provider()
    engine = RepairEngine(
        budget=RepairBudget.from_env(), prime_provider=provider,
        repo_root=repo, sandbox_factory=_Sandbox,
    )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=300)
    asyncio.run(engine._run_inner(_Ctx(), object(), deadline))

    assert provider.contexts
    for rc in provider.contexts:
        assert 'return os.environ.get("LITE") == "1"' in rc.subject_source

    from unittest.mock import MagicMock

    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    (repo / "tests/test_lite.py").write_text(TEST)
    ctx = MagicMock()
    ctx.op_id, ctx.description = "op-subject-source", DESCRIPTION
    ctx.target_files = ["tests/test_lite.py"]
    ctx.human_instructions = ctx.strategic_memory_prompt = ""
    ctx.expanded_context_files = ()
    ctx.cross_repo, ctx.repo_scope, ctx.telemetry, ctx.is_read_only = False, set(), None, False
    prompt = _build_codegen_prompt(
        ctx=ctx, repo_root=repo, repo_roots=None, repair_context=provider.contexts[0],
    )
    trace_at = prompt.index("FULL FAILURE TRACE")
    source_at = prompt.index("SOURCE UNDER TEST")
    assert trace_at < source_at, "the code follows the failure it explains"
