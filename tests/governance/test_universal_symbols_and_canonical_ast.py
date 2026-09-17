"""Module-level scope, and an AST comparison that cannot be padded.

Two gaps found by USING the gates on real work, not by reading them:

* ``target_symbol_resolver`` indexed only ``def`` and ``class``, so a goal whose
  work IS a module-level constant could never be scoped — while the scope
  validator counted every module-level change as out-of-scope. A real ambient
  red asks for a ``LOCAL_DEFECT`` entry in ``_FAILURE_MODE_DEFAULT``: unscopeable
  AND guaranteed to trip the validator.
* Soak bt-2026-09-17-184946 produced ``34b5bd8b33``, which added
  ``exc_info=True`` to ``logging.exception(...)``. That argument restates the
  function's own default, so the call is identical — but the AST is not, and a
  raw dump comparison called it a real change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import declared_symbols as DS
from backend.core.ouroboros.governance import target_symbol_resolver as TSR


@pytest.fixture(autouse=True)
def _contract_on(monkeypatch):
    monkeypatch.setenv("JARVIS_DECLARED_SYMBOL_CONTRACT_ENABLED", "true")
    yield


# --------------------------------------------------------------------------
# Phase 1 — the resolver can name a module-level binding
# --------------------------------------------------------------------------

_WITH_CONST = '''"""M."""
from enum import Enum


class Mode(Enum):
    A = "a"


_FAILURE_MODE_DEFAULT = {
    Mode.A: "retry",
}

ANNOTATED: dict = {"k": 1}

FIRST = SECOND = 2

X, Y = 3, 4


def handler():
    return _FAILURE_MODE_DEFAULT
'''


def test_a_module_level_constant_is_indexable(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(_WITH_CONST, encoding="utf-8")
    syms = TSR.resolve_for_goal(
        target_files=["m.py"],
        goal_text="add an entry to _FAILURE_MODE_DEFAULT",
        project_root=tmp_path,
    )
    assert "_FAILURE_MODE_DEFAULT" in syms


def test_annotated_assignment_is_indexed():
    names = {s.name for s in TSR._index(_WITH_CONST)}
    assert "ANNOTATED" in names


def test_chained_and_tuple_targets_are_all_indexed():
    names = {s.name for s in TSR._index(_WITH_CONST)}
    assert {"FIRST", "SECOND", "X", "Y"} <= names


def test_an_annotation_without_a_value_binds_nothing():
    """A declaration is not a binding — there is nothing there to change."""
    names = {s.name for s in TSR._index("DECLARED_ONLY: int\n")}
    assert "DECLARED_ONLY" not in names


def test_functions_are_still_indexed():
    names = {s.name for s in TSR._index(_WITH_CONST)}
    assert "handler" in names


def test_subscript_targets_bind_no_new_name():
    names = {s.name for s in TSR._index("CONFIG = {}\nCONFIG['k'] = 1\n")}
    assert names == {"CONFIG"}


# --------------------------------------------------------------------------
# Phase 1 — declaring a binding makes editing it IN scope
# --------------------------------------------------------------------------

def _candidate(content: str) -> dict:
    return {"files": [{"full_content": content}]}


_EDITED_CONST = _WITH_CONST.replace(
    '    Mode.A: "retry",', '    Mode.A: "retry",\n    Mode.A: "refuse",',
)


def test_editing_a_declared_binding_is_not_a_violation():
    """THE false positive this closes: the goal named the constant, so
    changing it is the work."""
    assert DS.out_of_scope_changes(
        ["_FAILURE_MODE_DEFAULT"], _candidate(_EDITED_CONST), _WITH_CONST,
    ) == ()


def test_editing_an_UNdeclared_binding_is_still_a_violation():
    assert DS.out_of_scope_changes(
        ["handler"], _candidate(_EDITED_CONST), _WITH_CONST,
    ) != ()


def test_half_a_chained_declaration_is_not_authorisation():
    """``FIRST = SECOND = ...`` binds two names; declaring one does not
    authorise rewriting the statement that binds both."""
    after = _WITH_CONST.replace("FIRST = SECOND = 2", "FIRST = SECOND = 99")
    assert DS.out_of_scope_changes(
        ["FIRST"], _candidate(after), _WITH_CONST,
    ) != ()


def test_the_deleted_export_is_still_caught():
    """The original defect must survive every refinement."""
    before = _WITH_CONST + '\n__all__ = ["handler"]\n'
    after = _WITH_CONST
    v = DS.out_of_scope_changes(["handler"], _candidate(after), before)
    assert any(DS.is_module_scope_violation(x) for x in v)


def test_module_scope_violations_are_identifiable():
    """Shadow mode needs to tell the classes apart without string-sniffing at
    the call site."""
    assert DS.is_module_scope_violation(DS.MODULE_SCOPE_MARKER + " (x)") is True
    assert DS.is_module_scope_violation("handler (modified)") is False


# --------------------------------------------------------------------------
# Phase 2 — canonicalisation
# --------------------------------------------------------------------------

_BASE = '''import logging


def f():
    try:
        pass
    except Exception:
        logging.exception("boom")
'''


def test_a_kwarg_restating_the_stdlib_default_is_not_a_change():
    """THE bypass: logging.exception already defaults exc_info=True."""
    bloat = _BASE.replace('"boom")', '"boom", exc_info=True)')
    assert DS.candidate_is_functional_noop(_candidate(bloat), _BASE) is True


def test_a_kwarg_that_differs_from_the_default_IS_a_change():
    real = _BASE.replace('"boom")', '"boom", stack_info=True)')
    assert DS.candidate_is_functional_noop(_candidate(real), _BASE) is False


def test_a_non_stdlib_module_is_never_imported_to_read_defaults():
    """Importing project code to inspect a signature would execute it during
    validation. A local module aliased as `logging` must be left alone."""
    shadowed = _BASE.replace("import logging", "import mylogging as logging")
    bloat = shadowed.replace('"boom")', '"boom", exc_info=True)')
    assert DS.candidate_is_functional_noop(_candidate(bloat), shadowed) is False


def test_a_bare_call_is_left_alone():
    base = "from logging import exception\n\n\ndef f():\n    exception('x')\n"
    after = base.replace("exception('x')", "exception('x', exc_info=True)")
    assert DS.candidate_is_functional_noop(_candidate(after), base) is False


def test_type_is_not_coerced_when_matching_a_default():
    """`1` is not `True` here: a candidate that swapped one for the other
    changed the source for a reader even if Python would not notice."""
    base = "import logging\n\n\ndef f():\n    logging.exception('x')\n"
    one = base.replace("'x')", "'x', exc_info=1)")
    assert DS.candidate_is_functional_noop(_candidate(one), base) is False


def test_real_work_is_never_called_trivial():
    """The failure direction that matters — the genuine landing 7f8c686ce0 was
    exactly the addition of logging calls."""
    added = _BASE.replace(
        "        logging.exception(\"boom\")",
        "        logging.exception(\"boom\")\n        raise",
    )
    assert DS.candidate_is_functional_noop(_candidate(added), _BASE) is False


def test_canonicalisation_is_shared_with_the_promotion_gate():
    """One normaliser, or a candidate refused upstream becomes promotable
    downstream."""
    import inspect

    from backend.core.ouroboros.governance import accumulation_promotion_gate as G

    assert "canonical_ast_dump" in inspect.getsource(G._check_semantic_delta)


def test_canonicalisation_surfaces_unparsable_source():
    with pytest.raises(SyntaxError):
        DS.canonical_ast_dump("def (:\n")


def test_no_hardcoded_kwarg_table():
    """A tabulated list of 'redundant kwargs' would freeze one stdlib version
    into a constant and rot silently."""
    import inspect

    body = inspect.getsource(DS._DefaultKwargPruner).split('"""')[-1]
    assert "signature" in body
    assert "exc_info" not in body, "a specific kwarg was hardcoded"


