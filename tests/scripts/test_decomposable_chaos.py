"""Tests for the DecomposableChaosInjector extension of chaos_injector_ast.py.

The DecomposableChaosInjector acquires + mutates N=3 MUTUALLY-ISOLATED pure-leaf
functions, each in a DIFFERENT source file, such that NO two of their files are
import-coupled. This guarantees the L3 AST Collision Matrix marks them pairwise
disjoint -> a clean 3-way parallel subagent fan-out.

Strategy mirrors test_chaos_injector_ast.py: build a tiny FAKE repo in a tmp dir
with 3 isolated pure-leaf modules (each with a green test) PLUS a coupled pair
(two modules that import each other) as a NEGATIVE control. Run the full
acquire -> inject-N -> verify-each-red -> revert-all flow end-to-end with real
pytest subprocesses, never touching the live repo.

Reuse-first: every test exercises the EXISTING purity/mutation/verify/manifest/
revert primitives via the new N-target orchestration; none of those are rewritten.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import textwrap

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODULE_PATH = os.path.join(_REPO_ROOT, "scripts", "chaos_injector_ast.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("chaos_injector_ast", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["chaos_injector_ast"] = mod
    spec.loader.exec_module(mod)
    return mod


cia = _load_module()


# --------------------------------------------------------------------------- #
# Fake-repo builders.
# --------------------------------------------------------------------------- #

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _build_isolated_repo(tmp_path, *, n_isolated=3, with_coupled_pair=True):
    """Build a repo with ``n_isolated`` mutually-isolated pure-leaf modules
    (each with a green test) and, optionally, a coupled pair (two modules where
    one imports the other) as a negative control.

    Returns (repo_root, {modname: abs_path}).
    """
    repo = tmp_path / "repo"
    pkg = repo / "backend" / "utils"
    _write(str(repo / "backend" / "__init__.py"), "")
    _write(str(pkg / "__init__.py"), "")
    tests = repo / "tests"
    _write(str(tests / "__init__.py"), "")

    modpaths = {}
    # Each isolated module imports NOTHING from sibling modules. They share only
    # the stdlib (none here) so the import graph has zero cross-edges.
    for i in range(n_isolated):
        name = f"calc{i}"
        fn = f"op{i}"
        _write(
            str(pkg / f"{name}.py"),
            textwrap.dedent(f"""\
                from __future__ import annotations


                def {fn}(a, b):
                    return a + b + {i}
            """),
        )
        _write(
            str(tests / f"test_{name}.py"),
            textwrap.dedent(f"""\
                from backend.utils.{name} import {fn}


                def test_{fn}():
                    assert {fn}(2, 3) == {5 + i}
            """),
        )
        modpaths[name] = str(pkg / f"{name}.py")

    if with_coupled_pair:
        # coupled_a defines a pure leaf; coupled_b IMPORTS coupled_a. Both have
        # green tests, so both look individually viable -- but the pair must
        # NEVER be co-selected because they are import-coupled.
        _write(
            str(pkg / "coupled_a.py"),
            textwrap.dedent("""\
                from __future__ import annotations


                def adda(a, b):
                    return a * b
            """),
        )
        _write(
            str(pkg / "coupled_b.py"),
            textwrap.dedent("""\
                from __future__ import annotations

                from backend.utils.coupled_a import adda


                def addb(a, b):
                    return a - b
            """),
        )
        _write(
            str(tests / "test_coupled_a.py"),
            textwrap.dedent("""\
                from backend.utils.coupled_a import adda


                def test_adda():
                    assert adda(3, 4) == 12
            """),
        )
        _write(
            str(tests / "test_coupled_b.py"),
            textwrap.dedent("""\
                from backend.utils.coupled_b import addb


                def test_addb():
                    assert addb(10, 4) == 6
            """),
        )
        modpaths["coupled_a"] = str(pkg / "coupled_a.py")
        modpaths["coupled_b"] = str(pkg / "coupled_b.py")

    return str(repo), modpaths


def _cfg(repo, **kw):
    return cia.InjectConfig(repo_root=str(repo), **kw)


# --------------------------------------------------------------------------- #
# Import-graph coupling primitive (bounded AST scan).
# --------------------------------------------------------------------------- #

def test_module_dotted_name_from_path():
    repo, mods = _build_isolated_repo(_tmp(), n_isolated=1, with_coupled_pair=False)
    name = cia._module_dotted_name(repo, mods["calc0"])
    assert name == "backend.utils.calc0"


def test_file_imports_extracts_dotted_targets(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=True)
    imports = cia._file_import_targets(mods["coupled_b"])
    # coupled_b imports coupled_a.
    assert "backend.utils.coupled_a" in imports


def test_files_are_import_coupled_detects_pair(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=True)
    # b imports a -> coupled.
    assert cia._files_are_import_coupled(repo, mods["coupled_a"], mods["coupled_b"]) is True
    # symmetric: order should not matter.
    assert cia._files_are_import_coupled(repo, mods["coupled_b"], mods["coupled_a"]) is True


def test_isolated_files_not_coupled(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=False)
    assert cia._files_are_import_coupled(repo, mods["calc0"], mods["calc1"]) is False
    assert cia._files_are_import_coupled(repo, mods["calc1"], mods["calc2"]) is False
    assert cia._files_are_import_coupled(repo, mods["calc0"], mods["calc2"]) is False


# --------------------------------------------------------------------------- #
# acquire_isolated_targets.
# --------------------------------------------------------------------------- #

def test_acquire_isolated_targets_returns_3_distinct_files(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=True)
    cfg = _cfg(repo, seed=0)
    targets = cia.acquire_isolated_targets(cfg, n=3)
    assert len(targets) == 3
    files = [t.target_file for t in targets]
    # All three in DIFFERENT files.
    assert len(set(files)) == 3


def test_acquire_isolated_targets_are_pairwise_uncoupled(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=True)
    cfg = _cfg(repo, seed=0)
    targets = cia.acquire_isolated_targets(cfg, n=3)
    files = [t.target_file for t in targets]
    # ASSERT pairwise isolation via the real import graph.
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            assert cia._files_are_import_coupled(repo, files[i], files[j]) is False


def test_acquire_isolated_targets_excludes_coupled_pair(tmp_path):
    # Repo where ONLY a coupled pair (plus 1 isolated) exists: cannot form a
    # set of 3 that includes both coupled files. The selection must never pick
    # two files that import each other.
    repo, mods = _build_isolated_repo(tmp_path, n_isolated=1, with_coupled_pair=True)
    cfg = _cfg(repo, seed=0)
    targets = cia.acquire_isolated_targets(cfg, n=3)
    files = set(t.target_file for t in targets)
    # If both coupled files were selected they'd be coupled -> forbidden.
    assert not (mods["coupled_a"] in files and mods["coupled_b"] in files)


def test_acquire_fewer_when_not_enough_isolated(tmp_path):
    # Only 2 isolated leaves available (+ coupled pair which can contribute at
    # most ONE of its two files). Request 3 -> honestly return fewer, never
    # fabricate.
    repo, mods = _build_isolated_repo(tmp_path, n_isolated=2, with_coupled_pair=False)
    cfg = _cfg(repo, seed=0)
    targets = cia.acquire_isolated_targets(cfg, n=3)
    assert len(targets) == 2  # honest: fewer, not fabricated
    assert len(set(t.target_file for t in targets)) == 2


# --------------------------------------------------------------------------- #
# inject_decomposable: all N go red + N-entry manifest.
# --------------------------------------------------------------------------- #

def test_inject_decomposable_turns_all_three_red(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=True)
    cfg = _cfg(repo, seed=0, now_iso="2026-06-25T00:00:00Z", test_timeout_s=120.0)

    rc = cia.do_inject_decomposable(cfg, n=3)
    assert rc == 0, "decomposable inject should turn all 3 tests red"

    manifest = cia._read_manifest(str(repo))
    assert manifest is not None
    assert manifest.get("schema_version") == 2
    targets = manifest["targets"]
    assert len(targets) == 3
    # Every target turned its OWN test red.
    for t in targets:
        assert t["test_red_post"] is True
        assert t["test_was_green_pre"] is True
    # Three distinct files.
    assert len({t["target_file"] for t in targets}) == 3

    cia.do_revert(cfg)


def test_inject_decomposable_files_actually_mutated(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=False)
    before = {name: open(p, "rb").read() for name, p in mods.items()}
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    rc = cia.do_inject_decomposable(cfg, n=3)
    assert rc == 0
    manifest = cia._read_manifest(str(repo))
    mutated_files = {t["target_file_abs"] for t in manifest["targets"]}
    # Each mutated file differs from its original bytes.
    for abs_path in mutated_files:
        name = os.path.splitext(os.path.basename(abs_path))[0]
        assert open(abs_path, "rb").read() != before[name]
    cia.do_revert(cfg)


# --------------------------------------------------------------------------- #
# revert-ALL byte-identical (incl partial-failure cleanup, no half-state).
# --------------------------------------------------------------------------- #

def test_revert_restores_all_three_byte_identical(tmp_path):
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=False)
    before = {name: open(p, "rb").read() for name, p in mods.items()}
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)

    assert cia.do_inject_decomposable(cfg, n=3) == 0
    # Mutated on disk.
    assert any(open(p, "rb").read() != before[name] for name, p in mods.items())

    assert cia.do_revert(cfg) == 0
    # ALL restored byte-identically.
    for name, p in mods.items():
        assert open(p, "rb").read() == before[name]
    assert cia._read_manifest(str(repo)) is None


def test_partial_failure_leaves_no_half_state(tmp_path, monkeypatch):
    # Force the THIRD target to fail to go red: monkeypatch the post-mutation
    # pytest run so the 3rd injection cannot be confirmed red. The orchestration
    # must revert ALL already-injected targets (no half-injected state) and
    # honestly report fewer / failure -- never leave files mutated.
    repo, mods = _build_isolated_repo(tmp_path, with_coupled_pair=False)
    before = {name: open(p, "rb").read() for name, p in mods.items()}
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)

    # Make EVERY post-mutation verification report "still green" (inert) so NO
    # target can ever turn red -> the whole decomposable inject must fail and
    # leave zero mutations on disk.
    real_run = cia._run_pytest_node
    call = {"n": 0}

    def fake_run(repo_root, node_id, timeout_s):
        # Pre-injection green checks must pass; post-injection checks (after a
        # file write) must report green (inert) so red is never produced.
        # We detect post-injection by checking if any target file currently
        # differs from its original on disk.
        for name, p in mods.items():
            if open(p, "rb").read() != before[name]:
                return True  # report still-green => inert => fail to go red
        return real_run(repo_root, node_id, timeout_s)

    monkeypatch.setattr(cia, "_run_pytest_node", fake_run)

    rc = cia.do_inject_decomposable(cfg, n=3)
    assert rc != 0  # could not produce 3 reds
    # CRITICAL: no half-state -- every file is byte-identical to its original.
    for name, p in mods.items():
        assert open(p, "rb").read() == before[name], f"{name} left mutated!"
    # No active manifest left behind.
    assert cia._read_manifest(str(repo)) is None


# --------------------------------------------------------------------------- #
# Honesty: fewer-than-N isolated leaves available => fewer targets, no fabricate.
# --------------------------------------------------------------------------- #

def test_inject_decomposable_honest_when_fewer_available(tmp_path):
    # Only 2 isolated leaves: a strict n=3 request can't be satisfied. Default
    # behaviour: inject the 2 it CAN isolate and report honestly (rc==0 with a
    # 2-entry manifest), never fabricate a 3rd.
    repo, mods = _build_isolated_repo(tmp_path, n_isolated=2, with_coupled_pair=False)
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    rc = cia.do_inject_decomposable(cfg, n=3, require_exact=False)
    assert rc == 0
    manifest = cia._read_manifest(str(repo))
    assert manifest["schema_version"] == 2
    assert len(manifest["targets"]) == 2  # honest fewer
    cia.do_revert(cfg)


def test_inject_decomposable_require_exact_refuses_fewer(tmp_path):
    # With require_exact=True and only 2 isolated leaves, refuse rather than
    # inject a partial set. No mutation left on disk.
    repo, mods = _build_isolated_repo(tmp_path, n_isolated=2, with_coupled_pair=False)
    before = {name: open(p, "rb").read() for name, p in mods.items()}
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    rc = cia.do_inject_decomposable(cfg, n=3, require_exact=True)
    assert rc != 0
    for name, p in mods.items():
        assert open(p, "rb").read() == before[name]
    assert cia._read_manifest(str(repo)) is None


# --------------------------------------------------------------------------- #
# Back-compat: single-target manifest still reverts via do_revert.
# --------------------------------------------------------------------------- #

def test_single_target_manifest_still_reverts(tmp_path):
    # The existing single-target inject writes a schema_version==1 manifest;
    # do_revert must still handle it (back-compat).
    repo, mods = _build_isolated_repo(tmp_path, n_isolated=1, with_coupled_pair=False)
    before = open(mods["calc0"], "rb").read()
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    rc = cia.do_inject(cfg)  # legacy single-target path
    assert rc == 0
    m = cia._read_manifest(str(repo))
    assert m.get("schema_version") == 1  # legacy shape preserved
    assert cia.do_revert(cfg) == 0
    assert open(mods["calc0"], "rb").read() == before


# --------------------------------------------------------------------------- #
# tiny tmp_path helper for the one non-fixture test above.
# --------------------------------------------------------------------------- #

def _tmp():
    import tempfile
    import pathlib
    return pathlib.Path(tempfile.mkdtemp())
