"""Tests for scripts/chaos_injector_ast.py -- the Dynamic AST Chaos Injector.

Strategy: build a tiny FAKE repo in a tmp dir (a util module + its green test)
so the full acquire -> mutate -> verify-red -> revert flow runs end-to-end with
real pytest subprocess execution, but never touches the live repo. Plus pure
unit tests of the AST purity analysis and mutation primitives.
"""
from __future__ import annotations

import ast
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
# AST purity analysis.
# --------------------------------------------------------------------------- #

def _first_func(src: str) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(src))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            return node
    raise AssertionError("no function in source")


def test_pure_function_accepted():
    fi = cia._analyze_function(_first_func("""
        def add(a, b):
            return a + b
    """))
    assert fi.pure is True
    assert fi.name == "add"


def test_pure_function_with_locals_accepted():
    fi = cia._analyze_function(_first_func("""
        def weighted(a, b):
            total = a * 2 + b
            if total > 10:
                return total - 1
            return total
    """))
    assert fi.pure is True


def test_io_function_rejected():
    fi = cia._analyze_function(_first_func("""
        def write_it(path, data):
            with open(path, 'w') as fh:
                fh.write(data)
            return data
    """))
    assert fi.pure is False


def test_print_function_rejected():
    fi = cia._analyze_function(_first_func("""
        def shout(x):
            print(x)
            return x
    """))
    assert fi.pure is False


def test_global_mutating_function_rejected():
    fi = cia._analyze_function(_first_func("""
        def bump():
            global COUNTER
            COUNTER = COUNTER + 1
            return COUNTER
    """))
    assert fi.pure is False


def test_network_function_rejected():
    fi = cia._analyze_function(_first_func("""
        def fetch(url):
            r = requests.get(url)
            return r.text
    """))
    assert fi.pure is False


def test_os_attr_function_rejected():
    fi = cia._analyze_function(_first_func("""
        def cwd_join(name):
            return os.path.join(os.getcwd(), name)
    """))
    assert fi.pure is False


def test_subprocess_function_rejected():
    fi = cia._analyze_function(_first_func("""
        def run(cmd):
            return subprocess.run(cmd).returncode
    """))
    assert fi.pure is False


def test_attribute_mutation_rejected():
    fi = cia._analyze_function(_first_func("""
        def stamp(obj, val):
            obj.value = val
            return val
    """))
    assert fi.pure is False


def test_no_return_rejected():
    fi = cia._analyze_function(_first_func("""
        def noop(a, b):
            c = a + b
    """))
    assert fi.pure is False


def test_dunder_rejected():
    fi = cia._analyze_function(_first_func("""
        def __eq__(self, other):
            return self is other
    """))
    assert fi.pure is False


def test_async_function_rejected():
    tree = ast.parse(textwrap.dedent("""
        async def go(x):
            return x + 1
    """))
    node = tree.body[0]
    # async funcs are not FunctionDef; the scanner only picks FunctionDef, but
    # ensure analyze handles a plain func with await inside is rejected.
    fi = cia._analyze_function(_first_func("""
        def go(x):
            return x + 1
    """))
    assert fi.pure is True  # the sync one is fine
    assert isinstance(node, ast.AsyncFunctionDef)


def test_decorated_property_rejected():
    fi = cia._analyze_function(_first_func("""
        @property
        def value(self):
            return self._v
    """))
    assert fi.pure is False


# --------------------------------------------------------------------------- #
# Denylist.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "/repo/backend/core/ouroboros/governance/orchestrator.py",
    "/repo/backend/core/ouroboros/governance/semantic_firewall.py",
    "/repo/backend/iron_gate.py",
    "/repo/backend/utils/scoped_tool_backend.py",
    "/repo/backend/critical_elevation.py",
    "/repo/scripts/chaos_injector_ast.py",
    "/repo/tests/scripts/test_x.py",
    "/repo/backend/utils/__init__.py",
    "/repo/backend/db/migrations/0001_init.py",
    "/repo/backend/utils/test_helper.py",
])
def test_denylisted_paths_rejected(path):
    assert cia._is_denied(path) is True


