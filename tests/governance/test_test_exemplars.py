"""Show the model a test that PASSES for a module like the one it is testing.

Soak bt-2026-09-20-183259: asked to write tests from scratch for async FastAPI
modules, the 30B went 0 for 5 at nine attempts each — ``argument of type
'coroutine' is not iterable``, ``'JSONResponse' is not iterable``, an unmocked
``psutil``. Conventions it was never shown. It landed the two test files whose
subjects were plain synchronous utilities.

Pinned here: relevance is computed from the repository (rare shared traits
count for more), an async subject is never handed a synchronous exemplar, and
NOTHING unverified is ever injected.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import repo_state
from backend.core.ouroboros.governance import test_exemplars as tx
from tests.support.ast_contract import calls_to, parse_module

ASYNC_SUBJECT = "import rareframework\n\nasync def handler(x):\n    return x\n"
SYNC_SUBJECT = "import os\n\ndef helper(x):\n    return x\n"

ASYNC_TEST = '''import pytest
from unittest.mock import AsyncMock


@pytest.fixture
def client():
    return AsyncMock()


@pytest.mark.asyncio
async def test_awaits(client):
    assert await client() is not None


@pytest.mark.asyncio
async def test_awaits_again(client):
    assert await client() is not None


def test_plain():
    assert 1 == 1
'''
SYNC_TEST = "def test_helper():\n    assert True is not False\n"


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo_state.reset_for_tests()
    tx.reset_for_tests()
    monkeypatch.setenv("JARVIS_TEST_EXEMPLAR_REGISTRY", str(tmp_path / "registry.json"))
    _write(tmp_path, "backend/alpha.py", ASYNC_SUBJECT)
    _write(tmp_path, "tests/test_alpha.py", ASYNC_TEST)
    _write(tmp_path, "backend/beta.py", SYNC_SUBJECT)
    _write(tmp_path, "tests/test_beta.py", SYNC_TEST)
    # The op's own subject: async, shares the RARE import with alpha.
    _write(tmp_path, "backend/gamma.py", ASYNC_SUBJECT.replace("handler", "endpoint"))
    yield tmp_path
    repo_state.reset_for_tests()
    tx.reset_for_tests()


def _verifier(monkeypatch, passing=lambda cand: True):
    ran = []

    async def fake(cand, repo_root, timeout_s):
        ran.append(cand.test.name)
        return bool(passing(cand))

    monkeypatch.setattr(tx, "_run_passes", fake)
    monkeypatch.setattr(tx, "_safe_to_run", lambda cand, root: True)
    return ran


OP = (["tests/test_gamma.py"], "`backend/gamma.py` has no test module. CREATE `tests/test_gamma.py`")


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------


def test_traits_are_imports_plus_structure():
    traits = tx.traits_of_source(ASYNC_SUBJECT)
    assert "rareframework" in traits and tx.FLAG_ASYNC in traits


def test_a_rare_shared_import_outweighs_a_common_one(repo):
    for i in range(6):
        _write(repo, f"backend/common{i}.py", SYNC_SUBJECT)
        _write(repo, f"tests/test_common{i}.py", SYNC_TEST)
    catalog = tx.build_catalog(repo)
    assert catalog.weight("rareframework") > catalog.weight("os")


def test_an_async_subject_is_never_offered_a_sync_exemplar(repo):
    ranked = tx.rank(repo / "backend" / "gamma.py", tx.build_catalog(repo))
    assert [c.test.name for _s, c in ranked] == ["test_alpha.py"]


def test_a_subject_is_not_offered_its_own_test(repo):
    ranked = tx.rank(repo / "backend" / "alpha.py", tx.build_catalog(repo))
    assert "test_alpha.py" not in [c.test.name for _s, c in ranked]


# ---------------------------------------------------------------------------
# Nothing unverified is ever shown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_passing_exemplar_is_injected(repo, monkeypatch):
    _verifier(monkeypatch)
    block = await tx.exemplar_instruction(*OP, repo)
    assert "VERIFIED PASSING" in block
    assert "tests/test_alpha.py" in block and "async def test_awaits" in block


@pytest.mark.asyncio
async def test_a_FAILING_exemplar_is_never_injected(repo, monkeypatch):
    _verifier(monkeypatch, passing=lambda cand: False)
    assert await tx.exemplar_instruction(*OP, repo) == ""


@pytest.mark.asyncio
async def test_each_pair_is_verified_once(repo, monkeypatch):
    ran = _verifier(monkeypatch)
    await tx.exemplar_instruction(*OP, repo)
    await tx.exemplar_instruction(*OP, repo)
    assert ran == ["test_alpha.py"], "the registry did not hold the verdict"


@pytest.mark.asyncio
async def test_editing_the_exemplar_forces_reverification(repo, monkeypatch):
    ran = _verifier(monkeypatch)
    await tx.exemplar_instruction(*OP, repo)
    (repo / "tests" / "test_alpha.py").write_text(ASYNC_TEST + "\n# edited\n")
    repo_state.reset_for_tests()
    tx.reset_for_tests()
    await tx.exemplar_instruction(*OP, repo)
    assert ran == ["test_alpha.py", "test_alpha.py"]


@pytest.mark.asyncio
async def test_an_unsafe_subject_is_never_even_run(repo, monkeypatch):
    ran = _verifier(monkeypatch)
    monkeypatch.setattr(tx, "_safe_to_run", lambda cand, root: False)
    assert await tx.exemplar_instruction(*OP, repo) == ""
    assert ran == []


@pytest.mark.asyncio
async def test_the_real_verifier_runs_a_real_test(repo):
    """No stub: the candidate is executed through the canonical spawn site."""
    _write(repo, "backend/delta.py", SYNC_SUBJECT.replace("helper", "other"))
    block = await tx.exemplar_instruction(
        ["tests/test_delta.py"], "`backend/delta.py` has no test. CREATE `tests/test_delta.py`", repo,
    )
    assert "tests/test_beta.py" in block


# ---------------------------------------------------------------------------
# When it must stay out of the way
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_op_that_is_not_writing_a_new_test_gets_nothing(repo, monkeypatch):
    ran = _verifier(monkeypatch)
    assert await tx.exemplar_instruction(["backend/gamma.py"], "harden `backend/gamma.py`", repo) == ""
    assert await tx.exemplar_instruction(["tests/test_alpha.py"], "extend the test", repo) == ""
    assert ran == []


@pytest.mark.asyncio
async def test_the_switch_turns_it_off(repo, monkeypatch):
    _verifier(monkeypatch)
    monkeypatch.setenv("JARVIS_TEST_EXEMPLAR_INJECTION_ENABLED", "false")
    assert await tx.exemplar_instruction(*OP, repo) == ""


@pytest.mark.asyncio
async def test_a_broken_verifier_injects_nothing_and_does_not_raise(repo, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(tx, "_run_passes", boom)
    monkeypatch.setattr(tx, "_safe_to_run", lambda cand, root: True)
    assert await tx.exemplar_instruction(*OP, repo) == ""


@pytest.mark.asyncio
async def test_context_is_returned_unchanged_when_there_is_nothing_to_add(repo):
    ctx = SimpleNamespace(target_files=("backend/gamma.py",), description="x", op_id="op-1")
    assert await tx.with_exemplar(ctx, repo) is ctx


# ---------------------------------------------------------------------------
# The excerpt
# ---------------------------------------------------------------------------


def test_the_excerpt_shows_each_shape_once_and_async_first():
    body = tx.excerpt(ASYNC_TEST, 10_000, prefer_async=True)
    ast.parse(body)  # whole definitions only
    assert "def client" in body, "fixtures are part of the structure"
    assert "test_awaits(" in body and "test_awaits_again" not in body
    assert body.index("test_awaits(") < body.index("test_plain")


def test_an_excerpt_that_cannot_fit_is_empty_not_truncated():
    assert tx.excerpt(ASYNC_TEST, 5, prefer_async=True) == ""


def test_an_unparseable_exemplar_yields_nothing():
    assert tx.excerpt("def broken(:\n", 10_000, prefer_async=False) == ""


# ---------------------------------------------------------------------------
# Reachability: both twins go through the one helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", [
    "backend/core/ouroboros/governance/phase_runners/plan_runner.py",
    "backend/core/ouroboros/governance/orchestrator.py",
])
def test_both_pre_generate_seams_call_the_helper(module):
    assert calls_to(parse_module(Path(module)), "with_exemplar")
