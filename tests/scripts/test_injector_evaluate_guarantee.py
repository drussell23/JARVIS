"""Tests for the evaluate-and-guarantee injection pipeline (chaos_injector_ast.py).

THE BUG (Omni-Soak ``inject:not_red``): the legacy ``_iter_mutations`` carried
only 4 mutation kinds (Compare / BinOp / BoolOp / Return-LITERAL). An
object-returning function (e.g. ``get_stream_renderer``) has NONE of those, so it
yielded "no viable mutation site" -> was DROPPED mid-inject -> only 2 of 3 targets
went red -> ``--require-exact 3`` aborted with ``inject:not_red``.

THE FIX (this suite proves):
  T1 -- expanded TYPE-SAFE mutation arsenal: string-literal alteration, boolean
        flip, if-condition negation, assign-RHS literal/operator flip. return-None
        is type-GUARDED OUT against typed object-returners (would raise a fatal
        TypeError before the assertion runs).
  T2 -- semantic pre-validation: ``has_viable_mutation`` gates pool entry. A
        zero-site function is DISQUALIFIED at selection, never dropped mid-inject.
  T3 -- async ``generate_verified_targets(n)`` yields exactly N verified, isolated,
        green targets, adapting depth-0 -> depth-1 (disjoint depth-0 sub-graphs).
  T4 -- clean teardown: inability to reach N reverts the N-1 already-mutated
        targets byte-identically before raising. No dangling state.
  Constraint 4 -- hyper-observability trace lines emit.

Reuse-first: every test exercises the EXISTING purity/mutation/verify/manifest/
revert primitives via the new evaluate-and-guarantee orchestration. Builds tiny
FAKE repos in tmp dirs; never touches the live repo.
"""
from __future__ import annotations

import asyncio
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


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _func(src, name):
    """Parse ``src`` and return the top-level FunctionDef named ``name``."""
    tree = ast.parse(textwrap.dedent(src))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node, textwrap.dedent(src)
    raise AssertionError("function %r not found" % (name,))


# --------------------------------------------------------------------------- #
# T1 -- expanded TYPE-SAFE mutation arsenal.
# --------------------------------------------------------------------------- #

def test_object_returner_with_if_is_mutatable_typesafe():
    """An object-returning function that has an ``if`` is now mutatable via a
    TYPE-SAFE vector (if-condition negation) -- NOT via return-None."""
    src = """\
        def get_thing(flag):
            if flag:
                return "alpha"
            return "beta"
    """
    func, dsrc = _func(src, "get_thing")
    muts = cia._iter_mutations(dsrc, func)
    assert muts, "expected at least one viable mutation site"
    kinds = [m.kind for m in muts]
    # No return-None vector; an if-negation or string-literal alteration instead.
    assert any(k.startswith("if-negate") or k.startswith("return-literal:str")
               for k in kinds), kinds
    assert not any("return-none" in k for k in kinds), kinds


def test_string_literal_alteration_produces_wrong_value():
    src = """\
        def label():
            return "ok"
    """
    func, dsrc = _func(src, "label")
    muts = cia._iter_mutations(dsrc, func)
    str_muts = [m for m in muts if m.kind.startswith("return-literal:str")]
    assert str_muts
    mutated = cia._apply_mutation(dsrc, str_muts[0])
    # Must still parse (syntactically valid) and change the value.
    ast.parse(mutated)
    assert '"ok"' not in mutated or "_chaos" in mutated


def test_boolean_flip_mutation():
    src = """\
        def is_ready():
            return True
    """
    func, dsrc = _func(src, "is_ready")
    muts = cia._iter_mutations(dsrc, func)
    bool_muts = [m for m in muts if "bool" in m.kind.lower()]
    assert bool_muts
    mutated = cia._apply_mutation(dsrc, bool_muts[0])
    assert "False" in mutated
    ast.parse(mutated)


def test_if_condition_negation_mutation():
    src = """\
        def pick(x):
            if x > 0:
                return "pos"
            return "neg"
    """
    func, dsrc = _func(src, "pick")
    muts = cia._iter_mutations(dsrc, func)
    neg = [m for m in muts if m.kind.startswith("if-negate")]
    assert neg, [m.kind for m in muts]
    mutated = cia._apply_mutation(dsrc, neg[0])
    ast.parse(mutated)
    assert "not (" in mutated