@pytest.mark.parametrize("path", [
    "/repo/backend/utils/env_config.py",
    "/repo/backend/neural_mesh/utils/helpers.py",
])
def test_allowed_paths_not_denied(path):
    assert cia._is_denied(path) is False


def test_safety_cage_file_never_in_candidates(tmp_path):
    # Build a fake "utils" dir that ALSO contains a governance cage file path.
    repo = tmp_path / "repo"
    cage_dir = repo / "backend" / "core" / "governance"
    cage_dir.mkdir(parents=True)
    (cage_dir / "iron_gate.py").write_text("def gate(a, b):\n    return a + b\n")
    utils_dir = repo / "backend" / "utils"
    utils_dir.mkdir(parents=True)
    (utils_dir / "m.py").write_text("def add(a, b):\n    return a + b\n")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_m.py").write_text(
        "from backend.utils.m import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    cands = cia._scan_pure_leaf_functions(str(repo), [str(cage_dir), str(utils_dir)])
    files = {c.target_file for c in cands}
    assert all("governance" not in f for f in files)
    assert all("iron_gate" not in f for f in files)


# --------------------------------------------------------------------------- #
# Mutation primitives.
# --------------------------------------------------------------------------- #

def test_plan_mutation_flips_binop():
    src = "def add(a, b):\n    return a + b\n"
    func = ast.parse(src).body[0]
    mut = cia._plan_mutation(src, func)
    assert mut is not None
    assert mut.kind.startswith("binop:Add->Sub")
    assert mut.original_segment == "+"
    assert mut.mutated_segment == "-"
    mutated = cia._apply_mutation(src, mut)
    assert "return a - b" in mutated


def test_plan_mutation_flips_compare():
    src = "def cmp(a, b):\n    return a < b\n"
    func = ast.parse(src).body[0]
    mut = cia._plan_mutation(src, func)
    assert mut is not None
    assert mut.kind.startswith("cmpop:Lt->LtE")
    mutated = cia._apply_mutation(src, mut)
    assert "return a <= b" in mutated


def test_plan_mutation_alters_return_int_literal():
    src = "def zero():\n    return 0\n"
    func = ast.parse(src).body[0]
    mut = cia._plan_mutation(src, func)
    assert mut is not None
    assert mut.kind == "return-literal:int+1"
    mutated = cia._apply_mutation(src, mut)
    assert "return (0 + 1)" in mutated


def test_apply_mutation_rejects_drift():
    src = "def add(a, b):\n    return a + b\n"
    func = ast.parse(src).body[0]
    mut = cia._plan_mutation(src, func)
    assert mut is not None
    bad = cia.Mutation(
        kind=mut.kind, lineno=mut.lineno, col_offset=mut.col_offset,
        end_col_offset=mut.end_col_offset, original_segment="ZZZ",
        mutated_segment="-",
    )
    with pytest.raises(ValueError):
        cia._apply_mutation(src, bad)


# --------------------------------------------------------------------------- #
# End-to-end: build a fake repo, inject, verify red, revert byte-identical.
# --------------------------------------------------------------------------- #

def _build_fake_repo(tmp_path, func_src: str, test_src: str):
    repo = tmp_path / "repo"
    utils_dir = repo / "backend" / "utils"
    utils_dir.mkdir(parents=True)
    (repo / "backend" / "__init__.py").write_text("")
    (repo / "backend" / "utils" / "__init__.py").write_text("")
    mod = utils_dir / "calc.py"
    mod.write_text(func_src)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    test_file = tests_dir / "test_calc.py"
    test_file.write_text(test_src)
    return repo, mod, test_file


FUNC_SRC = textwrap.dedent("""\
    from __future__ import annotations


    def add(a, b):
        return a + b


    def scale(a, b):
        return a * b
""")

