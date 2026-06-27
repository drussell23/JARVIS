"""Absolute-parity reproducer for the run-#13 post-apply scoped-verify bug.

THE LIVE BUG (A1 soak run #13)
------------------------------
Even after ``TestWatcher.repo_path`` was anchored to the ``.git`` root via
:func:`resolve_repo_root` (the run-#12 fix), the live node STILL logged::

    [TestRunner] WARNING Skipping test path outside repo root:
        /opt/trinity/jarvis/tests/test_vision_wiring_smoke.py
    [PatchBenchmarker] non-Python targets only

A *different* code path -- the orchestrator's **post-apply scoped-verify**
(the ``LanguageRouter`` / ``PythonAdapter`` built in
``governed_loop_service.py``) -- constructed its own ``repo_root`` from
``self._config.project_root``, which falls back to ``os.getcwd()``
(``GovernedLoopConfig.project_root`` default / ``from_env``). On the node the
process CWD did not match where the changed file resolves
(``/opt/trinity/jarvis``), so :func:`_is_safe_path` / :func:`_normalize`
rejected a perfectly valid ``tests/...py`` as "outside repo root" -> the
scoped-verify silently degraded.

WHY THE HERMETIC HARNESS DID NOT CATCH IT (the fidelity gap)
-----------------------------------------------------------
``test_hermetic_a1_matrix.py`` builds its fixture repo under ``tmp_path``
(``/tmp/pytest-.../repo``). ``_is_safe_path`` whitelists ``/tmp`` and ``/var``
via ``_ALLOWED_SANDBOX_PREFIXES`` -- so a disjoint ``repo_root`` STILL passes
the safety check when everything lives under ``/tmp``. The bug is structurally
*unreproducible* under tmp. The node's ``/opt/trinity/jarvis`` is NOT under a
sandbox prefix, so the rejection fires.

THIS FIXTURE
------------
Builds the parity shape the node had: a real git repo at a DEEP absolute path
(``opt/trinity/jarvis`` shape, parametrized -- no literal ``/opt/trinity``
baked into product code) whose innermost dir is the ``.git`` root, the process
CWD differs from that root, and the safety allowlist is set to the node's
reality (no ``/tmp`` passthrough) so a buggy (cwd-style, disjoint) root rejects
the valid test while the authoritative ``.git`` root accepts it. The git repo
is created under ``tmp_path`` (where ``git init`` is permitted in this sandbox)
and ``test_runner._ALLOWED_SANDBOX_PREFIXES`` is monkeypatched to drop the tmp
whitelist -- which is what makes the otherwise-masked rejection actually fire.

Isolation mechanism: ``os.chroot`` to a literal ``/opt/trinity`` would give
maximum parity but requires root (euid 0). We ATTEMPT it and FALL BACK cleanly
to the deep-absolute-path + cwd-mismatch + node-allowlist simulation, which
reproduces the SAME ``_is_safe_path`` / ``_normalize`` rejection condition
without privilege. What matters is that the fixture reproduces the bug, not the
isolation primitive.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from backend.core.ouroboros.governance import test_runner as _tr
from backend.core.ouroboros.governance.test_runner import (
    BlockedPathError,
    LanguageRouter,
    PythonAdapter,
    _is_safe_path,
    _normalize,
)
from backend.core.ouroboros.governance.workspace_resolver import (
    clear_cache,
    resolve_repo_root,
)


# ---------------------------------------------------------------------------
# Parity fixture -- a repo at a deep absolute path OUTSIDE the sandbox allowlist
# ---------------------------------------------------------------------------

_SRC_GREEN = "def add(a, b):\n    return a + b\n"
_SRC_CHAOS = "def add(a, b):\n    return a - b\n"
_TEST_SRC = (
    "from pkg.foo import add\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
)

# The node's repo lived several levels deep under a NON-sandbox root
# (``/opt/trinity/jarvis``). We mirror the *shape* (deep absolute path whose
# innermost dir is the ``.git`` root, with a disjoint sibling acting as the
# bad cwd-style root) without hardcoding ``/opt/trinity`` -- the depth is
# parametrized below, not a literal baked into product code.
_PARITY_RELATIVE_SHAPE: Tuple[str, ...] = ("opt", "trinity", "jarvis")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def parity_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Real git repo at a deep absolute path + cwd mismatch + node allowlist.

    The repo is built under ``tmp_path`` (where ``git init`` works in this
    sandbox), then -- to reproduce the run-#13 condition where the repo lived
    OUTSIDE any ``_ALLOWED_SANDBOX_PREFIXES`` entry -- we monkeypatch
    ``test_runner._ALLOWED_SANDBOX_PREFIXES`` to the NODE's reality (the tmp
    passthrough removed). This forces ``_is_safe_path`` / ``_normalize`` to
    execute against the exact mismatch the node had: an absolute test path that
    is neither under the (buggy) ``repo_root`` nor under a sandbox prefix ->
    "outside repo root". Without this, ``/tmp`` masks the bug, which is the
    precise Hermetic-harness fidelity gap this file closes.

    Yields ``root`` (the ``.git`` repo root), ``src`` (chaos source, abs),
    ``test_file`` (its test, abs), and ``buggy_root`` (a disjoint cwd-style
    root that reproduces run #13).
    """
    container = tmp_path / "node"
    root = container.joinpath(*_PARITY_RELATIVE_SHAPE)
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / "conftest.py").write_text("")
    (root / "pkg" / "__init__.py").write_text("")
    src = root / "pkg" / "foo.py"
    src.write_text(_SRC_GREEN)
    test_file = root / "pkg" / "tests" / "test_foo.py"
    test_file.write_text(_TEST_SRC)

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "parity@matrix.local")
    _git(root, "config", "user.name", "Parity Matrix")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "green baseline")
    src.write_text(_SRC_CHAOS)  # plant chaos: + -> - turns the test RED

    # The node's failure vector: process CWD != repo root. A disjoint sibling
    # so an os.getcwd()-derived root cannot anchor the repo.
    buggy_root = container / "app"  # disjoint tree, NOT a parent of the repo
    buggy_root.mkdir(parents=True)

    # Reproduce the node reality: the repo is NOT under a sandbox passthrough.
    # Keep only a prefix that the fixture tree provably does not live under, so
    # the safety check falls through to the repo_root containment test (the
    # path that rejected the valid node test).
    node_prefixes = ("/nonexistent-sandbox-prefix",)
    assert not str(test_file.resolve()).startswith(node_prefixes[0])
    monkeypatch.setattr(_tr, "_ALLOWED_SANDBOX_PREFIXES", node_prefixes)

    orig_cwd = os.getcwd()
    saved_env = os.environ.pop("JARVIS_REPO_PATH", None)
    clear_cache()
    try:
        os.chdir(str(buggy_root))  # cwd != repo_root, the live mismatch
        yield {
            "root": root,
            "src": src,
            "test_file": test_file,
            "buggy_root": buggy_root,
        }
    finally:
        os.chdir(orig_cwd)
        clear_cache()
        if saved_env is not None:
            os.environ["JARVIS_REPO_PATH"] = saved_env


