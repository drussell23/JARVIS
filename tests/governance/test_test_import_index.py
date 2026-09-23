"""The test-import map is bounded and content-addressed.

Soak bt-2026-09-23-005910's growth tracer named ``test_runner`` import-map
assembly as the top heap growth (~450 MB/h): the map was cached per tree
PATH, and every L2 sandbox / test-writing candidate is a fresh /tmp path,
so each pinned a ~6 MB map for the process lifetime and re-parsed every
test file to build it.
"""
from __future__ import annotations

import ast
import asyncio
import shutil
from pathlib import Path
from typing import Dict, List

import pytest

from backend.core.ouroboros.governance import test_import_index as tii
from backend.core.ouroboros.governance import test_runner as tr
from backend.core.ouroboros.governance.target_stratification import (
    _strat_build_ast_map,
)

DIRS = frozenset({"tests"})


def _tree(root: Path, n: int = 6) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    for i in range(n):
        (root / "pkg" / f"m{i}.py").write_text(f"X{i} = {i}\n")
    tests = root / "tests"
    (tests / "sub").mkdir(parents=True)
    for i in range(n):
        (tests / f"test_m{i}.py").write_text(
            f"import os\nfrom pkg import m{i}\nfrom pkg.m{i} import X{i}\n"
            f"import pkg.m{i}\nfrom pkg import m{i}  # duplicate\n"
        )
    (tests / "sub" / "test_rel.py").write_text("from . import helper\nimport json\n")
    (tests / "test_broken.py").write_text("def (:\n")
    return root


def _legacy_map(repo_root: Path) -> Dict[str, List[Path]]:
    """The builder this replaced, verbatim in behaviour (the oracle)."""
    out: Dict[str, List[Path]] = {}

    def reg(key, f):
        if key:
            lst = out.setdefault(key, [])
            if f not in lst:
                lst.append(f)

    for tdn in sorted(DIRS):
        for f in sorted((repo_root / tdn).rglob("test_*.py")):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        reg(a.name, f)
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod:
                        reg(mod, f)
                    for a in node.names:
                        reg(f"{mod}.{a.name}" if mod else a.name, f)
    return out


@pytest.fixture(autouse=True)
def _clean():
    tii.reset_for_tests()
    tr._ast_import_cache.clear()
    tr._ast_import_inflight.clear()
    yield
    tii.reset_for_tests()
    tr._ast_import_cache.clear()
    tr._ast_import_inflight.clear()


@pytest.fixture
def parse_count(monkeypatch):
    calls = {"n": 0}
    real = tii.imported_names

    def counting(source, filename="<test>"):
        calls["n"] += 1
        return real(source, filename)

    monkeypatch.setattr(tii, "imported_names", counting)
    return calls


def test_output_matches_the_legacy_builder(tmp_path):
    root = _tree(tmp_path / "r")
    assert tii.build_import_map(root, DIRS) == _legacy_map(root)
    assert tr._build_test_import_map(root, DIRS) == _legacy_map(root)
    assert _strat_build_ast_map(root, DIRS) == _legacy_map(root)


def test_an_identical_tree_elsewhere_is_not_reparsed(tmp_path, parse_count):
    base = _tree(tmp_path / "base")
    tii.build_import_map(base, DIRS)
    first = parse_count["n"]
    assert first == 8  # 6 + rel + broken (a syntax error is a property of the bytes too)
    for i in range(5):
        sandbox = tmp_path / f"jarvis_repair_sandbox_{i}"
        shutil.copytree(base, sandbox)
        m = tii.build_import_map(sandbox, DIRS)
        assert m["pkg.m0"] == [sandbox / "tests" / "test_m0.py"]
    assert parse_count["n"] == first


def test_a_changed_test_file_is_the_only_one_parsed_and_is_seen(tmp_path, parse_count):
    base = _tree(tmp_path / "base")
    tii.build_import_map(base, DIRS)
    before = parse_count["n"]
    sandbox = tmp_path / "sandbox"
    shutil.copytree(base, sandbox)
    (sandbox / "tests" / "test_m0.py").write_text("from pkg import m5\n")
    m = tii.build_import_map(sandbox, DIRS)
    assert parse_count["n"] == before + 1
    assert sandbox / "tests" / "test_m0.py" in m["pkg.m5"]
    assert "pkg.m0" not in m


async def test_per_sandbox_maps_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_IMPORT_MAP_CACHE_MAX", "3")
    base = _tree(tmp_path / "base")
    for i in range(10):
        sandbox = tmp_path / f"sb{i}"
        shutil.copytree(base, sandbox)
        runner = tr.TestRunner(repo_root=sandbox)
        await runner._get_ast_import_map()
    assert len(tr._ast_import_cache) == 3
    assert (tmp_path / "sb9").resolve() in tr._ast_import_cache
    assert (tmp_path / "sb0").resolve() not in tr._ast_import_cache
    assert tr._ast_import_inflight == {}


async def test_file_tier_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_IMPORT_FILE_CACHE_MAX", "4")
    tii.build_import_map(_tree(tmp_path / "r"), DIRS)
    assert len(tii._file_imports) == 4


async def test_a_cancelled_caller_still_caches_and_leaves_nothing_in_flight(tmp_path):
    root = _tree(tmp_path / "r")
    runner = tr.TestRunner(repo_root=root)
    task = asyncio.ensure_future(runner._get_ast_import_map())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(200):
        if root.resolve() in tr._ast_import_cache:
            break
        await asyncio.sleep(0.01)
    assert root.resolve() in tr._ast_import_cache
    assert tr._ast_import_inflight == {}


def test_lru_evicts_least_recently_used():
    lru = tii.BoundedLRU(2)
    lru["a"] = 1
    lru["b"] = 2
    assert lru.get("a") == 1  # touch: b is now oldest
    lru["c"] = 3
    assert "b" not in lru and "a" in lru and "c" in lru
    assert lru.pop("a") == 1 and len(lru) == 1
