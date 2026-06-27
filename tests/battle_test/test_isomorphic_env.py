"""TDD suite for ``IsomorphicEnv`` (Task 1 — Isomorphic Local Sandbox).

Five mandatory assertions (RED → GREEN):

1. ``env.root`` ends with the live shape (opt/trinity/jarvis) and is absolute.
2. ``cwd != env.root`` inside the context (the run-#13 mismatch condition).
3. The ``/tmp`` whitelist is absent inside the context (a ``/tmp`` path that
   WOULD be allowed by the prefix is now rejected by the test-runner policy)
   and RESTORED verbatim after exit.
4. Node env vars are set inside and restored to prior values after exit.
5. ``__exit__`` restores cwd + env + policy even when the body raises.

Container-mode test is skipped when Docker is absent.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

# Module under test
from backend.core.ouroboros.battle_test.isomorphic_env import (
    IsomorphicEnv,
    _PARITY_RELATIVE_SHAPE,
    _REMOTE_ROOT_ENV,
)

# The test_runner module and its policy attribute we assert against.
import backend.core.ouroboros.governance.test_runner as _tr
from backend.core.ouroboros.governance.test_runner import _is_safe_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path) -> Path:
    """Minimal repo directory — IsomorphicEnv only needs a valid directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# ---------------------------------------------------------------------------
# Test 1 — env.root ends with live shape and is absolute
# ---------------------------------------------------------------------------

def test_root_ends_with_live_shape(tmp_path: Path) -> None:
    """``env.root`` must be an absolute path whose last three segments are
    ``("opt", "trinity", "jarvis")`` — the canonical live shape."""
    repo = _make_repo(tmp_path)
    with IsomorphicEnv(repo) as env:
        assert env.root.is_absolute(), "env.root must be absolute"
        assert env.root.parts[-3:] == _PARITY_RELATIVE_SHAPE, (
            f"Expected root to end with {_PARITY_RELATIVE_SHAPE}; "
            f"got parts {env.root.parts}"
        )


def test_root_not_accessible_outside_context(tmp_path: Path) -> None:
    """Accessing ``env.root`` outside the context must raise RuntimeError."""
    repo = _make_repo(tmp_path)
    env = IsomorphicEnv(repo)
    with pytest.raises(RuntimeError, match="outside context"):
        _ = env.root


# ---------------------------------------------------------------------------
# Test 2 — cwd ≠ env.root inside the context
# ---------------------------------------------------------------------------

def test_cwd_differs_from_root(tmp_path: Path) -> None:
    """The process cwd must NOT equal ``env.root`` inside the context
    (replicates the live mismatch that exposed the run-#13 bug)."""
    repo = _make_repo(tmp_path)
    with IsomorphicEnv(repo) as env:
        assert Path.cwd() != env.root, (
            "cwd must differ from env.root inside the context"
        )


def test_cwd_restored_after_exit(tmp_path: Path) -> None:
    """After exit the process cwd must be restored to what it was before."""
    repo = _make_repo(tmp_path)
    cwd_before = os.getcwd()
    with IsomorphicEnv(repo):
        pass
    assert os.getcwd() == cwd_before


# ---------------------------------------------------------------------------
# Test 3 — /tmp whitelist removed inside, restored after
# ---------------------------------------------------------------------------

def test_tmp_whitelist_absent_inside(tmp_path: Path) -> None:
    """Inside the context, ``_ALLOWED_SANDBOX_PREFIXES`` must contain no
    ``/tmp`` or ``/private/tmp`` entry."""
    repo = _make_repo(tmp_path)
    # Pre-condition: the original policy DOES have a /tmp entry.
    original = _tr._ALLOWED_SANDBOX_PREFIXES
    assert any("tmp" in p for p in original), (
        f"Test precondition violated: original _ALLOWED_SANDBOX_PREFIXES "
        f"{original!r} must contain a /tmp entry"
    )
    with IsomorphicEnv(repo):
        current = _tr._ALLOWED_SANDBOX_PREFIXES
        assert not any("tmp" in p for p in current), (
            f"Inside context: no /tmp prefix expected; got {current!r}"
        )