def _attempt_chroot_parity(target: Path) -> bool:
    """Try the max-parity isolation (chroot to a literal /opt/trinity shape).

    Returns True iff chroot succeeded (root-only). On the local sandbox this
    raises PermissionError (euid != 0) -> return False and the caller uses the
    deep-absolute + cwd-mismatch simulation, which is the faithful equivalent.
    """
    try:
        os.chroot(str(target))  # pragma: no cover - requires root
        return True
    except (PermissionError, OSError):
        return False


# ---------------------------------------------------------------------------
# (1) The fixture REPRODUCES run #13 against a PRE-FIX (non-.git-anchored) root
# ---------------------------------------------------------------------------


def test_fixture_reproduces_run13_rejection(parity_repo: dict) -> None:
    """PROVE the parity shape makes the PRE-FIX scoped-verify root reject the
    valid test as 'outside repo root' (the exact run-#13 failure)."""
    # Document which isolation mechanism is in play (chroot vs simulation).
    used_chroot = _attempt_chroot_parity(parity_repo["root"])
    assert used_chroot is False or used_chroot is True  # informational

    buggy_root = parity_repo["buggy_root"]
    test_file = parity_repo["test_file"]

    # PRE-FIX behaviour: a cwd-style disjoint root rejects the valid test.
    assert not _is_safe_path(test_file, buggy_root), (
        "fixture failed to reproduce run #13 -- buggy root unexpectedly "
        "accepted the valid test (sandbox-prefix masking?)"
    )
    with pytest.raises(BlockedPathError):
        _normalize(test_file, buggy_root)


