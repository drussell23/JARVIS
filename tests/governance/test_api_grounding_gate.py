"""Refuse a plan that calls what does not exist -- and nothing else.

Of validation failures carrying an identifiable exception, ~63% are
AttributeError / ModuleNotFoundError / ImportError, against 6 SyntaxError in
the same corpus. All of them are found after GENERATE has been paid for. The
question "does module.symbol exist" is answerable from this repository's own
AST in milliseconds.

The danger is not missing one. It is refusing a correct plan: a gate that
cries wolf is the first thing an operator disables, and then the 63% comes
back permanently. So most of these tests are about what the gate must NOT
claim.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.api_grounding_gate import (
    APIGroundingFault,
    GroundingReport,
    Verdict,
    code_spans,
    extract_references,
    gate_enabled,
    ground_plan,
    shed_enabled,
)


@pytest.fixture
def repo(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "real.py").write_text(
        "LIMIT = 5\n\n\n"
        "def exists(a, b=1):\n"
        '    """Real."""\n'
        "    return a\n\n\n"
        "class Thing:\n"
        "    def method(self):\n"
        "        return 1\n"
    )
    return tmp_path


def _fenced(code: str) -> str:
    return f"Plan:\n\n```python\n{code}\n```\n"


# ---------------------------------------------------------------------------
# What it must catch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_attribute_is_caught(repo):
    report = await ground_plan(
        "Call `pkg.real.does_not_exist` then finish.", project_root=repo,
    )
    assert not report.grounded
    assert report.verdict is Verdict.MISSING
    assert [r.symbol for r in report.missing] == ["does_not_exist"]


@pytest.mark.asyncio
async def test_missing_import_name_is_caught(repo):
    """Only the parser can see this: the name sits after ``import`` with no
    dot for a regex to find it by."""
    report = await ground_plan(
        _fenced("from pkg.real import no_such_function"), project_root=repo,
    )
    assert not report.grounded
    assert [r.symbol for r in report.missing] == ["no_such_function"]


@pytest.mark.asyncio
async def test_several_misses_are_all_reported(repo):
    report = await ground_plan(
        _fenced("from pkg.real import nope_one, nope_two, exists"),
        project_root=repo,
    )
    assert {r.symbol for r in report.missing} == {"nope_one", "nope_two"}


# ---------------------------------------------------------------------------
# What it must NOT claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_symbol_is_grounded(repo):
    report = await ground_plan("Use `pkg.real.exists` here.", project_root=repo)
    assert report.grounded


@pytest.mark.asyncio
async def test_module_reference_is_not_a_missing_symbol(repo):
    """``pkg.real`` splits into module ``pkg`` + symbol ``real``, and
    ``pkg/__init__.py`` has no name ``real`` -- it has a SUBMODULE by that
    name. Refusing this would reject every plan that names a module."""
    report = await ground_plan("We will edit `pkg.real` today.", project_root=repo)
    assert report.grounded


@pytest.mark.asyncio
async def test_stdlib_is_unknown_not_missing(repo):
    """Absence of evidence is not evidence of absence. A gate that failed
    stdlib would refuse every correct plan that imports pathlib."""
    report = await ground_plan(
        "Use `pathlib.Path` and `os.environ`.", project_root=repo,
    )
    assert report.grounded
    assert {r.dotted for r in report.unknown} == {"pathlib.Path", "os.environ"}


@pytest.mark.asyncio
async def test_module_level_constants_count_as_api(repo):
    report = await ground_plan("Read `pkg.real.LIMIT`.", project_root=repo)
    assert report.grounded


@pytest.mark.asyncio
async def test_class_methods_count_as_api(repo):
    report = await ground_plan("Call `pkg.real.method`.", project_root=repo)
    assert report.grounded


@pytest.mark.asyncio
async def test_prose_is_not_mined(repo):
    """'the manager's internal state' is not an API claim. Mining prose for
    dotted names produces noise a gate cannot act on."""
    report = await ground_plan(
        "We will update the manager.state and refresh things.",
        project_root=repo,
    )
    assert report.grounded
    assert report.checked == ()


@pytest.mark.asyncio
async def test_bare_names_are_not_references(repo):
    """A bare name is a local far more often than an API."""
    assert extract_references("`something_undefined`") == ()


@pytest.mark.asyncio
async def test_empty_plan_is_grounded(repo):
    report = await ground_plan("", project_root=repo)
    assert report.grounded
    assert report.verdict is Verdict.UNKNOWN


@pytest.mark.asyncio
async def test_unparseable_code_span_still_scanned(repo):
    """A plan whose snippet does not parse must not silently skip checking."""
    report = await ground_plan(
        _fenced("def broken(:\n  pkg.real.does_not_exist()"), project_root=repo,
    )
    assert not report.grounded


# ---------------------------------------------------------------------------
# Extraction mechanics
# ---------------------------------------------------------------------------


def test_code_spans_prefers_fences_and_inline():
    spans = code_spans("prose ```python\nX = 1\n``` more `a.b` prose")
    assert any("X = 1" in s for s in spans)
    assert any("a.b" in s for s in spans)


def test_relative_imports_are_skipped():
    """``from . import x`` names no module the gate can adjudicate."""
    refs = extract_references("```python\nfrom . import thing\n```")
    assert all(r.symbol != "thing" for r in refs)


def test_star_import_is_skipped():
    refs = extract_references("```python\nfrom pkg.real import *\n```")
    assert all(r.symbol != "*" for r in refs)


def test_duplicate_references_collapse():
    refs = extract_references("`a.b` and `a.b` again")
    assert len(refs) == 1


def test_extraction_never_raises_on_junk():
    for junk in ("", None, "```", "`", "\x00"):
        extract_references(junk or "")


# ---------------------------------------------------------------------------
# Switches and fault
# ---------------------------------------------------------------------------


def test_gate_defaults_on(monkeypatch):
    monkeypatch.delenv("JARVIS_API_GROUNDING_GATE_ENABLED", raising=False)
    assert gate_enabled() is True


def test_shed_defaults_off(monkeypatch):
    """The gate earns the authority to shed by first proving it fires on
    real plans and not on correct ones."""
    monkeypatch.delenv("JARVIS_API_GROUNDING_SHED_ENABLED", raising=False)
    assert shed_enabled() is False


@pytest.mark.asyncio
async def test_disabled_gate_checks_nothing(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_API_GROUNDING_GATE_ENABLED", "false")
    report = await ground_plan("`pkg.real.nope`", project_root=repo)
    assert report.grounded
    assert report.checked == ()


def test_fault_names_every_missing_symbol():
    from backend.core.ouroboros.governance.api_grounding_gate import Reference
    report = GroundingReport(
        checked=(), missing=(Reference("m.a", "m", "a"), Reference("m.b", "m", "b")),
    )
    fault = APIGroundingFault(report)
    assert "m.a" in str(fault) and "m.b" in str(fault)
    assert fault.report is report


@pytest.mark.asyncio
async def test_concurrent_grounding_is_safe(repo):
    """The sentinel runs goals concurrently; resolution is I/O under a
    thread hop and must not interleave into wrong verdicts."""
    reports = await asyncio.gather(*[
        ground_plan("`pkg.real.exists` and `pkg.real.nope`", project_root=repo)
        for _ in range(25)
    ])
    assert all(not r.grounded for r in reports)
    assert all([x.symbol for x in r.missing] == ["nope"] for r in reports)