# --------------------------------------------------------------------------
# Phase 3 — shadow mode for the module-level class
# --------------------------------------------------------------------------

_MODULE_V = DS.MODULE_SCOPE_MARKER + " (module-level statements changed)"
_SYMBOL_V = "handler (modified)"


def test_unarmed_enforcement_only_reports():
    assert DS.scope_verdict([_SYMBOL_V], enforced=False) == "report"


def test_nothing_found_is_report():
    assert DS.scope_verdict([], enforced=True) == "report"


def test_module_only_violations_calibrate_rather_than_refuse():
    """The newest class the validator can see: no production run has exercised
    a module-scope verdict, so it gets one calibration pass."""
    assert DS.scope_verdict([_MODULE_V], enforced=True) == "calibrate"


def test_a_symbol_violation_refuses():
    assert DS.scope_verdict([_SYMBOL_V], enforced=True) == "refuse"


def test_a_mixed_verdict_refuses():
    """A candidate that breached both does not earn the benefit of the doubt
    for the half that is new."""
    assert DS.scope_verdict([_MODULE_V, _SYMBOL_V], enforced=True) == "refuse"


def test_the_orchestrator_consumes_the_shared_verdict():
    """The policy must not be re-implemented inline where it cannot be tested."""
    import inspect

    from backend.core.ouroboros.governance import orchestrator as O

    src = inspect.getsource(O)
    assert "scope_verdict" in src
    assert "ModuleScopeCalibrationEvent" in src