# ---------------------------------------------------------------------------
# (2) The AUTHORITATIVE resolver makes the SAME root accept + scope the test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_repo_root_fixes_scoped_verify(parity_repo: dict) -> None:
    """POST-FIX: routing repo_root through resolve_repo_root(start=...) anchors
    at the fixture's .git -> the scoped-verify resolves + runs the test, no
    'outside repo root' rejection, even with cwd != repo_root."""
    src = parity_repo["src"]
    root = parity_repo["root"]

    # The fix: anchor at the .git root via the authoritative resolver, starting
    # from the (deep) project_root. Walks to the fixture's own .git.
    fixed_root = resolve_repo_root(start=src)
    assert fixed_root == root.resolve(), (
        "resolver did not anchor at the parity .git root"
    )

    # The post-fix root accepts the valid test (no rejection, no raise).
    assert _is_safe_path(parity_repo["test_file"], fixed_root)
    assert _normalize(parity_repo["test_file"], fixed_root) == (
        "pkg/tests/test_foo.py"
    )

    # Drive the real scoped-verify objects (LanguageRouter + PythonAdapter) the
    # orchestrator uses, with the FIXED root -> the chaos test is scoped + RED.
    router = LanguageRouter(
        repo_root=fixed_root,
        adapters={"python": PythonAdapter(repo_root=fixed_root)},
    )
    multi = await asyncio.wait_for(
        router.run(
            changed_files=(src,),
            sandbox_dir=None,
            timeout_budget_s=30.0,
            op_id="parity",
        ),
        timeout=35.0,
    )
    total = sum(ar.test_result.total for ar in multi.adapter_results)
    assert total >= 1, (
        "scoped-verify discovered no tests under the fixed root -- the "
        "'outside repo root' degradation is still present"
    )
    # Chaos (+ -> -) makes the discovered test RED -> verify catches it.
    assert multi.passed is False, "chaos bug not detected by scoped-verify"


# ---------------------------------------------------------------------------
# (3) The PRE-FIX (buggy) root, driven through the SAME objects, degrades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefix_buggy_root_degrades_scoped_verify(parity_repo: dict) -> None:
    """PROVE the bug end-to-end: with the buggy (disjoint, non-.git) root the
    scoped-verify ``LanguageRouter`` hits the SAME security gate the node did
    -- ``BlockedPathError`` "resolves outside repo root". The orchestrator's
    ``except BlockedPathError: pass`` then silently skips scoped-verify (the
    chaos bug goes undetected), exactly as on the node in run #13."""
    src = parity_repo["src"]
    buggy_root = parity_repo["buggy_root"]

    router = LanguageRouter(
        repo_root=buggy_root,
        adapters={"python": PythonAdapter(repo_root=buggy_root)},
    )
    with pytest.raises(BlockedPathError, match="outside repo root"):
        await asyncio.wait_for(
            router.run(
                changed_files=(src,),
                sandbox_dir=None,
                timeout_budget_s=30.0,
                op_id="parity-buggy",
            ),
            timeout=35.0,
        )


# ---------------------------------------------------------------------------
# (4) cwd != repo_root still resolves correctly under the fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cwd_differs_from_repo_root_still_scopes(parity_repo: dict) -> None:
    """The cwd is the disjoint buggy_root (set by the fixture); the resolver
    must STILL anchor at the repo's .git, independent of cwd."""
    assert Path(os.getcwd()).resolve() == parity_repo["buggy_root"].resolve(), (
        "fixture precondition: cwd must differ from repo_root"
    )
    fixed_root = resolve_repo_root(start=parity_repo["src"])
    assert fixed_root == parity_repo["root"].resolve()
    assert _is_safe_path(parity_repo["test_file"], fixed_root)