def test_assign_rhs_literal_flip_mutation():
    src = """\
        def compute(x):
            base = 10
            return base + x
    """
    func, dsrc = _func(src, "compute")
    muts = cia._iter_mutations(dsrc, func)
    assigns = [m for m in muts if m.kind.startswith("assign-literal")]
    assert assigns, [m.kind for m in muts]
    mutated = cia._apply_mutation(dsrc, assigns[0])
    ast.parse(mutated)


def test_return_none_skipped_on_typed_object_returner():
    """A strictly-typed object-returner with NO other site is DISQUALIFIED:
    return-None is type-guarded OUT (it would raise a fatal TypeError before the
    assertion runs). The function must yield ZERO sites -> ``inject:not_red``
    avoided by exclusion at selection, not a dud mutation."""
    src = """\
        def get_renderer() -> "Renderer":
            return Renderer()
    """
    func, dsrc = _func(src, "get_renderer")
    muts = cia._iter_mutations(dsrc, func)
    # No comparator/binop/bool/if/assign sites, and return-None is BANNED on an
    # annotated non-Optional return -> zero viable sites.
    assert muts == [], [m.kind for m in muts]


def test_return_none_allowed_when_unannotated_and_no_other_site():
    """When there is NO type annotation and no other vector, return-None is the
    last-resort vector (the call site may still tolerate None, but we cannot prove
    a fatal TypeError, so it is permitted)."""
    src = """\
        def get_obj():
            return make()
    """
    func, dsrc = _func(src, "get_obj")
    muts = cia._iter_mutations(dsrc, func)
    # make() is non-allowlisted so purity fails elsewhere, but _iter_mutations is
    # purity-agnostic; here we just assert the return-None vector is offered.
    assert any("return-none" in m.kind for m in muts), [m.kind for m in muts]


def test_return_none_skipped_when_annotation_is_optional():
    src = """\
        from typing import Optional
        def maybe() -> Optional[int]:
            return compute()
    """
    func, dsrc = _func(src, "maybe")
    muts = cia._iter_mutations(dsrc, func)
    # Optional return -> None is a legal value -> return-None would NOT fail the
    # type system, but it also may not fail the test; still, it is type-SAFE to
    # offer (won't raise a fatal TypeError). Offered.
    assert any("return-none" in m.kind for m in muts), [m.kind for m in muts]


def test_every_mutation_is_syntactically_valid():
    src = """\
        def f(x, flag):
            base = 5
            if flag and x > 0:
                return "a" + str(base)
            return "b"
    """
    func, dsrc = _func(src, "f")
    for m in cia._iter_mutations(dsrc, func):
        mutated = cia._apply_mutation(dsrc, m)
        ast.parse(mutated)  # raises SyntaxError if invalid


# --------------------------------------------------------------------------- #
# T2 -- semantic pre-validation (has_viable_mutation).
# --------------------------------------------------------------------------- #

def test_has_viable_mutation_true_for_object_returner_with_if():
    src = """\
        def get_thing(flag):
            if flag:
                return "alpha"
            return "beta"
    """
    func, dsrc = _func(src, "get_thing")
    assert cia.has_viable_mutation(dsrc, func) is True


def test_has_viable_mutation_false_for_typed_zero_site_returner():
    src = """\
        def get_renderer() -> "Renderer":
            return Renderer()
    """
    func, dsrc = _func(src, "get_renderer")
    assert cia.has_viable_mutation(dsrc, func) is False


def test_prevalidation_excludes_zero_site_function_from_pool(tmp_path):
    """A pure-leaf with a green test but NO viable mutation site must NOT enter
    the candidate pool (disqualified at selection, not dropped mid-inject)."""
    repo, mods = _build_typed_zero_site_repo(tmp_path)
    cfg = cia.InjectConfig(repo_root=repo, verify_green=False)
    cands = cia.acquire_candidates(cfg)
    names = {c.function for c in cands}
    # The mutatable one is in; the zero-site one is out.
    assert "good_op" in names
    assert "frozen_obj" not in names


