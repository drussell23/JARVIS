"""Tests for Boot-Time Differential Hydration (TestWatcher offline-state fix).

THE BUG (A1 live soak): the TestWatcher relies on live ``fs.changed`` events,
but the chaos bug is mutated BEFORE O+V boots -> no event fires -> the full-suite
poll SIGKILLs at 180s -> the bug is never detected. Architecturally, an
event-driven watcher that boots AFTER a state mutation loses it forever (also
true on any crash/restart in prod).

The fix reconstructs missed mutations from ground truth (the working tree) on
every boot:

1. ``TestWatcher.diff_working_tree()`` runs an async ``git diff --name-only
   HEAD`` (+ untracked ``.py`` via ``git ls-files --others``) to enumerate
   uncommitted changes -- non-blocking subprocess, fail-soft.
2. ``TestFailureSensor.hydrate_on_boot()`` resolves each changed source file
   to its tests via the EXISTING ``resolve_affected_tests`` and runs the
   localized SCOPED pytest (NOT ``tests/``), de-duping against later live
   ``fs.changed`` events for the same file.

Both gated ``JARVIS_TESTWATCHER_BOOT_HYDRATION_ENABLED`` (default true);
OFF == legacy byte-identical.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import (
    TestFailure,
    TestWatcher,
)
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRouter:
    def __init__(self) -> None:
        self.ingested: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.ingested.append(envelope)
        return "enqueued"


class _RecordingWatcher:
    """A TestWatcher stand-in that records the target_paths it was asked to run.

    ``poll_once`` returns one stable IntentSignal so we can prove the hydration
    path reaches ``handle_signals`` -> ``router.ingest``.
    """

    def __init__(self, repo_path: str, *, diff_files: Sequence[str]) -> None:
        self.repo_path = repo_path
        self.poll_interval_s = 30.0
        self._diff_files = list(diff_files)
        self.poll_calls: List[Optional[Sequence[str]]] = []

    async def diff_working_tree(self) -> List[str]:
        return list(self._diff_files)

    async def poll_once(
        self, target_paths: Optional[Sequence[str]] = None
    ) -> List[Any]:
        self.poll_calls.append(
            list(target_paths) if target_paths is not None else None
        )
        # Emit a stable signal targeting the resolved test file so the
        # sensor's ingest path is exercised.
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        target = (target_paths[0] if target_paths else "tests/unknown.py")
        return [
            IntentSignal(
                source="intent:test_failure",
                target_files=(target,),
                repo="jarvis",
                description="hydrated stable failure",
                evidence={"signature": "x"},
                confidence=0.9,
                stable=True,
            )
        ]


def _make_sensor(
    repo_path: str, diff_files: Sequence[str]
) -> Tuple[TestFailureSensor, _RecordingWatcher, _FakeRouter]:
    watcher = _RecordingWatcher(repo_path, diff_files=diff_files)
    router = _FakeRouter()
    sensor = TestFailureSensor(repo="jarvis", router=router, test_watcher=watcher)
    return sensor, watcher, router


# ---------------------------------------------------------------------------
# 1. Watcher.diff_working_tree runs git diff --name-only HEAD (async, non-block)
# ---------------------------------------------------------------------------


class TestDiffWorkingTree:
    def test_runs_git_diff_name_only_head(self, monkeypatch: Any) -> None:
        captured: Dict[str, Any] = {}

        async def _fake_exec(*argv: str, **kwargs: Any) -> Any:
            captured.setdefault("argvs", []).append(list(argv))

            class _Proc:
                returncode = 0

                async def communicate(self) -> Tuple[bytes, bytes]:
                    if "diff" in argv:
                        return (b"backend/core/utils/calc.py\n", b"")
                    # ls-files --others
                    return (b"", b"")

            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        watcher = TestWatcher(repo="jarvis", repo_path="/repo")
        out = asyncio.run(watcher.diff_working_tree())

        # Assert the git diff --name-only HEAD command was issued.
        diff_argv = next(a for a in captured["argvs"] if "diff" in a)
        assert "git" in diff_argv[0] or diff_argv[0] == "git"
        assert "diff" in diff_argv
        assert "--name-only" in diff_argv
        assert "HEAD" in diff_argv
        assert "backend/core/utils/calc.py" in out

    def test_includes_untracked_py(self, monkeypatch: Any) -> None:
        async def _fake_exec(*argv: str, **kwargs: Any) -> Any:
            class _Proc:
                returncode = 0

                async def communicate(self) -> Tuple[bytes, bytes]:
                    if "diff" in argv:
                        return (b"", b"")
                    if "ls-files" in argv:
                        return (b"backend/new_mod.py\nREADME.md\n", b"")
                    return (b"", b"")

            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        watcher = TestWatcher(repo="jarvis", repo_path="/repo")
        out = asyncio.run(watcher.diff_working_tree())
        assert "backend/new_mod.py" in out
        # Non-.py untracked files are filtered out.
        assert "README.md" not in out

    def test_git_error_returns_empty_no_raise(self, monkeypatch: Any) -> None:
        async def _boom(*argv: str, **kwargs: Any) -> Any:
            raise OSError("git not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
        watcher = TestWatcher(repo="jarvis", repo_path="/repo")
        # Must NOT raise -- graceful skip.
        out = asyncio.run(watcher.diff_working_tree())
        assert out == []

    def test_nonzero_returncode_returns_empty(self, monkeypatch: Any) -> None:
        async def _fake_exec(*argv: str, **kwargs: Any) -> Any:
            class _Proc:
                returncode = 128

                async def communicate(self) -> Tuple[bytes, bytes]:
                    return (b"", b"fatal: not a git repository")

            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        watcher = TestWatcher(repo="jarvis", repo_path="/repo")
        out = asyncio.run(watcher.diff_working_tree())
        assert out == []


# ---------------------------------------------------------------------------
# 2. Core fix: a pre-boot-mutated file is detected on boot with NO fs.changed
# ---------------------------------------------------------------------------


class TestPreBootMutationDetected:
    def test_pre_boot_mutation_detected_via_git_diff(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The whole point: a file mutated BEFORE boot (no fs.changed event)
        is reconstructed from `git diff` and its tests run scoped."""
        # The changed source file the chaos injector mutated pre-boot.
        changed = "backend/core/utils/calc.py"
        sensor, watcher, router = _make_sensor(str(tmp_path), [changed])

        # Resolver maps calc.py -> tests/test_calc.py (scoped, NOT tests/).
        async def _fake_resolve(self_sensor: Any, rel: str) -> Optional[List[str]]:
            assert rel == changed
            return ["tests/utils/test_calc.py"]

        monkeypatch.setattr(
            TestFailureSensor, "_resolve_scoped_targets", _fake_resolve
        )

        n = asyncio.run(sensor.hydrate_on_boot())

        # A scoped pytest ran with the resolved test target (NOT the whole suite).
        assert watcher.poll_calls, "hydration must run at least one scoped poll"
        ran = watcher.poll_calls[0]
        assert ran == ["tests/utils/test_calc.py"]
        assert ran != ["tests/"] and "tests/" not in (ran or [])
        # The stable signal was ingested -> bug detected with NO fs.changed.
        assert n >= 1
        assert router.ingested, "the pre-boot bug must reach the router"

    def test_scoped_not_full_suite(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        sensor, watcher, _router = _make_sensor(
            str(tmp_path), ["backend/a.py"]
        )

        async def _fake_resolve(self_sensor: Any, rel: str) -> Optional[List[str]]:
            return ["tests/test_a.py"]

        monkeypatch.setattr(
            TestFailureSensor, "_resolve_scoped_targets", _fake_resolve
        )
        asyncio.run(sensor.hydrate_on_boot())
        for call in watcher.poll_calls:
            assert call is not None  # never the whole-suite (None) sweep
            assert "tests/" not in call


# ---------------------------------------------------------------------------
# 3. De-dupe: a hydrated file later touched live is not double-run
# ---------------------------------------------------------------------------


class TestDeDupeVsLiveEvent:
    def test_hydrated_file_not_rerun_by_later_fs_event(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        changed = "backend/core/utils/calc.py"
        sensor, watcher, _router = _make_sensor(str(tmp_path), [changed])

        async def _fake_resolve(self_sensor: Any, rel: str) -> Optional[List[str]]:
            return ["tests/utils/test_calc.py"]

        monkeypatch.setattr(
            TestFailureSensor, "_resolve_scoped_targets", _fake_resolve
        )

        # Boot hydration records the file as freshly hydrated.
        asyncio.run(sensor.hydrate_on_boot())
        calls_after_boot = len(watcher.poll_calls)

        # A live fs.changed for the SAME file arriving immediately after boot
        # must be suppressed by the hydration de-dupe window.
        assert sensor._is_recently_hydrated(changed) is True

        # A DIFFERENT file is not suppressed.
        assert sensor._is_recently_hydrated("backend/core/other.py") is False
        assert len(watcher.poll_calls) == calls_after_boot


# ---------------------------------------------------------------------------
# 4. OFF flag -> legacy behavior byte-identical (no hydration runs)
# ---------------------------------------------------------------------------


class TestGatedOff:
    def test_off_flag_skips_hydration(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("JARVIS_TESTWATCHER_BOOT_HYDRATION_ENABLED", "false")
        sensor, watcher, router = _make_sensor(
            str(tmp_path), ["backend/a.py"]
        )

        async def _fake_resolve(self_sensor: Any, rel: str) -> Optional[List[str]]:
            return ["tests/test_a.py"]

        monkeypatch.setattr(
            TestFailureSensor, "_resolve_scoped_targets", _fake_resolve
        )
        n = asyncio.run(sensor.hydrate_on_boot())
        assert n == 0
        assert watcher.poll_calls == []
        assert router.ingested == []

    def test_no_diff_no_runs(self, monkeypatch: Any, tmp_path: Path) -> None:
        # Clean working tree -> nothing to hydrate.
        sensor, watcher, _router = _make_sensor(str(tmp_path), [])
        n = asyncio.run(sensor.hydrate_on_boot())
        assert n == 0
        assert watcher.poll_calls == []