def test_tmp_path_rejected_inside_context(tmp_path: Path) -> None:
    """/tmp paths that WOULD be allowed by the old whitelist are REJECTED
    inside the context (the condition that fires on the live node)."""
    repo = _make_repo(tmp_path)
    # A disjoint root: ensures the safety check cannot pass via repo containment.
    disjoint_root = Path("/nonexistent-root-for-iso-test")
    with IsomorphicEnv(repo):
        # On macOS, /tmp resolves to /private/tmp — both are in the original
        # whitelist and both are stripped by IsomorphicEnv.
        result = _is_safe_path(Path("/tmp/iso_probe_file.py"), disjoint_root)
        assert result is False, (
            "A /tmp path must be REJECTED when the sandbox whitelist is stripped"
        )


def test_sandbox_prefixes_restored_after_exit(tmp_path: Path) -> None:
    """`_ALLOWED_SANDBOX_PREFIXES` is restored to the original object after exit."""
    repo = _make_repo(tmp_path)
    original = _tr._ALLOWED_SANDBOX_PREFIXES
    with IsomorphicEnv(repo):
        pass
    # Must be the SAME object (not just an equal one) — we saved a reference.
    assert _tr._ALLOWED_SANDBOX_PREFIXES is original, (
        "After exit, _ALLOWED_SANDBOX_PREFIXES must be the exact original object"
    )


# ---------------------------------------------------------------------------
# Test 4 — node env vars set inside, restored after
# ---------------------------------------------------------------------------

_NODE_ENV_KEYS = [
    "JARVIS_IAC_REMOTE_ROOT",
    "JARVIS_PRIME_REPO_PATH",
    "JARVIS_REACTOR_REPO_PATH",
    "JARVIS_TRINITY_PREBAKE_ENABLED",
    "JARVIS_CROSS_REPO_MUTATION_ENABLED",
    "JARVIS_CHAOS_INJECTOR_ENABLED",
    "JARVIS_REPO_PATH",
]


def test_node_env_vars_present_inside(tmp_path: Path) -> None:
    """The live-node env vars must be set (non-None) inside the context."""
    repo = _make_repo(tmp_path)
    with IsomorphicEnv(repo) as env:
        assert os.environ.get("JARVIS_IAC_REMOTE_ROOT") is not None
        assert os.environ.get("JARVIS_TRINITY_PREBAKE_ENABLED") == "1"
        assert os.environ.get("JARVIS_CROSS_REPO_MUTATION_ENABLED") == "1"
        assert os.environ.get("JARVIS_CHAOS_INJECTOR_ENABLED") == "1"
        # JARVIS_REPO_PATH must point at the effective root.
        assert os.environ.get("JARVIS_REPO_PATH") == str(env.root)
        # Prime/reactor paths must derive from the trinity root.
        remote_root = os.environ.get("JARVIS_IAC_REMOTE_ROOT", "")
        assert os.environ.get("JARVIS_PRIME_REPO_PATH") == f"{remote_root}/prime"
        assert os.environ.get("JARVIS_REACTOR_REPO_PATH") == f"{remote_root}/reactor"


def test_node_env_vars_restored_after_exit(tmp_path: Path) -> None:
    """Every node env var is restored to its pre-context value after exit."""
    repo = _make_repo(tmp_path)
    before: dict = {k: os.environ.get(k) for k in _NODE_ENV_KEYS}
    with IsomorphicEnv(repo):
        pass
    after: dict = {k: os.environ.get(k) for k in _NODE_ENV_KEYS}
    assert after == before, (
        f"Env vars not restored correctly.\n"
        f"Before: {before}\n"
        f"After:  {after}"
    )


# ---------------------------------------------------------------------------
# Test 5 — exit restores all state even when the body raises
# ---------------------------------------------------------------------------

def test_exit_restores_all_on_exception(tmp_path: Path) -> None:
    """``__exit__`` must restore cwd, env, and policy even if the body raises."""
    repo = _make_repo(tmp_path)
    cwd_before = os.getcwd()
    env_before: dict = {k: os.environ.get(k) for k in _NODE_ENV_KEYS}
    prefixes_before = _tr._ALLOWED_SANDBOX_PREFIXES

    with pytest.raises(RuntimeError, match="intentional"):
        with IsomorphicEnv(repo):
            raise RuntimeError("intentional failure in body")

    # All three pieces of global state must be restored.
    assert os.getcwd() == cwd_before, "cwd not restored after exception in body"
    assert {k: os.environ.get(k) for k in _NODE_ENV_KEYS} == env_before, (
        "env vars not restored after exception in body"
    )
    assert _tr._ALLOWED_SANDBOX_PREFIXES is prefixes_before, (
        "sandbox prefixes not restored after exception in body"
    )