# --------------------------------------------------------------------------- #
# T3 -- async generate_verified_targets + adaptive depth.
# --------------------------------------------------------------------------- #

def test_generate_verified_targets_yields_exactly_n(tmp_path):
    repo, _ = _build_isolated_object_repo(tmp_path, n=4)
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=60.0)

    async def _drive():
        out = []
        async for t in cia.generate_verified_targets(cfg, 3):
            out.append(t)
        return out

    targets = asyncio.run(_drive())
    assert len(targets) == 3
    files = [t.target_file for t in targets]
    assert len(set(files)) == 3
    # Pairwise import-disjoint.
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            assert cia._files_are_import_coupled(repo, files[i], files[j]) is False


def test_generate_verified_targets_stops_at_n_even_with_more(tmp_path):
    repo, _ = _build_isolated_object_repo(tmp_path, n=6)
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=60.0)

    async def _drive():
        out = []
        async for t in cia.generate_verified_targets(cfg, 2):
            out.append(t)
        return out

    targets = asyncio.run(_drive())
    assert len(targets) == 2


def test_adaptive_depth1_expansion_with_disjoint_deps(tmp_path, capsys):
    """When depth-0 pure-leaves are exhausted before N, shift to depth-1 nodes
    and require pairwise-DISJOINT depth-0 dependency sub-graphs."""
    repo = _build_depth1_repo(tmp_path)
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=90.0)

    async def _drive():
        out = []
        async for t in cia.generate_verified_targets(cfg, 2):
            out.append(t)
        return out

    targets = asyncio.run(_drive())
    assert len(targets) == 2
    err = capsys.readouterr().err
    # The adaptive gear-shift line must have fired.
    assert "[ChaosInjector][adaptive]" in err
    # Disjoint depth-0 deps: the two selected depth-1 funcs must not share a dep.
    deps = [cia._depth0_dependency_set(repo, t.target_file, t.function) for t in targets]
    assert deps[0].isdisjoint(deps[1]), (deps[0], deps[1])


def test_depth1_sharing_a_dep_are_not_co_selected(tmp_path):
    """Two depth-1 functions that SHARE a depth-0 dependency are NOT co-selected
    (else the swarm agents collide fixing the shared dep)."""
    repo = _build_depth1_shared_dep_repo(tmp_path)
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=90.0)

    async def _drive():
        out = []
        async for t in cia.generate_verified_targets(cfg, 2):
            out.append(t)
        return out

    targets = asyncio.run(_drive())
    # Only one of the two shared-dep depth-1 funcs can be selected (or zero if no
    # disjoint pair exists). Never both.
    fns = {t.function for t in targets}
    assert not ({"uses_shared_a", "uses_shared_b"} <= fns)


# --------------------------------------------------------------------------- #
# T4 -- clean teardown (revert N-1 on failure).
# --------------------------------------------------------------------------- #

def test_inability_to_reach_n_reverts_prior_targets(tmp_path, monkeypatch):
    """If the pipeline cannot reach the Nth target, the N-1 already-mutated
    targets are reverted byte-identically BEFORE the failure surfaces."""
    repo, mods = _build_isolated_object_repo(tmp_path, n=2)
    # Capture originals.
    originals = {p: open(p).read() for p in mods.values()}
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=60.0, now_iso="t0")

    # Ask for 3 when only 2 isolated targets exist AND require_exact -> failure.
    rc = cia.do_inject_decomposable(cfg, n=3, require_exact=True)
    assert rc != 0
    # Every file is byte-identical to its original (N-1 cleanup).
    for p, orig in originals.items():
        assert open(p).read() == orig, "file %s left in mutated state" % p
    # No manifest lingering.
    assert cia._read_manifest(repo) is None


def test_generator_cleanup_on_interrupt(tmp_path):
    """The async generator's interrupt path does not leave mutated state (it only
    mutates during inject, not during generation -- generation is read-only +
    green checks). This asserts generation itself never writes."""
    repo, mods = _build_isolated_object_repo(tmp_path, n=3)
    originals = {p: open(p).read() for p in mods.values()}
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=60.0)

    async def _drive():
        gen = cia.generate_verified_targets(cfg, 3)
        # Consume only one then close early.
        await gen.__anext__()
        await gen.aclose()

    asyncio.run(_drive())
    for p, orig in originals.items():
        assert open(p).read() == orig


