"""Regression spine — Slice 6 Task 1: shared import-extraction +
module→path primitives factored from the forward-graph builder
(DRY mandate: one extractor, two consumers)."""
from __future__ import annotations

import ast
import textwrap

from backend.core.ouroboros.governance.reverse_dep_resolver import (
    _build_forward_import_graph,
    build_module_to_path,
    extract_module_imports,
)


def _extract(src: str, module: str = "tests.governance.test_x", is_init: bool = False):
    return extract_module_imports(ast.parse(textwrap.dedent(src)), module, is_init)


def test_plain_import() -> None:
    assert "backend.core.foo" in _extract("import backend.core.foo")


def test_aliased_import() -> None:
    # ``import x as y`` — the alias never obscures the real target (mandate 4)
    assert "backend.core.foo" in _extract("import backend.core.foo as f")


def test_from_import_emits_module_and_qualified() -> None:
    got = _extract("from backend.core.foo import bar")
    assert "backend.core.foo" in got
    assert "backend.core.foo.bar" in got


def test_from_import_aliased_symbol() -> None:
    got = _extract("from backend.core.foo import bar as b")
    assert "backend.core.foo.bar" in got  # alias.name, not asname


def test_relative_import_resolved_against_package() -> None:
    got = extract_module_imports(
        ast.parse("from . import sibling"),
        "backend.core.pkg.mod",
        False,
    )
    assert "backend.core.pkg.sibling" in got


def test_extraction_matches_forward_graph(tmp_path) -> None:
    """The factored extractor and the graph builder must agree — proof the
    refactor didn't fork semantics."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from pkg import b\nimport os\n")
    (pkg / "b.py").write_text("")
    graph = _build_forward_import_graph(str(tmp_path))
    src = (pkg / "a.py").read_text()
    direct = extract_module_imports(ast.parse(src), "pkg.a", False)
    assert graph["pkg.a"] == direct


def test_build_module_to_path(tmp_path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("")
    (tmp_path / "top.py").write_text("")
    mapping = build_module_to_path(str(tmp_path))
    assert mapping["pkg"] == "pkg/__init__.py"
    assert mapping["pkg.mod"] == "pkg/mod.py"
    assert mapping["top"] == "top.py"


def test_build_module_to_path_skips_non_py(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("")
    assert build_module_to_path(str(tmp_path)) == {}
