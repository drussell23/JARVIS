"""Tests for the Stage B file-isolation soak verifier's pure assessment."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = _ROOT / "scripts" / "verify_file_isolation.py"
    spec = importlib.util.spec_from_file_location("vfi_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


vfi = _load()

_ROUTED = (
    "[FileIsolation] routed project_root -> "
    "/repo/.worktrees/ouroboros__auto__bt-2026-06-16-x "
    "(session=bt-2026-06-16-x branch=ouroboros/auto/bt-2026-06-16-x)"
)
_QPATH = "/repo/.worktrees/ouroboros__auto__bt-2026-06-16-x"
_COMPLETE = {"session_outcome": "complete"}


def test_all_invariants_pass():
    log = (
        "phase=GENERATE\n" + _ROUTED + "\n"
        f"APPLY wrote {_QPATH}/backend/x.py\n"
        f"VALIDATE cwd={_QPATH}\n"
    )
    v = vfi.assess_isolation(
        debug_log=log,
        primary_status_porcelain="",          # primary clean
        worktree_list_porcelain="",            # not listed → reaped
        summary=_COMPLETE,
    )
    assert v.overall == vfi.PASS
    assert v.quarantine_path == _QPATH
    keys = {i.key: i.status for i in v.invariants}
    assert keys["I1_worktree_initialized"] == vfi.PASS
    assert keys["I2_mutations_quarantined"] == vfi.PASS
    assert keys["I3_primary_pristine"] == vfi.PASS
    assert keys["I4_worktree_reaped"] == vfi.PASS


def test_primary_dirty_fails_i3():
    log = "phase=GENERATE\n" + _ROUTED + "\n" + _QPATH + " again\n"
    v = vfi.assess_isolation(
        debug_log=log,
        primary_status_porcelain=" M backend/core/ouroboros/x.py\n",
        worktree_list_porcelain="",
        summary=_COMPLETE,
    )
    assert v.overall == vfi.FAIL
    keys = {i.key: i.status for i in v.invariants}
    assert keys["I3_primary_pristine"] == vfi.FAIL


def test_primary_soak_artifacts_ignored():
    # .ouroboros/.worktrees/.jarvis untracked noise must NOT fail I3.
    porcelain = (
        "?? .ouroboros/sessions/bt-x/\n"
        "?? .worktrees/ouroboros__auto__bt-x/\n"
        "?? .jarvis/episodic_memory.jsonl\n"
    )
    log = "phase=GENERATE\n" + _ROUTED + "\n" + _QPATH + " ref\n"
    v = vfi.assess_isolation(
        debug_log=log, primary_status_porcelain=porcelain,
        worktree_list_porcelain="", summary=_COMPLETE,
    )
    keys = {i.key: i.status for i in v.invariants}
    assert keys["I3_primary_pristine"] == vfi.PASS


def test_no_routing_marker_fails_i1():
    v = vfi.assess_isolation(
        debug_log="phase=GENERATE\nno isolation here\n",
        primary_status_porcelain="", worktree_list_porcelain="",
        summary=_COMPLETE,
    )
    keys = {i.key: i.status for i in v.invariants}
    assert keys["I1_worktree_initialized"] == vfi.FAIL
    assert v.overall == vfi.FAIL


def test_incomplete_session_is_incomplete():
    v = vfi.assess_isolation(
        debug_log=_ROUTED, primary_status_porcelain="",
        worktree_list_porcelain="",
        summary={"session_outcome": ""},
    )
    assert v.overall == vfi.INCOMPLETE


def test_worktree_still_listed_warns_i4():
    log = "phase=GENERATE\n" + _ROUTED + "\n" + _QPATH + " ref\n"
    v = vfi.assess_isolation(
        debug_log=log, primary_status_porcelain="",
        worktree_list_porcelain=f"worktree {_QPATH}\nHEAD abc\n",
        summary=_COMPLETE,
    )
    keys = {i.key: i.status for i in v.invariants}
    assert keys["I4_worktree_reaped"] == vfi.WARN
    assert v.overall == vfi.WARN


def test_reaped_via_log_passes_i4_even_if_listed():
    log = "phase=GENERATE\n" + _ROUTED + "\n" + _QPATH + " ref\n"
    v = vfi.assess_isolation(
        debug_log=log, primary_status_porcelain="",
        worktree_list_porcelain=f"worktree {_QPATH}\n",
        summary=_COMPLETE,
        reap_log="[WorktreeManager] reaped ouroboros/auto/bt-2026-06-16-x",
    )
    keys = {i.key: i.status for i in v.invariants}
    assert keys["I4_worktree_reaped"] == vfi.PASS
