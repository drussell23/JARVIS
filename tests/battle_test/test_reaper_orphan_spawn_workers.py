"""Regression spine — boot reaper orphan-spawn-worker extension
(2026-07-11 OOM RCA).

The gap: the zombie reaper path-tail matches only
``ouroboros_battle_test.py`` — a multiprocessing spawn worker's
cmdline (``python -c "from multiprocessing.spawn import spawn_main;
..." --multiprocessing-fork``) never matches, so orphaned Oracle/FS-
pool workers survived across sessions (caught live: 33.9 GB + 7 GB,
29 h old). The extension reaps them at boot, scoped by three
conjunctive discriminators so no other app's workers are touched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def bt():
    spec = importlib.util.spec_from_file_location(
        "ouroboros_battle_test_reaper_test",
        _REPO / "scripts" / "ouroboros_battle_test.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_SPAWN_CMDLINE = [
    "/usr/bin/python3",
    "-Wignore::UserWarning:multiprocessing.resource_tracker",
    "-c",
    "from multiprocessing.spawn import spawn_main; "
    "spawn_main(tracker_fd=11, pipe_handle=13)",
    "--multiprocessing-fork",
]


# ---------------------------------------------------------------------------
# cmdline shape
# ---------------------------------------------------------------------------


def test_spawn_cmdline_matches_real_worker_shape(bt) -> None:
    assert bt._is_spawn_worker_cmdline(_SPAWN_CMDLINE)


@pytest.mark.parametrize("cmdline", [
    [],
    ["/bin/bash", "-c", "spawn_main --multiprocessing-fork"],  # not python
    ["/usr/bin/python3", "scripts/ouroboros_battle_test.py"],  # main, not worker
    # spawn_main without the fork flag (someone's -c experiment)
    ["/usr/bin/python3", "-c", "from multiprocessing.spawn import spawn_main"],
    # fork flag without spawn bootstrap
    ["/usr/bin/python3", "worker.py", "--multiprocessing-fork"],
])
def test_spawn_cmdline_rejects_non_workers(bt, cmdline: list) -> None:
    assert not bt._is_spawn_worker_cmdline(cmdline)


# ---------------------------------------------------------------------------
# session fingerprint
# ---------------------------------------------------------------------------


def test_env_marker_active_session_log(bt) -> None:
    assert bt._has_session_env_marker(
        {"JARVIS_ACTIVE_SESSION_LOG": "/x/.ouroboros/sessions/bt-1/debug.log"}
    )


def test_env_marker_battle_prefix(bt) -> None:
    assert bt._has_session_env_marker({"OUROBOROS_BATTLE_MAX_WALL_SECONDS": "5000"})


def test_env_marker_rejects_foreign_env(bt) -> None:
    assert not bt._has_session_env_marker(
        {"PATH": "/usr/bin", "HOME": "/Users/x", "VIRTUAL_ENV": "/x/venv"}
    )


# ---------------------------------------------------------------------------
# full predicate — three conjunctive discriminators, fail-safe
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, *, ppid: int, cmdline: list, environ: dict,
                 environ_raises: bool = False) -> None:
        self._ppid = ppid
        self._cmdline = cmdline
        self._environ = environ
        self._environ_raises = environ_raises

    def ppid(self) -> int:
        return self._ppid

    def cmdline(self) -> list:
        return self._cmdline

    def environ(self) -> dict:
        if self._environ_raises:
            raise PermissionError("AccessDenied")
        return self._environ


_SESSION_ENV = {"JARVIS_ACTIVE_SESSION_LOG": "/r/.ouroboros/sessions/bt-1/debug.log"}


def test_orphan_predicate_matches_the_live_caught_class(bt) -> None:
    proc = _FakeProc(ppid=1, cmdline=_SPAWN_CMDLINE, environ=_SESSION_ENV)
    assert bt._is_orphaned_session_spawn_worker(proc)


def test_orphan_predicate_spares_live_session_workers(bt) -> None:
    # Parent alive (the organism) → ppid != 1 → NEVER reaped.
    proc = _FakeProc(ppid=4242, cmdline=_SPAWN_CMDLINE, environ=_SESSION_ENV)
    assert not bt._is_orphaned_session_spawn_worker(proc)


def test_orphan_predicate_spares_foreign_orphan_workers(bt) -> None:
    # Another app's orphaned pool worker: right shape, wrong env.
    proc = _FakeProc(
        ppid=1, cmdline=_SPAWN_CMDLINE, environ={"PATH": "/usr/bin"},
    )
    assert not bt._is_orphaned_session_spawn_worker(proc)


def test_orphan_predicate_fail_safe_on_environ_denial(bt) -> None:
    # environ() unreadable → cannot prove ownership → NOT reaped.
    proc = _FakeProc(
        ppid=1, cmdline=_SPAWN_CMDLINE, environ={}, environ_raises=True,
    )
    assert not bt._is_orphaned_session_spawn_worker(proc)