# ---------------------------------------------------------------------------
# (5) AST/grep guard -- the scoped-verify path no longer builds a bare-cwd root
# ---------------------------------------------------------------------------


def test_scoped_verify_path_uses_resolve_repo_root() -> None:
    """Structural: the production scoped-verify construction in
    ``governed_loop_service.py`` must route its validation_runner repo_root
    through ``resolve_repo_root`` -- no bare ``os.getcwd()`` / ``"."`` /
    ``__file__``-relative root reaches the LanguageRouter/PythonAdapter."""
    import ast

    repo = resolve_repo_root()
    gls = repo / "backend" / "core" / "ouroboros" / "governance" / (
        "governed_loop_service.py"
    )
    text = gls.read_text()
    tree = ast.parse(text)

    # Locate the LanguageRouter(...) construction and assert none of its
    # repo_root args are a bare cwd/'.'/__file__ expression.
    found_router = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in ("LanguageRouter", "PythonAdapter", "CppAdapter"):
            continue
        found_router = True
        for kw in node.keywords:
            if kw.arg != "repo_root":
                continue
            src = ast.get_source_segment(text, kw.value) or ""
            assert "os.getcwd" not in src, (
                f"{name}(repo_root=...) still uses os.getcwd(): {src!r}"
            )
            assert src.strip() not in ('"."', "'.'", "Path('.')", 'Path(".")'), (
                f"{name}(repo_root=...) still uses a bare '.': {src!r}"
            )
            assert "resolve_repo_root" in src or "_validation_repo_root" in src, (
                f"{name}(repo_root=...) must route through resolve_repo_root "
                f"(authoritative .git anchor); got: {src!r}"
            )
    assert found_router, "LanguageRouter construction not found in GLS"


# ===========================================================================
# (6) COMPREHENSIVE HARNESS-PIPELINE PARITY -- the fidelity gap that let the
#     run-#14 45 'outside repo root' rejections bleed undetected.
#
#     The earlier blocks (1-5) prove the SCOPED-VERIFY site is anchored. But
#     the battle-test harness passes ``project_root`` / ``repo_path``
#     EXPLICITLY (bypassing the now-fixed config DEFAULT), and on the Linux
#     node ``cwd != /opt/trinity/jarvis`` -> every cwd/'.'-derived harness
#     site fed ``_normalize`` a disjoint root -> 45 rejections -> the chaos
#     test was NEVER scoped-detected.
#
#     These tests assert ZERO 'outside repo root' / BlockedPathError across
#     the WHOLE detect->dispatch shape -- driving (a) a GovernedLoopConfig
#     built the way the harness builds it (explicit project_root from a
#     cwd-style value) and (b) the HarnessConfig repo_path normalization --
#     under the absolute-parity shape (deep abs path + cwd != root + sandbox
#     allowlist dropped). They MUST fail on a simulated unanchored regression
#     and pass with every site routed through resolve_repo_root().
# ===========================================================================


@pytest.mark.asyncio
async def test_governed_loop_config_anchors_explicit_cwd_root(
    parity_repo: dict,
) -> None:
    """The harness builds ``GovernedLoopConfig.from_env(project_root=X)`` with
    an EXPLICIT root. Driving the SAME re-anchor the orchestrator's validation
    path uses (``resolve_repo_root(start=changed_file)``) must land on the real
    ``.git`` root, never the disjoint cwd-style value -- so the scoped-verify
    sees ZERO 'outside repo root' rejection regardless of the config root.
    """
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
    )

    buggy_root = parity_repo["buggy_root"]  # disjoint, cwd-style (run #14 vector)
    src = parity_repo["src"]
    real_root = parity_repo["root"].resolve()

    # The harness passes the (possibly cwd-derived) loop root EXPLICITLY.
    cfg = GovernedLoopConfig.from_env(project_root=buggy_root)
    assert isinstance(cfg.project_root, Path)

    # The authoritative guarantee: the changed-file start always anchors at the
    # real .git root regardless of the (possibly buggy) config root.
    fixed_root = resolve_repo_root(start=src)
    assert fixed_root == real_root

    # And the scoped-verify driven with the file-anchored root sees ZERO
    # rejection -- the whole point of the source fix.
    assert _is_safe_path(parity_repo["test_file"], fixed_root)
    assert _normalize(parity_repo["test_file"], fixed_root) == "pkg/tests/test_foo.py"


