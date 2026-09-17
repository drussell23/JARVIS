"""Upstream work-selection: goals must declare a scope, and redundant work
must be recognised as redundant.

Both defects were measured in soak bt-2026-09-17-180722:

* 22 of 28 roadmap goals carried no ``target_symbols``, so every contract keyed
  on them was inert for the work the lane actually dispatches. No discovery
  source ever populated ``DiscoveredWork.symbols``, so ``synthesize_and_sign``
  signed every auto-authored goal with ``()``.
* An already-landed work order was re-emitted (the soak profile sets
  ``JARVIS_ALLOW_ROADMAP_REVISIT``), the model correctly found the work done,
  and the pipeline produced ``7e7fe18c3c`` anyway: a duplicated banner comment,
  quote churn and a stripped trailing newline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import declared_symbols as DS
from backend.core.ouroboros.governance import target_symbol_resolver as TSR


# --------------------------------------------------------------------------
# Phase 1 — symbols bound at authoring
# --------------------------------------------------------------------------

_MODULE = '''"""A module."""


def handle_command(payload):
    """Handle a command."""
    try:
        return payload["cmd"]
    except Exception:
        return None


def _private(x):
    return x
'''


def test_symbols_resolve_from_the_file_a_goal_names(tmp_path):
    src = tmp_path / "backend" / "api" / "thing.py"
    src.parent.mkdir(parents=True)
    src.write_text(_MODULE, encoding="utf-8")
    syms = TSR.resolve_for_goal(
        target_files=["backend/api/thing.py"],
        goal_text="fix handle_command so it logs before degrading",
        project_root=tmp_path,
    )
    assert "handle_command" in syms


def test_a_file_that_does_not_exist_yet_resolves_to_nothing(tmp_path):
    """A test synthesis goal names a file nothing has written. Inventing a
    scope would be worse than none — the validator would then enforce the
    invention."""
    assert TSR.resolve_for_goal(
        target_files=["tests/test_not_written_yet.py"],
        goal_text="[dag] test synthesis", project_root=tmp_path,
    ) == ()


def test_resolution_never_raises_on_junk(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n", encoding="utf-8")
    assert TSR.resolve_for_goal(
        target_files=["bad.py", "missing.py", "", "notpython.md"],
        goal_text="x", project_root=tmp_path,
    ) == ()


def test_only_primaries_are_bound_never_the_cluster():
    """A declared symbol is an obligation, not a hint: the existing no-op
    contract refuses a candidate in which ANY declared symbol is unchanged, so
    binding call-graph neighbours would demand they all be rewritten."""
    import inspect

    src = inspect.getsource(TSR.resolve_for_goal)
    body = src.split('"""')[-1]
    assert "result.primary" in body
    assert "symbol_names" not in body


def test_the_signer_binds_symbols_when_none_are_declared(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance import operator_goal_sanction as OGS

    monkeypatch.setattr(
        OGS, "_symbol_binding_enabled", lambda: True,
    )
    monkeypatch.setattr(
        Path, "cwd", classmethod(lambda cls: tmp_path),
    )
    src = tmp_path / "backend" / "api" / "thing.py"
    src.parent.mkdir(parents=True)
    src.write_text(_MODULE, encoding="utf-8")

    spec = OGS.GoalSpec(
        goal_id="g1", title="repair handle_command",
        description="handle_command must log before degrading",
        target_files=("backend/api/thing.py",),
    )
    bound = OGS._bind_target_symbols(spec)
    assert bound.target_symbols, "the signer authored an unscoped goal"
    assert "handle_command" in bound.target_symbols


def test_an_explicit_declaration_is_never_overridden(tmp_path):
    from backend.core.ouroboros.governance import operator_goal_sanction as OGS

    spec = OGS.GoalSpec(
        goal_id="g2", title="t", description="d",
        target_files=("backend/api/thing.py",),
        target_symbols=("operator_said_this",),
    )
    assert OGS._bind_target_symbols(spec).target_symbols == ("operator_said_this",)


def test_binding_failure_leaves_the_goal_authorable(monkeypatch):
    """Authoring must never break on resolution — an unscoped goal is the
    status quo, a refused goal is a regression."""
    from backend.core.ouroboros.governance import operator_goal_sanction as OGS

    def _boom(**kw):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(TSR, "resolve_for_goal", _boom)
    spec = OGS.GoalSpec(
        goal_id="g3", title="t", description="d", target_files=("a.py",),
    )
    assert OGS._bind_target_symbols(spec) is not None


# --------------------------------------------------------------------------
# Phase 3 — a candidate that changes nothing
# --------------------------------------------------------------------------

_ORIGINAL = '''"""Doc."""
import json


def render(payload):
    try:
        return json.dumps(payload, separators=(",", ":"))
    except Exception:
        return "{}"
'''

# Exactly what 7e7fe18c3c did: banner comment, quote churn, newline strip.
_CHURN = '''# [Ouroboros] Modified by Ouroboros (op=op-01a0b08d-) at 2026-09-17 18:18 UTC
"""Doc."""
import json


def render(payload):
    try:
        return json.dumps(payload, separators=(',', ':'))
    except Exception:
        return "{}"'''

_REAL = '''"""Doc."""
import json
import logging


def render(payload):
    try:
        return json.dumps(payload, separators=(",", ":"))
    except Exception:
        logging.exception("failed")
        return "{}"
'''


def _candidate(content: str) -> dict:
    return {"files": [{"full_content": content}]}


@pytest.fixture(autouse=True)
def _contract_on(monkeypatch):
    monkeypatch.setenv("JARVIS_DECLARED_SYMBOL_CONTRACT_ENABLED", "true")
    yield


def test_the_real_churn_commit_is_a_no_op():
    """THE regression, reduced from 7e7fe18c3c itself."""
    assert DS.candidate_is_functional_noop(_candidate(_CHURN), _ORIGINAL) is True


def test_a_real_change_is_not_a_no_op():
    """The one genuine autonomous landing this repo has produced was exactly
    this: two added logging calls."""
    assert DS.candidate_is_functional_noop(_candidate(_REAL), _ORIGINAL) is False


def test_a_new_file_is_never_a_no_op():
    assert DS.candidate_is_functional_noop(_candidate(_REAL), None) is False
    assert DS.candidate_is_functional_noop(_candidate(_REAL), "") is False


def test_a_docstring_change_is_a_real_change():
    """Docstrings are in the AST and they are documentation callers read."""
    after = _ORIGINAL.replace('"""Doc."""', '"""Renders a frame."""')
    assert DS.candidate_is_functional_noop(_candidate(after), _ORIGINAL) is False


def test_an_unparsable_candidate_is_not_judged_here():
    """Syntax has its own gate; refusing it here would misattribute."""
    assert DS.candidate_is_functional_noop(_candidate("def ("), _ORIGINAL) is False


def test_no_threshold_was_introduced():
    """'How much changed' would be a constant nobody can justify, and it would
    eventually reject a real one-character fix."""
    import inspect

    body = inspect.getsource(DS.candidate_is_functional_noop).split('"""')[-1]
    for suspect in ("0.1", "0.5", "ratio", "threshold", "len(diff)", "> 1"):
        assert suspect not in body, f"a delta threshold crept in: {suspect}"


def test_it_never_raises():
    for bad in ({}, {"files": []}, {"files": [{"full_content": None}]}):
        assert DS.candidate_is_functional_noop(bad, _ORIGINAL) is False