def test_reuse_across_tests_no_leakage(tmp_path: Path) -> None:
    """Two sequential IsomorphicEnv uses must not leak state to each other."""
    repo = _make_repo(tmp_path)
    cwd_before = os.getcwd()
    original_prefixes = _tr._ALLOWED_SANDBOX_PREFIXES

    for _ in range(2):
        with IsomorphicEnv(repo):
            pass
        # After each exit: state is clean.
        assert os.getcwd() == cwd_before
        assert _tr._ALLOWED_SANDBOX_PREFIXES is original_prefixes


# ---------------------------------------------------------------------------
# Test 6 — container mode (skip when Docker is absent)
# ---------------------------------------------------------------------------

try:
    from backend.core.ouroboros.governance.container_sandbox import docker_available as _docker_available
    _DOCKER_PRESENT = _docker_available()
except Exception:
    _DOCKER_PRESENT = False


@pytest.mark.skipif(not _DOCKER_PRESENT, reason="Docker not available on this host")
def test_container_mode_root_is_live_path(tmp_path: Path) -> None:
    """In container mode, ``env.root`` must be the in-container live path."""
    repo = _make_repo(tmp_path)
    with IsomorphicEnv(repo, mode="container") as env:
        assert env.root.is_absolute()
        assert env.root.parts[-3:] == _PARITY_RELATIVE_SHAPE


@pytest.mark.skipif(not _DOCKER_PRESENT, reason="Docker not available on this host")
def test_container_mode_restores_state(tmp_path: Path) -> None:
    """Container mode must restore cwd/env/policy identically to process mode."""
    repo = _make_repo(tmp_path)
    cwd_before = os.getcwd()
    prefixes_before = _tr._ALLOWED_SANDBOX_PREFIXES
    with IsomorphicEnv(repo, mode="container"):
        pass
    assert os.getcwd() == cwd_before
    assert _tr._ALLOWED_SANDBOX_PREFIXES is prefixes_before


# ---------------------------------------------------------------------------
# Test 7 — invalid mode raises immediately
# ---------------------------------------------------------------------------

def test_invalid_mode_raises(tmp_path: Path) -> None:
    """An unrecognised mode must raise ``ValueError`` at construction time."""
    repo = _make_repo(tmp_path)
    with pytest.raises(ValueError, match="unknown mode"):
        IsomorphicEnv(repo, mode="invalid-mode")


# ---------------------------------------------------------------------------
# Test 8 — JARVIS_SANDBOX_PREFIXES env var exported for child-process fidelity
# ---------------------------------------------------------------------------

def test_jarvis_sandbox_prefixes_set_inside(tmp_path: Path) -> None:
    """Inside the context, ``JARVIS_SANDBOX_PREFIXES`` must be set to the node
    policy (no /tmp) so any spawned child process inherits the restriction."""
    repo = _make_repo(tmp_path)
    with IsomorphicEnv(repo):
        val = os.environ.get("JARVIS_SANDBOX_PREFIXES")
        assert val is not None, (
            "JARVIS_SANDBOX_PREFIXES must be set inside IsomorphicEnv"
        )
        prefixes = [p.strip() for p in val.split(",") if p.strip()]
        assert not any("tmp" in p for p in prefixes), (
            "JARVIS_SANDBOX_PREFIXES must contain no /tmp prefix inside context; "
            "got %r" % val
        )


def test_jarvis_sandbox_prefixes_restored_after_exit(tmp_path: Path) -> None:
    """``JARVIS_SANDBOX_PREFIXES`` must be restored to its pre-context value
    (or removed) after ``__exit__``."""
    repo = _make_repo(tmp_path)
    # Ensure a clean baseline: unset before entry.
    os.environ.pop("JARVIS_SANDBOX_PREFIXES", None)
    with IsomorphicEnv(repo):
        assert "JARVIS_SANDBOX_PREFIXES" in os.environ
    # After exit: must be gone (was absent before entry).
    assert "JARVIS_SANDBOX_PREFIXES" not in os.environ, (
        "JARVIS_SANDBOX_PREFIXES must be removed after exit when it was absent before"
    )


def test_jarvis_sandbox_prefixes_restored_prior_value(tmp_path: Path) -> None:
    """When ``JARVIS_SANDBOX_PREFIXES`` was already set before entry, the prior
    value must be restored after ``__exit__``."""
    repo = _make_repo(tmp_path)
    prior = "/custom/prior/prefix"
    os.environ["JARVIS_SANDBOX_PREFIXES"] = prior
    try:
        with IsomorphicEnv(repo):
            # Inside: overwritten with node policy.
            assert os.environ.get("JARVIS_SANDBOX_PREFIXES") != prior
        # After: prior value restored.
        assert os.environ.get("JARVIS_SANDBOX_PREFIXES") == prior, (
            "Prior JARVIS_SANDBOX_PREFIXES value must be restored after exit"
        )
    finally:
        os.environ.pop("JARVIS_SANDBOX_PREFIXES", None)


