"""Spine for the AST-Signature Anchor — the structural cure for the local
model's API hallucination (it wrote parse_model_physics("model_a", 100, 200)
against a real parse_model_physics(payload) -> Optional[ModelPhysics])."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import ast_signature_anchor as A


MOD = textwrap.dedent('''
    from typing import Any, Optional
    _PRIVATE = 1
    def public_fn(payload: Any, *, flag: bool = False) -> "Optional[int]":
        return None
    async def afetch(url: str) -> bytes:
        return b""
    def _private_fn(x):
        return x
    class Thing:
        def __init__(self, x: int) -> None:
            self.x = x
        def method(self, a, b=2) -> int:
            return a + b
        def _hidden(self):
            return None
    class _PrivateClass:
        pass
''')


def test_extract_public_api_real_signatures():
    out = A.extract_public_api(MOD, "pkg.mod")
    assert "def public_fn(payload: Any, *, flag: bool=False) -> 'Optional[int]': ..." in out
    assert "async def afetch(url: str) -> bytes: ..." in out
    assert "class Thing:" in out
    assert "def __init__(self, x: int) -> None: ..." in out
    assert "def method(self, a, b=2) -> int: ..." in out
    # privates excluded
    assert "_private_fn" not in out
    assert "_hidden" not in out
    assert "_PrivateClass" not in out


@pytest.mark.parametrize("bad", ["def broken(:\n", "", "   ", "not python at ("])
def test_extract_public_api_failsoft(bad):
    assert A.extract_public_api(bad) == ""


def test_extract_no_hints_degrades_to_bare_names():
    out = A.extract_public_api("def f(a, b, *args, **kw):\n    return a\n")
    assert "def f(a, b, *args, **kw): ..." in out


def test_collect_and_build_from_description(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    src = tmp_path / "pkg" / "widget.py"
    src.write_text("def build(spec: dict) -> str:\n    return ''\n")
    desc = "author a test at tests/test_widget.py for pkg/widget.py"
    block = A.build_signature_anchor(["tests/test_widget.py"], desc, tmp_path)
    assert "AUTHORITATIVE API SIGNATURES" in block
    assert "def build(spec: dict) -> str: ..." in block


def test_resolves_module_under_test_from_name(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text("def go() -> int:\n    return 1\n")
    # no description path — must resolve widget.py from test_widget.py alone
    block = A.build_signature_anchor(["tests/test_widget.py"], "author a test", tmp_path)
    assert "def go() -> int: ..." in block


def test_disabled_flag_yields_empty(tmp_path, monkeypatch):
    (tmp_path / "w.py").write_text("def f() -> int:\n    return 1\n")
    monkeypatch.setenv("JARVIS_AST_SIGNATURE_ANCHOR_ENABLED", "false")
    assert A.build_signature_anchor(["tests/test_w.py"], "w.py", tmp_path) == ""


def test_nothing_resolves_yields_empty(tmp_path):
    assert A.build_signature_anchor(["tests/test_nonexistent_xyz.py"], "no path here", tmp_path) == ""


def test_never_raises_on_garbage(tmp_path):
    assert A.build_signature_anchor(None, None, tmp_path) == ""  # type: ignore[arg-type]
