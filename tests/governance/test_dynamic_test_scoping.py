"""Dynamic test scoping — FS-changed file -> scoped pytest run.

Validates the wiring that fixes the 180s SIGKILL which blocked O+V chaos
self-detection in the A1 soak:

* ``TestWatcher.run_pytest(target_paths=[...])`` builds an argv targeting the
  SCOPED paths, not the whole ``tests/`` directory.
* The sensor's FS path resolves a changed source file -> scoped test targets
  (via the EXISTING ``TestRunner.resolve_affected_tests``) ->
  ``poll_once(target_paths=...)``.
* Resolver miss -> bounded mirror-dir fallback, NEVER the whole ``tests/``.
* Master gate OFF -> byte-identical legacy whole-suite argv.
* Back-compat: no ``target_paths`` -> legacy argv.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
    dynamic_scoping_enabled,
    full_suite_fallback_enabled,
)


# ---------------------------------------------------------------------------
# Helpers: capture the argv handed to the canonical pytest subprocess helper
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self) -> None:
        self.timed_out = False
        self.returncode = 0
        self.stdout = ""


def _patch_subprocess(monkeypatch) -> List[List[str]]:
    """Patch run_pytest_subprocess; return a list captured argvs are appended to."""
    captured: List[List[str]] = []

    async def _fake(argv, *, cwd, timeout_s, caller):  # noqa: ANN001
        captured.append(list(argv))
        return _FakeResult()

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.test_subprocess_helper."
        "run_pytest_subprocess",
        _fake,
    )
    return captured


# ---------------------------------------------------------------------------
# 1. run_pytest(target_paths=[...]) -> scoped argv, NOT tests/
# ---------------------------------------------------------------------------


def test_run_pytest_scoped_argv_targets_paths(monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    watcher = TestWatcher(repo="jarvis", test_dir="tests/")

    out, rc = asyncio.run(
        watcher.run_pytest(
            target_paths=["tests/governance/test_foo.py", "tests/test_bar.py"]
        )
    )

    assert rc == 0
    argv = captured[0]
    assert "tests/governance/test_foo.py" in argv
    assert "tests/test_bar.py" in argv
    # The whole-suite root must NOT be a bare argv token.
    assert "tests/" not in argv
    # Other flags preserved.
    for flag in ("--tb=short", "-q", "--no-header", "--color=no"):
        assert flag in argv


def test_run_pytest_no_target_paths_legacy_argv(monkeypatch) -> None:
    """Back-compat: no target_paths -> legacy whole-test_dir argv."""
    captured = _patch_subprocess(monkeypatch)
    watcher = TestWatcher(repo="jarvis", test_dir="tests/")

    asyncio.run(watcher.run_pytest())

    argv = captured[0]
    assert "tests/" in argv


def test_run_pytest_empty_target_paths_falls_back_to_legacy(monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    watcher = TestWatcher(repo="jarvis", test_dir="tests/")

    asyncio.run(watcher.run_pytest(target_paths=[]))

    argv = captured[0]
    assert "tests/" in argv


def test_poll_once_passes_target_paths_through(monkeypatch) -> None:
    captured = _patch_subprocess(monkeypatch)
    watcher = TestWatcher(repo="jarvis", test_dir="tests/")

    asyncio.run(watcher.poll_once(target_paths=["tests/test_scoped.py"]))

    argv = captured[0]
    assert "tests/test_scoped.py" in argv
    assert "tests/" not in argv


# ---------------------------------------------------------------------------
# Sensor FS-path fixtures
# ---------------------------------------------------------------------------


class _SpyWatcher:
    """Records poll_once(target_paths=...) calls; no subprocess."""

    def __init__(self, repo_path: str) -> None:
        self.repo_path = repo_path
        self.poll_interval_s = 30.0
        self.calls: List[Optional[Sequence[str]]] = []

    async def poll_once(
        self, target_paths: Optional[Sequence[str]] = None
    ) -> list:
        self.calls.append(target_paths)
        return []

    def stop(self) -> None:
        pass


class _Event:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


def _build_repo_tree(tmp_path: Path) -> Tuple[Path, Path, Path]:
    """Create repo/pkg/foo.py + repo/pkg/tests/test_foo.py. Return (root, src, test)."""
    pkg = tmp_path / "pkg"
    tests_dir = pkg / "tests"
    tests_dir.mkdir(parents=True)
    src = pkg / "foo.py"
    src.write_text("x = 1\n", encoding="utf-8")
    test = tests_dir / "test_foo.py"
    test.write_text("def test_foo():\n    assert True\n", encoding="utf-8")
    return tmp_path, src, test


# ---------------------------------------------------------------------------
# 2. FS path: changed file -> scoped targets via resolve_affected_tests
# ---------------------------------------------------------------------------


def test_fs_path_resolves_scoped_targets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DYNAMIC_SCOPING_ENABLED", "true")
    root, src, test = _build_repo_tree(tmp_path)

    watcher = _SpyWatcher(repo_path=str(root))
    sensor = TestFailureSensor(repo="jarvis", router=object(), test_watcher=watcher)
    sensor._last_plugin_ts = -1e9  # ensure no plugin-suppression
    # Skip the 2s debounce sleep.
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(sensor._debounced_pytest_run(changed_rel_path="pkg/foo.py"))

    assert len(watcher.calls) == 1
    targets = watcher.calls[0]
    assert targets is not None
    # Resolved to the file's own test (name convention), not the whole suite.
    assert any(str(test) in t for t in targets)
    # Never the whole tests/ root passed implicitly.
    assert all(not t.endswith("/tests") and t != "tests" for t in targets)


# ---------------------------------------------------------------------------
# 3. Resolver miss -> bounded mirror-dir fallback, NEVER whole tests/
# ---------------------------------------------------------------------------


def test_resolver_miss_falls_back_to_mirror_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DYNAMIC_SCOPING_ENABLED", "true")
    # pkg/tests/ exists but has no matching test_*.py for bar.py and no
    # recursive match -> strategy 1/2 miss; package fallback is empty;
    # strategy 4 would be the repo root -> filtered out.
    pkg = tmp_path / "pkg"
    tests_dir = pkg / "tests"
    tests_dir.mkdir(parents=True)
    (pkg / "bar.py").write_text("y = 2\n", encoding="utf-8")

    watcher = _SpyWatcher(repo_path=str(tmp_path))
    sensor = TestFailureSensor(repo="jarvis", router=object(), test_watcher=watcher)
    sensor._last_plugin_ts = -1e9
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(sensor._debounced_pytest_run(changed_rel_path="pkg/bar.py"))

    assert len(watcher.calls) == 1
    targets = watcher.calls[0]
    assert targets is not None
    # Bounded to the single sibling mirror dir, not the repo-root tests/.
    assert len(targets) == 1
    assert targets[0] == str(tests_dir.resolve())
    assert targets[0] != str((tmp_path / "tests").resolve())


def test_unresolvable_change_skips_run_without_full_suite(tmp_path, monkeypatch) -> None:
    """No mirror dir at all + full-suite fallback off -> NO poll (never full suite)."""
    monkeypatch.setenv("JARVIS_TEST_DYNAMIC_SCOPING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TEST_FULL_SUITE_FALLBACK", "false")
    # A bare repo with no tests/ dir anywhere up the tree from the file.
    src = tmp_path / "loose.py"
    src.write_text("z = 3\n", encoding="utf-8")

    watcher = _SpyWatcher(repo_path=str(tmp_path))
    sensor = TestFailureSensor(repo="jarvis", router=object(), test_watcher=watcher)
    sensor._last_plugin_ts = -1e9
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(sensor._debounced_pytest_run(changed_rel_path="loose.py"))

    # Resolver last-resort would be repo-root tests/ (which doesn't exist
    # here) -> nothing -> skip. Crucially: poll_once was NOT called blind.
    assert watcher.calls == []


def test_unresolvable_change_full_suite_opt_in(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DYNAMIC_SCOPING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TEST_FULL_SUITE_FALLBACK", "true")
    src = tmp_path / "loose.py"
    src.write_text("z = 3\n", encoding="utf-8")

    watcher = _SpyWatcher(repo_path=str(tmp_path))
    sensor = TestFailureSensor(repo="jarvis", router=object(), test_watcher=watcher)
    sensor._last_plugin_ts = -1e9
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(sensor._debounced_pytest_run(changed_rel_path="loose.py"))

    # Opt-in -> exactly one whole-suite poll (target_paths is None).
    assert len(watcher.calls) == 1
    assert watcher.calls[0] is None


# ---------------------------------------------------------------------------
# 4. Master gate OFF -> byte-identical legacy whole-suite behavior
# ---------------------------------------------------------------------------


def test_scoping_off_is_legacy_whole_suite(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DYNAMIC_SCOPING_ENABLED", "false")
    root, src, test = _build_repo_tree(tmp_path)

    watcher = _SpyWatcher(repo_path=str(root))
    sensor = TestFailureSensor(repo="jarvis", router=object(), test_watcher=watcher)
    sensor._last_plugin_ts = -1e9
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    asyncio.run(sensor._debounced_pytest_run(changed_rel_path="pkg/foo.py"))

    # OFF -> poll_once() called with NO target_paths (legacy).
    assert len(watcher.calls) == 1
    assert watcher.calls[0] is None


def test_gate_helpers_default(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_TEST_DYNAMIC_SCOPING_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_TEST_FULL_SUITE_FALLBACK", raising=False)
    assert dynamic_scoping_enabled() is True
    assert full_suite_fallback_enabled() is False


# ---------------------------------------------------------------------------
# Shared: no-op sleep so debounce doesn't actually wait 2s
# ---------------------------------------------------------------------------


async def _no_sleep(_delay, *a, **k):  # noqa: ANN001
    return None
