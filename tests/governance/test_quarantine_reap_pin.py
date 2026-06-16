"""Sovereign Execution Boundary (Stage A) — Phase 3 reaping pin.

The autonomous loop's quarantine worktrees (``ouroboros/auto/bt-*``) must be
flushed on boot so orphaned zones from a crashed/killed prior session can't
accumulate. The boot path (``governed_loop_service`` → ``reap_orphans()``)
already resolves a MULTI-PREFIX reap set via ``_resolve_reap_prefixes``.

This pin LOCKS that guarantee against future drift (no new code — the wiring
exists; this guards it). If a refactor drops the autonomous prefix from the
reap set, this fails loudly.
"""
from __future__ import annotations

from backend.core.ouroboros.governance import worktree_manager as wm


def test_default_reap_set_includes_autonomous_quarantine_prefix(monkeypatch):
    monkeypatch.delenv("JARVIS_WORKTREE_REAP_PREFIXES", raising=False)
    prefixes = wm._resolve_reap_prefixes("unit-")
    # The boot reaper's primary prefix plus the autonomous quarantine zones.
    assert "unit-" in prefixes
    assert "ouroboros/auto/bt-" in prefixes  # branch-form quarantine zone
    assert "ouroboros__auto__bt-" in prefixes  # on-disk dir form


def test_reap_prefixes_env_override_respected(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_WORKTREE_REAP_PREFIXES", "unit-, auto-loop-",
    )
    prefixes = wm._resolve_reap_prefixes("unit-")
    assert prefixes[0] == "unit-"
    assert "auto-loop-" in prefixes


def test_reap_prefixes_dedupes_and_preserves_order(monkeypatch):
    monkeypatch.setenv("JARVIS_WORKTREE_REAP_PREFIXES", "unit-,unit-,x-")
    prefixes = wm._resolve_reap_prefixes("unit-")
    assert prefixes == ("unit-", "x-")


def test_boot_path_calls_reap_orphans():
    # Characterization pin: the GovernedLoopService boot path invokes the
    # reaper (gated by JARVIS_WORKTREE_REAP_ORPHANS, default true). Guards
    # against a refactor silently removing the boot-time flush.
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = inspect.getsource(gls)
    assert "reap_orphans(" in src
    assert "JARVIS_WORKTREE_REAP_ORPHANS" in src
