"""Dynamic Dependency Mounting for the RepairSandbox.

Root cause (soak bt-2026-07-22-065329): VALIDATE ran the candidate's
scoped pytest in the ephemeral sandbox and hit
``[python:FAIL] ... ouroboros_pytest_plugin`` — the internal plugin the
suite registers via ``conftest.py``'s ``pytest_plugins`` failed to load
in the isolated environment.

Fix: the sandbox DYNAMICALLY resolves the internal test deps the suite
declares (AST-parse of ``pytest_plugins``, no hardcoded names) and
read-only-mounts any MISSING ones via ``os.symlink`` to the live repo
file — never overwriting the sandbox's own writable copies
(write-isolation preserved), plus the sandbox root on PYTHONPATH so the
dotted import resolves.

This suite proves — with a REAL pytest subprocess inside a REAL
isolated sandbox — that the plugin imports without ImportError.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_sandbox import (
    mount_internal_test_deps,
    resolve_internal_test_deps,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo_with_plugin(tmp_path: Path) -> Path:
    """A mock repo whose test suite registers an internal pytest plugin
    via ``conftest.py``'s ``pytest_plugins`` — the exact shape that
    broke in the sandbox."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("")
    # The internal plugin the suite depends on.
    (repo / "tests" / "internal_plugin.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef magic_number():\n"
        "    return 42\n"
    )
    # conftest registers it via the canonical pytest_plugins seam.
    (repo / "tests" / "conftest.py").write_text(
        'pytest_plugins = ["tests.internal_plugin"]\n'
    )
    # A test that USES the plugin fixture — proves live import.
    (repo / "tests" / "test_uses_plugin.py").write_text(
        "def test_magic(magic_number):\n    assert magic_number == 42\n"
    )
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


# ---------------------------------------------------------------------------
# 1. Dynamic resolution — no hardcoded plugin names
# ---------------------------------------------------------------------------


def test_resolves_declared_plugins_dynamically(repo_with_plugin: Path) -> None:
    deps = resolve_internal_test_deps(repo_with_plugin)
    assert "tests/internal_plugin.py" in deps
    assert "tests/__init__.py" in deps  # package ancestor for the dotted import
    assert "tests/conftest.py" in deps  # the declaring conftest itself


def test_resolution_ignores_nonexistent(tmp_path: Path) -> None:
    """A conftest declaring a plugin whose file is absent yields nothing
    for it (only EXISTING paths are returned) — no phantom mounts."""
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "conftest.py").write_text(
        'pytest_plugins = ["tests.does_not_exist"]\n'
    )
    deps = resolve_internal_test_deps(repo)
    assert "tests/does_not_exist.py" not in deps


# ---------------------------------------------------------------------------
# 2. Mounting — symlinks missing deps, never overwrites, preserves isolation
# ---------------------------------------------------------------------------


async def test_mount_symlinks_missing_deps(repo_with_plugin: Path, tmp_path: Path) -> None:
    # A sandbox that is MISSING the plugin (e.g. rsync-excluded infra).
    sandbox = tmp_path / "sandbox"
    (sandbox / "tests").mkdir(parents=True)
    (sandbox / "tests" / "test_uses_plugin.py").write_text("x")  # candidate present

    mounted = await asyncio.to_thread(
        mount_internal_test_deps, repo_with_plugin, sandbox,
    )

    assert "tests/internal_plugin.py" in mounted
    plugin = sandbox / "tests" / "internal_plugin.py"
    assert plugin.is_symlink()
    # Read-through resolves to the live repo file.
    assert plugin.resolve() == (repo_with_plugin / "tests" / "internal_plugin.py").resolve()


async def test_mount_never_overwrites_existing_sandbox_copy(
    repo_with_plugin: Path, tmp_path: Path,
) -> None:
    """Write-isolation: an existing sandbox copy (the git-worktree's
    writable tracked file) is NEVER replaced with a write-through
    symlink."""
    sandbox = tmp_path / "sandbox"
    (sandbox / "tests").mkdir(parents=True)
    # The sandbox already has its OWN copy (real file, not a symlink).
    real_copy = sandbox / "tests" / "internal_plugin.py"
    real_copy.write_text("# sandbox's own isolated copy\n")

    mounted = await asyncio.to_thread(
        mount_internal_test_deps, repo_with_plugin, sandbox,
    )

    assert "tests/internal_plugin.py" not in mounted
    assert not real_copy.is_symlink()  # untouched — isolation preserved
    assert real_copy.read_text() == "# sandbox's own isolated copy\n"


# ---------------------------------------------------------------------------
# 3. The mandated proof — a REAL pytest run in an isolated sandbox imports
#    the plugin without ImportError
# ---------------------------------------------------------------------------


async def test_pytest_imports_plugin_inside_mounted_sandbox(
    repo_with_plugin: Path, tmp_path: Path,
) -> None:
    # Build an isolated sandbox that LACKS the internal plugin + conftest
    # (simulating the fidelity gap) but has the candidate test.
    sandbox = tmp_path / "sandbox"
    (sandbox / "tests").mkdir(parents=True)
    (sandbox / "tests" / "test_uses_plugin.py").write_text(
        (repo_with_plugin / "tests" / "test_uses_plugin.py").read_text()
    )
    (sandbox / "pytest.ini").write_text(
        (repo_with_plugin / "pytest.ini").read_text()
    )

    # Without the mount, the plugin fixture is unresolvable → error.
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(sandbox)
    pre = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "tests/test_uses_plugin.py",
        "-q", "--no-header", "-c", "pytest.ini",
        cwd=str(sandbox), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    pre_out, _ = await pre.communicate()
    assert pre.returncode != 0, "control: unmounted sandbox should fail"

    # Mount the internal deps, then run again — plugin imports cleanly.
    await asyncio.to_thread(
        mount_internal_test_deps, repo_with_plugin, sandbox,
    )
    post = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "tests/test_uses_plugin.py",
        "-q", "--no-header", "-c", "pytest.ini",
        cwd=str(sandbox), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    post_out, _ = await post.communicate()
    text = post_out.decode("utf-8", errors="replace")

    assert post.returncode == 0, (
        f"mounted sandbox pytest failed:\n{text}"
    )
    assert "1 passed" in text
    assert "internal_plugin" not in text or "error" not in text.lower()


async def test_mount_disabled_env(
    repo_with_plugin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_SANDBOX_MOUNT_TEST_DEPS_ENABLED", "false")
    sandbox = tmp_path / "sandbox"
    (sandbox / "tests").mkdir(parents=True)
    mounted = await asyncio.to_thread(
        mount_internal_test_deps, repo_with_plugin, sandbox,
    )
    assert mounted == []