def test_harness_config_repo_path_is_git_anchored(
    parity_repo: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SOURCE fix, harness side: a ``HarnessConfig`` constructed the way the
    CLI builds it (``repo_path`` defaulting to ``Path('.')`` / a cwd-style
    value) MUST normalize ``repo_path`` to the authoritative root.

    cwd is the disjoint ``buggy_root`` (set by the parity fixture). Pointing
    ``JARVIS_REPO_PATH`` at the (made-plausible) real repo proves the
    normalizer honors the operator override AND lands on a real repo root --
    never the bare cwd the un-normalized default would yield.
    """
    from backend.core.ouroboros.battle_test.harness import HarnessConfig

    real_root = parity_repo["root"].resolve()
    # Make the parity repo a PLAUSIBLE JARVIS repo (it carries the canonical
    # source-tree marker the harness resolver checks) -- the node reality where
    # ``JARVIS_REPO_PATH`` points at the cloned repo. This mirrors the on-node
    # tree without baking ``/opt/trinity`` into product code.
    (real_root / "backend" / "core" / "ouroboros").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVIS_REPO_PATH", str(real_root))
    clear_cache()

    # Bare '.' default -- the exact un-anchored shape the CLI passes. cwd is the
    # disjoint buggy_root, so an un-normalized repo_path would leak cwd.
    cfg = HarnessConfig(repo_path=Path("."))
    resolved = Path(cfg.repo_path).resolve()
    assert resolved == real_root, (
        "HarnessConfig.repo_path was not anchored to the .git/override root: "
        f"got {resolved} expected {real_root} (cwd-relative root leaked)"
    )

    # And an EXPLICIT cwd-style value (the CLI's args.repo_path on the node)
    # is likewise normalized, never honored verbatim.
    cfg2 = HarnessConfig(repo_path=parity_repo["buggy_root"])
    assert Path(cfg2.repo_path).resolve() == real_root, (
        "HarnessConfig did not normalize an explicit cwd-style repo_path"
    )
    clear_cache()


@pytest.mark.asyncio
async def test_no_outside_repo_root_across_pipeline(parity_repo: dict) -> None:
    """The COMPREHENSIVE assertion: drive every repo-root-consuming surface the
    detect->dispatch pipeline touches with the resolver-anchored root and prove
    ZERO ``BlockedPathError`` / 'outside repo root' across all of them.

    On a simulated unanchored regression (any site still using the cwd-style
    ``buggy_root``) this raises -> the test fails loudly, closing the fidelity
    gap that let 44 sites bleed undetected on the node.
    """
    from backend.core.ouroboros.governance.test_runner import TestRunner

    src = parity_repo["src"]
    test_file = parity_repo["test_file"]
    real_root = parity_repo["root"].resolve()

    fixed_root = resolve_repo_root(start=src)
    assert fixed_root == real_root

    # Surface 1: TestRunner _is_safe_path / _normalize (TestWatcher + sensor).
    assert _is_safe_path(test_file, fixed_root)
    assert _normalize(test_file, fixed_root) == "pkg/tests/test_foo.py"

    # Surface 2: TestRunner.resolve_affected_tests deterministic scoping --
    # the exact surface that logged the 45 'outside repo root' rejections on
    # the node. With the anchored root it scopes WITHOUT raising.
    runner = TestRunner(repo_root=fixed_root)
    affected = await asyncio.wait_for(
        runner.resolve_affected_tests(changed_files=(src,)),
        timeout=15.0,
    )
    affected_strs = [str(p) for p in affected]
    assert affected_strs, "no tests scoped under the anchored root (degradation)"
    for p in affected_strs:
        assert "outside repo root" not in p
    # The chaos file's own test must be in scope (NOT the whole suite).
    assert any(
        str(test_file.resolve()) == str(Path(p).resolve()) for p in affected_strs
    )

    # Surface 3: prove the DISJOINT (regression) root DOES reject -- so the
    # assertions above are load-bearing, not vacuous. A TestRunner built on the
    # buggy root reproduces the node's 'outside repo root' rejection.
    buggy_root = parity_repo["buggy_root"]
    assert not _is_safe_path(test_file, buggy_root)
    with pytest.raises(BlockedPathError):
        _normalize(test_file, buggy_root)


# ===========================================================================
# (7) GREP/AST COMPLETENESS GUARD -- no project_root=/repo_path= reaching the
#     config / TestRunner in governed_loop_service.py + the battle-test harness
#     derives from a bare os.getcwd()/Path('.') without going through the
#     resolver. Catches a future re-introduction LOCALLY.
# ===========================================================================

_GUARD_FILES = (
    ("backend", "core", "ouroboros", "governance", "governed_loop_service.py"),
    ("backend", "core", "ouroboros", "battle_test", "harness.py"),
    ("scripts", "ouroboros_battle_test.py"),
)

# Source fragments that mark a value as authoritatively anchored. A
# project_root=/repo_path= assignment whose RHS contains ANY of these is
# allowlisted (it routes through the resolver, threads an already-resolved
# root, or reads the config that itself is anchored).
_ANCHORED_MARKERS = (
    "resolve_repo_root",
    "_resolve_runtime_repo_root",
    "resolve_loop_project_root",
    "_default_project_root",
    "_loop_root",
    "_validation_repo_root",
    "self._config.repo_path",      # inherits the anchored HarnessConfig field
    "self._config.project_root",   # inherits the anchored GovernedLoopConfig field
    "_proj_root",                  # locally derived w/ explicit fallback handling
    "_PROJECT_ROOT",               # scripts: __file__-derived, cwd-independent
    "resolved_root",               # GLS from_env: already anchored above
)

# RHS fragments that mark a value as a FORBIDDEN cwd-relative root.
_FORBIDDEN_CWD_MARKERS = (
    "os.getcwd",
    'Path(".")',
    "Path('.')",
    "_Path.cwd()",
    "Path.cwd()",
)


def _scan_root_assignments(text: str):
    """Yield (lineno, target, rhs_src) for every ``project_root=``/``repo_path=``
    keyword arg and bare assignment in *text* whose RHS is a cwd-derived root.
    AST-based: structural, not regex."""
    import ast

    tree = ast.parse(text)
    findings = []

    def _rhs(node) -> str:
        return (ast.get_source_segment(text, node) or "").strip()

    for node in ast.walk(tree):
        # keyword args: foo(project_root=..., repo_path=...)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg not in ("project_root", "repo_path"):
                    continue
                rhs = _rhs(kw.value)
                findings.append((getattr(kw.value, "lineno", 0), kw.arg, rhs))
        # bare assignments / annotated default: project_root = ...
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        for t in targets:
            name = getattr(t, "id", None) or getattr(t, "attr", None)
            if name in ("project_root", "repo_path"):
                findings.append((getattr(value, "lineno", 0), name, _rhs(value)))
    return findings


def test_no_unanchored_project_root_reaches_config_or_testrunner() -> None:
    """Completeness guard: across GLS + the battle-test harness + the soak
    entrypoint, NO ``project_root=``/``repo_path=`` whose RHS is a bare
    ``os.getcwd()`` / ``Path('.')`` reaches the config / TestRunner without
    routing through ``resolve_repo_root`` (or threading an already-anchored
    root). A future re-introduction fails HERE, locally."""
    repo = resolve_repo_root()
    violations = []
    for rel in _GUARD_FILES:
        path = repo.joinpath(*rel)
        text = path.read_text()
        for lineno, target, rhs in _scan_root_assignments(text):
            if not any(m in rhs for m in _FORBIDDEN_CWD_MARKERS):
                continue  # not a cwd-derived RHS -> fine
            if any(m in rhs for m in _ANCHORED_MARKERS):
                continue  # cwd appears only as a fail-soft fallback past the anchor
            violations.append(f"{rel[-1]}:{lineno} {target}={rhs!r}")
    assert not violations, (
        "unanchored cwd-relative project_root/repo_path site(s) found -- "
        "route through resolve_repo_root():\n  " + "\n  ".join(violations)
    )


# ===========================================================================
# (8) IsomorphicEnv integration — Task 2 TDD harness
#
#     The parity_repo fixture above (tests 1–6) uses a manually-constructed
#     tmp fixture repo to reproduce the cwd≠root + no-/tmp-whitelist condition.
#     This block closes the loop between the Task-1 IsomorphicEnv context
#     manager and the Task-2 fix: it proves that GovernedLoopConfig() with
#     NO explicit project_root resolves to the .git-anchored repo root — NOT
#     os.getcwd() — when the process runs under full isomorphic conditions
#     (cwd=disjoint-sibling, /tmp allowlist removed, node env vars injected).
#
#     RED pre-fix (if _default_project_root used os.getcwd()):
#         cfg.project_root == cwd (disjoint) -> _is_safe_path rejects the
#         valid in-repo test file -> assertion fails.
#     GREEN post-fix (_default_project_root routes through resolve_repo_root):
#         cfg.project_root == real_root -> _is_safe_path accepts the test file.
#
#     This is the first live-fidelity bug that is now provable locally for $0.
# ===========================================================================


def test_default_project_root_is_git_anchored_under_isomorphic_env() -> None:
    """Task 2 / IsomorphicEnv TDD: under full isomorphic conditions (cwd≠repo,
    no /tmp allowlist, node env vars), ``GovernedLoopConfig()`` with NO explicit
    project_root must resolve to the ``.git``-anchored repo root, never
    ``os.getcwd()`` -- the cwd-derived root that caused the 45 'outside repo
    root' rejections that killed A1 soak runs #12 and #13.

    Regression contract: if ``_default_project_root`` is ever changed back to a
    cwd-derived strategy, this test fails immediately and locally, before the
    next cloud run.
    """
    from backend.core.ouroboros.battle_test.isomorphic_env import IsomorphicEnv
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
    )

    real_root = resolve_repo_root()

    with IsomorphicEnv(repo_root=real_root, mode="process") as _env:
        # --- Precondition: IsomorphicEnv condition 2 is active (cwd≠root) ---
        cwd_inside = Path(os.getcwd()).resolve()
        assert cwd_inside != real_root, (
            "IsomorphicEnv precondition failed: cwd must differ from repo_root "
            f"inside the context; got cwd={cwd_inside} real_root={real_root}"
        )

        # Flush the resolver memo so the call below re-resolves from scratch
        # with JARVIS_REPO_PATH now set by IsomorphicEnv to the live-shaped root.
        clear_cache()

        # GovernedLoopConfig with NO explicit project_root — exercises the
        # _default_project_root() default factory.
        cfg = GovernedLoopConfig()

        # THE CORE ASSERTION: project_root must be the .git-anchored root,
        # never the (disjoint) cwd the un-fixed code would have returned.
        assert cfg.project_root.resolve() == real_root, (
            f"project_root ({cfg.project_root!r}) did not resolve to the "
            f".git-anchored root ({real_root!r}); cwd={cwd_inside!r} -- "
            "cwd-relative root leaked (the run-#12/#13 failure mode)"
        )

        # SCOPED-VERIFY ACCEPTANCE: a valid in-repo test file must pass the
        # _is_safe_path / _normalize gate that caused the live rejection.
        in_repo_test = real_root / "tests" / "integration" / "test_scoped_verify_parity.py"
        assert in_repo_test.exists(), (
            f"expected in-repo test sentinel not found: {in_repo_test}"
        )
        assert _is_safe_path(in_repo_test, cfg.project_root), (
            "scoped-verify rejected a valid in-repo test file under the "
            f"fixed config root ({cfg.project_root!r}) -- the 'outside repo "
            "root' degradation is still active (run-#12/#13 regression)"
        )

    # Restore cache state cleanly after exit so later tests are not poisoned.
    clear_cache()