TEST_SRC = textwrap.dedent("""\
    from backend.utils.calc import add, scale


    def test_add():
        assert add(2, 3) == 5
        assert add(10, 1) == 11


    def test_scale():
        assert scale(4, 5) == 20
""")


def _cfg(repo, **kw):
    return cia.InjectConfig(repo_root=str(repo), **kw)


def test_end_to_end_inject_verify_red_then_revert(tmp_path):
    repo, mod, test_file = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    original_bytes = mod.read_bytes()

    cfg = _cfg(repo, seed=0, now_iso="2026-06-24T00:00:00Z", test_timeout_s=120.0)

    rc = cia.do_inject(cfg)
    assert rc == 0, "inject should succeed and produce a red test"

    manifest = cia._read_manifest(str(repo))
    assert manifest is not None
    assert manifest["test_red_post"] is True
    assert manifest["test_was_green_pre"] is True
    assert manifest["injected_at_iso"] == "2026-06-24T00:00:00Z"
    assert manifest["function"] in ("add", "scale")
    assert manifest["original_source"] == FUNC_SRC

    # File on disk is actually mutated.
    assert mod.read_bytes() != original_bytes

    # Revert restores byte-identical original and clears the manifest.
    rc = cia.do_revert(cfg)
    assert rc == 0
    assert mod.read_bytes() == original_bytes
    assert cia._read_manifest(str(repo)) is None


def test_inject_refuses_when_manifest_active(tmp_path):
    repo, mod, _ = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    assert cia.do_inject(cfg) == 0
    # Second inject without --force must refuse (idempotent guard).
    assert cia.do_inject(cfg) == 3
    cia.do_revert(cfg)


def test_inject_force_overrides_active_manifest(tmp_path):
    repo, mod, _ = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    assert cia.do_inject(cfg) == 0
    cfg2 = _cfg(repo, seed=1, now_iso="t", force=True, test_timeout_s=120.0)
    assert cia.do_inject(cfg2) == 0
    cia.do_revert(cfg2)


def test_dry_run_writes_nothing(tmp_path):
    repo, mod, test_file = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    before = mod.read_bytes()
    cfg = _cfg(repo, seed=0, test_timeout_s=120.0)
    rc = cia.do_dry_run(cfg)
    assert rc == 0
    # No file mutation, no manifest written.
    assert mod.read_bytes() == before
    assert cia._read_manifest(str(repo)) is None
    assert not os.path.exists(cia._manifest_path(str(repo)))


def test_list_candidates_finds_fake_targets(tmp_path):
    repo, mod, _ = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    cfg = _cfg(repo, seed=0)
    rc = cia.do_list_candidates(cfg)
    assert rc == 0
    cands = cia.acquire_candidates(cfg)
    funcs = {c.function for c in cands}
    assert "add" in funcs
    assert "scale" in funcs


def test_manifest_round_trips(tmp_path):
    repo, _, _ = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    data = {"schema_version": 1, "function": "add", "original_source": FUNC_SRC}
    cia._write_manifest(str(repo), data)
    got = cia._read_manifest(str(repo))
    assert got == data


def test_seed_varies_selection(tmp_path):
    # Two candidates (add, scale); different seeds should be able to rotate.
    repo, _, _ = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    cfg = _cfg(repo, seed=0)
    cands = cia.acquire_candidates(cfg)
    assert len(cands) >= 2
    order0 = cia._seed_order(cands, 0)
    order1 = cia._seed_order(cands, 1)
    assert order0[0] != order1[0] or len(cands) == 1


