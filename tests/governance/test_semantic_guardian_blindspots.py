"""Tier 0 — Anticipatory Edge-Case Armor regression suite."""
from __future__ import annotations

import textwrap

import pytest

from backend.core.ouroboros.governance.semantic_guardian_blindspots import (
    pat_frozen_dataclass_mutation as frozen,
    pat_namedtuple_attr_assignment as ntuple,
    pat_pydantic_immutable_set as pyd,
    pat_type_coercion_blindspot as coerce,
    pat_loop_var_rebind_lost as loopreb,
)
from backend.core.ouroboros.governance.semantic_guardian import SemanticGuardian


def _run(detector, src: str):
    return detector(file_path="x.py", old_content="", new_content=textwrap.dedent(src))


# --------------------------------------------------------------------------- frozen
def test_frozen_infile_mutation_fires():
    hit = _run(frozen, """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class P:
            x: int
        def f():
            p = P(1)
            p.x = 2
    """)
    assert hit is not None and hit.severity == "hard" and hit.lines


def test_frozen_replace_is_clean():
    hit = _run(frozen, """
        import dataclasses
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class P:
            x: int
        def f():
            p = P(1)
            p = dataclasses.replace(p, x=2)
    """)
    assert hit is None


def test_frozen_cross_file_validationresult_via_registry():
    # The canonical case: ValidationResult defined elsewhere, mutated here.
    hit = _run(frozen, """
        def f(validation):
            validation = ValidationResult(passed=True)
            validation.passed = False
    """)
    assert hit is not None and hit.severity == "hard"


def test_frozen_replace_propagates_type():
    hit = _run(frozen, """
        import dataclasses
        def f(validation):
            validation = ValidationResult(passed=True)
            v2 = dataclasses.replace(validation, passed=False)
            v2.passed = True
    """)
    # v2 inherits ValidationResult type via replace propagation -> mutation flagged
    assert hit is not None


def test_non_frozen_dataclass_clean():
    hit = _run(frozen, """
        from dataclasses import dataclass
        @dataclass
        class P:
            x: int
        def f():
            p = P(1)
            p.x = 2
    """)
    assert hit is None


def test_env_extensible_registry(monkeypatch):
    monkeypatch.setenv("JARVIS_SEMGUARD_KNOWN_FROZEN_TYPES", "MyFrozenThing, Other")
    hit = _run(frozen, """
        def f():
            t = MyFrozenThing()
            t.value = 1
    """)
    assert hit is not None


# --------------------------------------------------------------------------- namedtuple
def test_namedtuple_mutation_fires():
    hit = _run(ntuple, """
        from typing import NamedTuple
        class Point(NamedTuple):
            x: int
            y: int
        def f():
            p = Point(1, 2)
            p.x = 9
    """)
    assert hit is not None and hit.severity == "hard"


def test_namedtuple_not_flagged_by_frozen_detector():
    # kind isolation: a NamedTuple is not a frozen dataclass
    assert _run(frozen, """
        from typing import NamedTuple
        class Point(NamedTuple):
            x: int
        def f():
            p = Point(1)
            p.x = 9
    """) is None


# --------------------------------------------------------------------------- pydantic
def test_pydantic_v2_frozen_fires():
    hit = _run(pyd, """
        from pydantic import BaseModel, ConfigDict
        class M(BaseModel):
            model_config = ConfigDict(frozen=True)
            x: int
        def f():
            m = M(x=1)
            m.x = 2
    """)
    assert hit is not None and hit.severity == "hard"


def test_pydantic_v1_config_fires():
    hit = _run(pyd, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
            class Config:
                allow_mutation = False
        def f():
            m = M(x=1)
            m.x = 2
    """)
    assert hit is not None


def test_pydantic_mutable_clean():
    assert _run(pyd, """
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        def f():
            m = M(x=1)
            m.x = 2
    """) is None


# --------------------------------------------------------------------------- coercion (CFG)
def test_coercion_unguarded_attr_fires():
    hit = _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            return x.upper()
    """)
    assert hit is not None and hit.severity == "soft"


def test_coercion_if_not_none_guard_clean():
    assert _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            if x is not None:
                return x.upper()
            return ""
    """) is None


def test_coercion_early_return_guard_clean():
    assert _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            if x is None:
                return ""
            return x.upper()
    """) is None


def test_coercion_assert_guard_clean():
    assert _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            assert x is not None
            return x.upper()
    """) is None


def test_coercion_and_shortcircuit_clean():
    assert _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            return x is not None and x.upper() or ""
    """) is None


def test_coercion_truthy_guard_clean():
    assert _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            if x:
                return x.upper()
            return ""
    """) is None


def test_coercion_pipe_none_annotation_fires():
    hit = _run(coerce, """
        def f(x: "str | None"):
            return len(x)
    """)
    assert hit is not None


def test_coercion_subscript_unguarded_fires():
    hit = _run(coerce, """
        from typing import Optional, List
        def f(x: Optional[list]):
            return x[0]
    """)
    assert hit is not None


def test_coercion_int_call_unguarded_fires():
    hit = _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            return int(x)
    """)
    assert hit is not None


def test_coercion_non_optional_clean():
    assert _run(coerce, """
        def f(x: str):
            return x.upper()
    """) is None


def test_coercion_reassign_drops_tracking():
    # x reassigned to a definitely-present value -> not flagged afterward
    assert _run(coerce, """
        from typing import Optional
        def f(x: Optional[str]):
            x = "default"
            return x.upper()
    """) is None


# --------------------------------------------------------------------------- loop rebind
def test_loop_rebind_lost_fires():
    hit = _run(loopreb, """
        def f(items):
            for v in items:
                v = v.strip()
    """)
    assert hit is not None and hit.severity == "soft"


def test_loop_rebind_used_after_clean():
    assert _run(loopreb, """
        def f(items, out):
            for v in items:
                v = v.strip()
                out.append(v)
    """) is None


# --------------------------------------------------------------------------- integration
def test_registered_in_guardian():
    g = SemanticGuardian()
    assert "frozen_dataclass_mutation" in g.patterns
    assert "type_coercion_blindspot" in g.patterns
    dets = g.inspect(file_path="x.py", old_content="", new_content=textwrap.dedent("""
        def f(validation):
            validation = ValidationResult(passed=True)
            validation.passed = False
    """))
    assert any(d.pattern == "frozen_dataclass_mutation" for d in dets)


def test_per_pattern_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_SEMGUARD_FROZEN_DATACLASS_MUTATION_ENABLED", "0")
    g = SemanticGuardian()
    dets = g.inspect(file_path="x.py", old_content="", new_content=textwrap.dedent("""
        def f(validation):
            validation = ValidationResult(passed=True)
            validation.passed = False
    """))
    assert not any(d.pattern == "frozen_dataclass_mutation" for d in dets)


def test_never_raises_on_garbage():
    for d in (frozen, ntuple, pyd, coerce, loopreb):
        assert d(file_path="x.py", old_content="", new_content="def (((") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