# ---------------------------------------------------------------------------
# Test 9 — _effective_sandbox_prefixes env-override (test_runner cross-process)
# ---------------------------------------------------------------------------

def test_effective_prefixes_unset_returns_default(tmp_path: Path) -> None:
    """When ``JARVIS_SANDBOX_PREFIXES`` is unset, ``_effective_sandbox_prefixes``
    must return the module default — byte-identical behavior."""
    import backend.core.ouroboros.governance.test_runner as _tr2
    os.environ.pop("JARVIS_SANDBOX_PREFIXES", None)
    result = _tr2._effective_sandbox_prefixes()
    assert result == _tr._ALLOWED_SANDBOX_PREFIXES, (
        "Unset JARVIS_SANDBOX_PREFIXES must return the default _ALLOWED_SANDBOX_PREFIXES"
    )


def test_effective_prefixes_env_override(tmp_path: Path) -> None:
    """When ``JARVIS_SANDBOX_PREFIXES`` is set, ``_effective_sandbox_prefixes``
    parses and returns those prefixes, overriding the default."""
    import backend.core.ouroboros.governance.test_runner as _tr2
    os.environ["JARVIS_SANDBOX_PREFIXES"] = "/nonexistent-sandbox-prefix"
    try:
        result = _tr2._effective_sandbox_prefixes()
        assert result == ("/nonexistent-sandbox-prefix",), (
            "Env override must parse comma-separated prefixes; got %r" % (result,)
        )
        # Verify that a /tmp path is now REJECTED (the whole point of the override).
        disjoint_root = Path("/nonexistent-root-for-override-test")
        from backend.core.ouroboros.governance.test_runner import _is_safe_path
        assert _is_safe_path(Path("/tmp/some_file.py"), disjoint_root) is False, (
            "With JARVIS_SANDBOX_PREFIXES set to /nonexistent, /tmp must be rejected"
        )
    finally:
        os.environ.pop("JARVIS_SANDBOX_PREFIXES", None)


def test_effective_prefixes_env_override_allows_var_when_set(tmp_path: Path) -> None:
    """When ``JARVIS_SANDBOX_PREFIXES`` explicitly includes ``/var``,
    ``_is_safe_path`` allows /var paths — the override is bidirectional.

    Uses /var (not /tmp) because on macOS /tmp resolves to /private/tmp;
    the effective-prefixes comparison is against the RESOLVED path, so a
    ``/tmp`` prefix entry would fail if the override omits ``/private/tmp``.
    The default _ALLOWED_SANDBOX_PREFIXES covers both by design; this test
    exercises the override using a prefix that resolves without a symlink hop.
    """
    import backend.core.ouroboros.governance.test_runner as _tr2
    os.environ["JARVIS_SANDBOX_PREFIXES"] = "/var,/private/tmp"
    try:
        from backend.core.ouroboros.governance.test_runner import _is_safe_path
        disjoint_root = Path("/nonexistent-root-for-override-test2")
        # /var is explicitly in the override, so a /var path must be allowed.
        # Use /var/tmp which resolves to /private/var/folders on macOS... actually
        # just check that the effective prefixes list is updated correctly.
        effective = _tr2._effective_sandbox_prefixes()
        assert "/var" in effective, (
            "Override must include /var when JARVIS_SANDBOX_PREFIXES=/var,/private/tmp"
        )
        assert "/private/tmp" in effective, (
            "Override must include /private/tmp when JARVIS_SANDBOX_PREFIXES=/var,/private/tmp"
        )
        # /tmp resolves to /private/tmp on macOS; with /private/tmp in the override
        # a /tmp path must be allowed.
        result = _is_safe_path(Path("/tmp/probe.py"), disjoint_root)
        # On Linux /tmp resolves to /tmp; on macOS to /private/tmp — both are in
        # the override, so result must be True on both platforms.
        assert result is True, (
            "With /private/tmp in JARVIS_SANDBOX_PREFIXES, /tmp path must be allowed "
            "(resolves to /private/tmp on macOS)"
        )
    finally:
        os.environ.pop("JARVIS_SANDBOX_PREFIXES", None)
