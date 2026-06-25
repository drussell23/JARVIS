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