# --------------------------------------------------------------------------- #
# Constraint 4 -- hyper-observability trace lines.
# --------------------------------------------------------------------------- #

def test_prevalidate_trace_line_emits_with_reason(tmp_path, capsys):
    repo, _ = _build_typed_zero_site_repo(tmp_path)
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=60.0, verbose=True)

    async def _drive():
        out = []
        async for t in cia.generate_verified_targets(cfg, 1):
            out.append(t)
        return out

    asyncio.run(_drive())
    err = capsys.readouterr().err
    assert "[ChaosInjector][prevalidate]" in err
    # The exact reason for the zero-site typed returner.
    assert "no_mutation_site" in err


def test_yield_trace_line_emits(tmp_path, capsys):
    repo, _ = _build_isolated_object_repo(tmp_path, n=3)
    cfg = cia.InjectConfig(repo_root=repo, test_timeout_s=60.0, verbose=True)

    async def _drive():
        out = []
        async for t in cia.generate_verified_targets(cfg, 2):
            out.append(t)
        return out

    asyncio.run(_drive())
    err = capsys.readouterr().err
    assert "[ChaosInjector][yield]" in err
    assert "depth=0" in err
    # The yield line now carries the PROVEN mutation kind, not a placeholder.
    assert "mutation=" in err


# --------------------------------------------------------------------------- #
# Fixtures (tiny fake repos).
# --------------------------------------------------------------------------- #

def _scaffold(tmp_path):
    repo = tmp_path / "repo"
    pkg = repo / "backend" / "utils"
    _write(str(repo / "backend" / "__init__.py"), "")
    _write(str(pkg / "__init__.py"), "")
    tests = repo / "tests"
    _write(str(tests / "__init__.py"), "")
    return repo, pkg, tests


def _build_typed_zero_site_repo(tmp_path):
    """One mutatable pure-leaf (string return) + one typed object-returner with NO
    viable site (return-None type-guarded out)."""
    repo, pkg, tests = _scaffold(tmp_path)
    mods = {}
    _write(
        str(pkg / "good.py"),
        textwrap.dedent("""\
            from __future__ import annotations


            def good_op(flag):
                if flag:
                    return "yes"
                return "no"
        """),
    )
    _write(
        str(tests / "test_good.py"),
        textwrap.dedent("""\
            from backend.utils.good import good_op


            def test_good_op():
                assert good_op(True) == "yes"
                assert good_op(False) == "no"
        """),
    )
    mods["good"] = str(pkg / "good.py")

    # A strictly-typed PURE leaf with NO comparator/binop/bool/if/assign/constant
    # site (it returns its argument unchanged) and a non-Optional return
    # annotation that bans return-None. This is a genuine zero-site pure leaf ->
    # must be disqualified at SELECTION, not dropped mid-inject.
    _write(
        str(pkg / "frozen.py"),
        textwrap.dedent("""\
            from __future__ import annotations


            def frozen_obj(payload: "dict") -> "dict":
                return payload
        """),
    )
    _write(
        str(tests / "test_frozen.py"),
        textwrap.dedent("""\
            from backend.utils.frozen import frozen_obj


            def test_frozen_obj():
                assert frozen_obj({"k": 1}) == {"k": 1}
        """),
    )
    mods["frozen"] = str(pkg / "frozen.py")
    return str(repo), mods


def _build_isolated_object_repo(tmp_path, *, n=4):
    """``n`` mutually-isolated pure-leaf modules, each an OBJECT/string-returner
    that is mutatable ONLY via the expanded arsenal (if-negation / string), NOT
    via the legacy binop/return-numeric path -- exercising the fix end-to-end."""
    repo, pkg, tests = _scaffold(tmp_path)
    mods = {}
    for i in range(n):
        name = f"svc{i}"
        fn = f"get_{i}"
        _write(
            str(pkg / f"{name}.py"),
            textwrap.dedent(f"""\
                from __future__ import annotations


                def {fn}(flag):
                    if flag:
                        return "on{i}"
                    return "off{i}"
            """),
        )
        _write(
            str(tests / f"test_{name}.py"),
            textwrap.dedent(f"""\
                from backend.utils.{name} import {fn}


                def test_{fn}():
                    assert {fn}(True) == "on{i}"
                    assert {fn}(False) == "off{i}"
            """),
        )
        mods[name] = str(pkg / f"{name}.py")
    return str(repo), mods


