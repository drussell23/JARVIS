# tests/governance/test_workspace_resolver_durable.py
from __future__ import annotations

import os
from pathlib import Path

from backend.core.ouroboros.governance.workspace_resolver import resolve_durable_path


def test_identity_when_pair_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_DURABLE_REROOT_FROM", raising=False)
    monkeypatch.delenv("JARVIS_TRINITY_ROOT", raising=False)
    p = Path("/opt/trinity/jarvis/.jarvis/ledger.jsonl")
    assert resolve_durable_path(p) == p  # real node / plain local: byte-identical


def test_reanchors_overlay_path_onto_durable_root(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path / "trinity_root"))
    out = resolve_durable_path(Path("/opt/trinity/jarvis/.jarvis/a1_lineage.jsonl"))
    assert out == tmp_path / "trinity_root" / ".jarvis" / "a1_lineage.jsonl"


def test_non_overlay_path_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path))
    p = Path.cwd() / ".jarvis" / "local.jsonl"
    assert resolve_durable_path(p) == p  # only overlay-rooted paths re-anchor


def test_kill_switch_reverts_to_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path))
    p = Path("/opt/trinity/jarvis/.jarvis/x.jsonl")
    assert resolve_durable_path(p) == p


def test_never_raises_on_garbage(monkeypatch):
    # os.environ.__setitem__ (os.putenv under the hood) rejects an embedded
    # null byte with ValueError at the OS layer -- a cross-platform CPython
    # restriction, not something resolve_durable_path controls. Swap the
    # os.environ mapping itself for a plain dict so the exact garbage
    # payload reaches the function under test unfiltered by that OS-level
    # guard (monkeypatch.setenv would raise before the call ever happens).
    fake_env = dict(os.environ)
    fake_env["JARVIS_DURABLE_REROOT_FROM"] = "\x00bad"
    fake_env["JARVIS_TRINITY_ROOT"] = "also-bad"
    monkeypatch.setattr(os, "environ", fake_env)
    out = resolve_durable_path("/opt/trinity/jarvis/.jarvis/x.jsonl")
    assert isinstance(out, Path)  # fail-soft identity, never raises
