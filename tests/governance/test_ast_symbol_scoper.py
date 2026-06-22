"""Tests for AstSymbolScoper — Task B1 of the Sovereign Resilience Chunking Matrix.

Pure-AST symbol isolation + syntactic-integrity gate (B1a).
"""
from __future__ import annotations

from backend.core.ouroboros.governance import ast_symbol_scoper as s

SRC = '''
import os
class SemanticIndex:
    def build(self):
        return 1
    def query(self, q):
        return q
def helper():
    return 0
'''


def test_isolates_named_class_method(tmp_path):
    p = tmp_path / "semantic_index.py"
    p.write_text(SRC)
    out = s.isolate_symbols(str(p), "route SemanticIndex.build through subprocess")
    names = {t.symbol for t in out}
    assert "SemanticIndex.build" in names or "SemanticIndex" in names
    assert all(t.symbol == "" or t.lineno > 0 for t in out)


def test_slice_integrity_gate_rejects_severed_decorator():
    assert s.slice_is_valid("def f():\n    return 1\n") is True
    assert s.slice_is_valid("@deco\n") is False          # severed decorator, no def
    assert s.slice_is_valid("    return 1") is False       # orphaned body


def test_parse_failure_degrades_to_whole_file(tmp_path):
    p = tmp_path / "broken.py"
    p.write_text("def (:\n")    # unparseable
    out = s.isolate_symbols(str(p), "fix thing")
    assert len(out) == 1 and out[0].symbol == ""


def test_no_match_degrades_to_whole_file(tmp_path):
    p = tmp_path / "semantic_index.py"
    p.write_text(SRC)
    out = s.isolate_symbols(str(p), "totally unrelated description")
    assert out == (s.ScopedTarget(str(p), "", 0, 0),)


def test_never_execs(monkeypatch):
    import builtins
    monkeypatch.setattr(
        builtins,
        "exec",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("exec called")),
    )
    s.slice_is_valid("def f(): return 1")
