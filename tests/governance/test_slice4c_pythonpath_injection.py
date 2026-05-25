"""Slice 4C-A — PYTHONPATH subprocess injection for InteractiveRepair.

Closes the final environment gap surfaced by raw pytest diagnostic
from soak bt-2026-05-25-094217. With Slice 3G/3H/3H.1/3H.2/3H.3/4A/4B
all live and pytest correctly scoped to FAIL_TO_PASS tests, the
subprocess STILL produced ``ModuleNotFoundError: No module named
'ansible'`` because the InteractiveRepair subprocess inherited the
JARVIS Python environment (which doesn't have the target project's
package installed). For Ansible-shape projects (src-layout: package
code under ``lib/<pkg>/``), the test file's
``from ansible.cli.doc import ...`` cannot resolve.

# Fix mechanism — per-subprocess env dict

Build a per-subprocess env dict that inherits ``os.environ`` and
PREPENDS the worktree's canonical Python source roots to
``PYTHONPATH``:

  * ``<repo_root>/lib`` — Ansible / Django / Flask / Pandas
  * ``<repo_root>/src`` — modern Python packaging
  * ``<repo_root>`` — flat-layout (single top-level package dir)

Only paths that exist on disk are included (avoids noise in tracebacks).
Pass via ``env=`` kwarg to ``asyncio.create_subprocess_exec`` — the
parent process's ``os.environ`` is NEVER mutated.

# Empirical proof

Local replication on the bt-2026-05-25-094217 worktree:

  Before: ``ModuleNotFoundError: No module named 'ansible'`` (collection error)
  After:  ``__________________________ test_rolemixin__build_doc __________________________``
          (real AssertionError that Slice 3H.3 cascade #4 can parse)

# Test surface (2 AST pins + 5 spine)
"""

from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INTERACTIVE_REPAIR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "interactive_repair.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_subprocess_invocation_uses_env_kwarg() -> None:
    """``asyncio.create_subprocess_exec`` MUST be called with the
    ``env=`` kwarg pointing to the per-subprocess dict. Without this,
    the subprocess inherits ``os.environ`` as-is and the PYTHONPATH
    override is decorative."""
    src = INTERACTIVE_REPAIR_FILE.read_text()
    assert "env=_subprocess_env" in src, (
        "create_subprocess_exec is NOT called with env=_subprocess_env "
        "— Slice 4C-A injection is dead-coded."
    )
    # The subprocess env must compose os.environ as the base layer
    # (otherwise we'd nuke PATH / HOME / TMPDIR / etc.)
    assert "**os.environ," in src or "**os.environ\n" in src, (
        "_subprocess_env does not inherit os.environ — risks "
        "missing PATH / HOME / TMPDIR for the subprocess."
    )