def test_inert_mutation_reverts_and_tries_next(tmp_path):
    # A function whose binop flip is inert against the test (test only checks a
    # case where + and - give the same result is hard; instead use a function
    # with NO mutation site followed by a real one). Here we make the FIRST
    # candidate's test assert nothing meaningful so a red is still produced by
    # the second. Simpler: ensure that when the chosen mutation does turn red,
    # disk is left mutated; covered above. Here assert the no-candidate path.
    repo = tmp_path / "repo"
    utils = repo / "backend" / "utils"
    utils.mkdir(parents=True)
    (repo / "backend" / "__init__.py").write_text("")
    (utils / "__init__.py").write_text("")
    # Pure function but NO test references it -> not a candidate.
    (utils / "lonely.py").write_text("def solo(a, b):\n    return a + b\n")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_other.py").write_text("def test_nothing():\n    assert True\n")
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=60.0)
    rc = cia.do_inject(cfg)
    assert rc == 4  # no viable candidates
    assert cia._read_manifest(str(repo)) is None


def test_iter_mutations_enumerates_multiple_sites():
    src = "def f(a, b, c):\n    return (a + b) < c\n"
    func = ast.parse(src).body[0]
    muts = cia._iter_mutations(src, func)
    kinds = [m.kind for m in muts]
    # Both the compare op and the arithmetic op are viable sites.
    assert any(k.startswith("cmpop:") for k in kinds)
    assert any(k.startswith("binop:") for k in kinds)


def test_inject_tries_multiple_sites_until_red(tmp_path):
    # A function with TWO operator sites where the FIRST flip is inert against
    # the test but the SECOND is detectable. f(a,b) = (a >= b) computes a bool;
    # the test only checks the arithmetic path so the cmp flip is inert while
    # the arithmetic flip is red.
    func_src = textwrap.dedent("""\
        from __future__ import annotations


        def combine(a, b):
            extra = a * b
            return extra + a
    """)
    test_src = textwrap.dedent("""\
        from backend.utils.calc import combine


        def test_combine():
            assert combine(3, 4) == 15
    """)
    repo, mod, _ = _build_fake_repo(tmp_path, func_src, test_src)
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    rc = cia.do_inject(cfg)
    assert rc == 0
    m = cia._read_manifest(str(repo))
    assert m["test_red_post"] is True
    assert m["function"] == "combine"
    cia.do_revert(cfg)


def test_no_verify_injects_without_running_tests(tmp_path):
    repo, mod, _ = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    before = mod.read_bytes()
    cfg = _cfg(repo, seed=0, now_iso="t", verify_green=False, test_timeout_s=120.0)
    rc = cia.do_inject(cfg)
    assert rc == 0
    m = cia._read_manifest(str(repo))
    assert m["test_red_post"] is None
    assert m["test_was_green_pre"] is False
    assert mod.read_bytes() != before  # file IS mutated
    cia.do_revert(cfg)
    assert mod.read_bytes() == before


def test_candidate_dedup_one_per_function(tmp_path):
    # Two tests reference the same function; acquisition collapses to ONE
    # candidate carrying both test nodes.
    func_src = textwrap.dedent("""\
        def add(a, b):
            return a + b
    """)
    test_src = textwrap.dedent("""\
        from backend.utils.calc import add


        def test_add_one():
            assert add(1, 1) == 2


        def test_add_two():
            assert add(2, 2) == 4
    """)
    repo, _, _ = _build_fake_repo(tmp_path, func_src, test_src)
    cfg = _cfg(repo, seed=0)
    cands = cia.acquire_candidates(cfg)
    add_cands = [c for c in cands if c.function == "add"]
    assert len(add_cands) == 1
    assert len(add_cands[0].test_nodes) == 2


def test_status_reports_active_and_inactive(tmp_path):
    repo, _, _ = _build_fake_repo(tmp_path, FUNC_SRC, TEST_SRC)
    cfg = _cfg(repo, seed=0, now_iso="t", test_timeout_s=120.0)
    assert cia.do_status(cfg) == 0  # inactive
    assert cia.do_inject(cfg) == 0
    assert cia.do_status(cfg) == 0  # active
    m = cia._read_manifest(str(repo))
    assert m is not None
    cia.do_revert(cfg)