def _build_depth1_repo(tmp_path):
    """Only ONE depth-0 pure-leaf available, forcing depth-1 expansion. Two
    depth-1 functions each call a DISTINCT depth-0 helper (disjoint deps)."""
    repo, pkg, tests = _scaffold(tmp_path)

    # depth-0 helpers (pure leaves), each in its OWN module so deps are file-grained.
    _write(
        str(pkg / "leaf_a.py"),
        textwrap.dedent("""\
            from __future__ import annotations


            def leaf_a(x):
                if x:
                    return "A"
                return "a"
        """),
    )
    _write(
        str(pkg / "leaf_b.py"),
        textwrap.dedent("""\
            from __future__ import annotations


            def leaf_b(x):
                if x:
                    return "B"
                return "b"
        """),
    )
    # depth-1 functions: each calls exactly ONE distinct depth-0 helper + has its
    # own if (so it is itself mutatable). They live in distinct modules and import
    # disjoint helpers.
    _write(
        str(pkg / "d1_x.py"),
        textwrap.dedent("""\
            from __future__ import annotations

            from backend.utils.leaf_a import leaf_a


            def d1_x(flag):
                inner = leaf_a(flag)
                if flag:
                    return inner + "_x_on"
                return inner + "_x_off"
        """),
    )
    _write(
        str(pkg / "d1_y.py"),
        textwrap.dedent("""\
            from __future__ import annotations

            from backend.utils.leaf_b import leaf_b


            def d1_y(flag):
                inner = leaf_b(flag)
                if flag:
                    return inner + "_y_on"
                return inner + "_y_off"
        """),
    )
    # Tests for the depth-1 funcs (green pre-injection).
    _write(
        str(tests / "test_d1_x.py"),
        textwrap.dedent("""\
            from backend.utils.d1_x import d1_x


            def test_d1_x():
                assert d1_x(True) == "A_x_on"
                assert d1_x(False) == "a_x_off"
        """),
    )
    _write(
        str(tests / "test_d1_y.py"),
        textwrap.dedent("""\
            from backend.utils.d1_y import d1_y


            def test_d1_y():
                assert d1_y(True) == "B_y_on"
                assert d1_y(False) == "b_y_off"
        """),
    )
    return str(repo)


def _build_depth1_shared_dep_repo(tmp_path):
    """Two depth-1 functions that BOTH call the SAME depth-0 helper (shared dep).
    They must NOT be co-selected."""
    repo, pkg, tests = _scaffold(tmp_path)
    _write(
        str(pkg / "shared.py"),
        textwrap.dedent("""\
            from __future__ import annotations


            def shared(x):
                if x:
                    return "S"
                return "s"
        """),
    )
    _write(
        str(pkg / "uses_a.py"),
        textwrap.dedent("""\
            from __future__ import annotations

            from backend.utils.shared import shared


            def uses_shared_a(flag):
                inner = shared(flag)
                if flag:
                    return inner + "_a_on"
                return inner + "_a_off"
        """),
    )
    _write(
        str(pkg / "uses_b.py"),
        textwrap.dedent("""\
            from __future__ import annotations

            from backend.utils.shared import shared


            def uses_shared_b(flag):
                inner = shared(flag)
                if flag:
                    return inner + "_b_on"
                return inner + "_b_off"
        """),
    )
    _write(
        str(tests / "test_uses_a.py"),
        textwrap.dedent("""\
            from backend.utils.uses_a import uses_shared_a


            def test_uses_shared_a():
                assert uses_shared_a(True) == "S_a_on"
                assert uses_shared_a(False) == "s_a_off"
        """),
    )
    _write(
        str(tests / "test_uses_b.py"),
        textwrap.dedent("""\
            from backend.utils.uses_b import uses_shared_b


            def test_uses_shared_b():
                assert uses_shared_b(True) == "S_b_on"
                assert uses_shared_b(False) == "s_b_off"
        """),
    )
    return str(repo)