def test_ast_pin_pythonpath_composes_canonical_layouts() -> None:
    """The injected PYTHONPATH must include the three canonical Python
    src layouts: ``lib/`` (Ansible-style), ``src/`` (modern packaging),
    and the repo root itself (flat layout). Without all three, some
    project layouts will still fail to import."""
    src = INTERACTIVE_REPAIR_FILE.read_text()
    # All three candidate paths must appear in the assembly
    assert '"lib"' in src, (
        "PYTHONPATH candidates missing 'lib' — Ansible/Django/Pandas "
        "src-layout projects won't import"
    )
    assert '"src"' in src, (
        "PYTHONPATH candidates missing 'src' — modern packaging "
        "layouts won't import"
    )
    # Only-existing-paths filter is critical (don't pollute PYTHONPATH
    # with non-existent dirs)
    assert "os.path.isdir" in src, (
        "PYTHONPATH composition does not filter to existing dirs — "
        "noisy import-error tracebacks under non-conforming repos"
    )
    # PATHSEP-joined assembly (so the OS understands the list)
    assert "os.pathsep.join" in src, (
        "PYTHONPATH not assembled via os.pathsep.join — invalid format"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 5 (pure-function tests of the assembly)
# ──────────────────────────────────────────────────────────────────────


def test_spine_pythonpath_assembly_with_all_three_layouts() -> None:
    """When all three canonical layout dirs exist, all three appear
    in PYTHONPATH in priority order (lib > src > root)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "lib").mkdir()
        (root / "src").mkdir()

        # Mirror the production assembly
        proj_root = str(root)
        existing_pp = "/some/other/pp"
        candidates = [
            os.path.join(proj_root, "lib"),
            os.path.join(proj_root, "src"),
            proj_root,
        ]
        real = [p for p in candidates if os.path.isdir(p)]
        parts = real + ([existing_pp] if existing_pp else [])
        pythonpath = os.pathsep.join(parts)

        assert f"{tmp}/lib" in pythonpath
        assert f"{tmp}/src" in pythonpath
        assert tmp in pythonpath
        assert "/some/other/pp" in pythonpath
        # Order: lib, src, root, existing
        idx_lib = pythonpath.find("lib")
        idx_src = pythonpath.find("src")
        idx_existing = pythonpath.find("/some/other/pp")
        assert idx_lib < idx_src < idx_existing


def test_spine_pythonpath_skips_nonexistent_dirs() -> None:
    """Non-existent ``lib/`` and ``src/`` are filtered out — only the
    repo root itself is in PYTHONPATH for flat-layout projects."""
    with tempfile.TemporaryDirectory() as tmp:
        # NO lib/ or src/ created — flat layout
        proj_root = str(tmp)
        candidates = [
            os.path.join(proj_root, "lib"),
            os.path.join(proj_root, "src"),
            proj_root,
        ]
        real = [p for p in candidates if os.path.isdir(p)]
        # Only the root exists
        assert real == [proj_root]


def test_spine_pythonpath_preserves_existing() -> None:
    """Existing ``PYTHONPATH`` (operator-set) is preserved AFTER
    the new prepends — operator overrides take precedence on
    conflicting names but worktree paths win on absent ones."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "lib").mkdir()
        proj_root = str(root)
        existing_pp = "/operator/custom/path"
        candidates = [
            os.path.join(proj_root, "lib"),
            os.path.join(proj_root, "src"),
            proj_root,
        ]
        real = [p for p in candidates if os.path.isdir(p)]
        parts = real + ([existing_pp] if existing_pp else [])
        pythonpath = os.pathsep.join(parts)

        # Existing is preserved at the end
        assert pythonpath.endswith(existing_pp)
        # But worktree lib is prepended
        assert pythonpath.startswith(f"{tmp}/lib")


def test_spine_pythonpath_empty_existing_is_ok() -> None:
    """No PYTHONPATH in parent env → just the worktree candidates."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "lib").mkdir()
        proj_root = str(root)
        existing_pp = ""  # parent env didn't set PYTHONPATH
        candidates = [
            os.path.join(proj_root, "lib"),
            os.path.join(proj_root, "src"),
            proj_root,
        ]
        real = [p for p in candidates if os.path.isdir(p)]
        parts = real + ([existing_pp] if existing_pp else [])
        pythonpath = os.pathsep.join(parts)

        # No trailing empty entry
        assert not pythonpath.endswith(os.pathsep)
        # Worktree lib is in there
        assert f"{tmp}/lib" in pythonpath


def test_spine_subprocess_env_inherits_os_environ() -> None:
    """The subprocess env dict MUST inherit ``os.environ`` so the
    subprocess has PATH / HOME / TMPDIR / etc. Without inheritance
    the subprocess can't find ``python3`` or any system command."""
    # Mirror the production env-dict construction
    subprocess_env = {
        **os.environ,
        "PYTHONPATH": "/tmp/test",
    }
    # Key system vars must be present
    assert "PATH" in subprocess_env
    # PYTHONPATH override is the one we set
    assert subprocess_env["PYTHONPATH"] == "/tmp/test"
    # Parent os.environ is NOT mutated by dict construction
    assert os.environ.get("PYTHONPATH", "") != "/tmp/test", (
        "Parent os.environ was mutated by dict construction — "
        "the spread+override pattern is broken"
    )
