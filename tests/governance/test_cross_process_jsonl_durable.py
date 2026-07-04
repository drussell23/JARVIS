"""Task 2 (durability-substrate fix) -- proves the 4 CrossProcessJSONL
public entry points route through ``resolve_durable_path`` before any
``Path(path)`` use, so an overlay-rooted writer lands in the durable
root instead of silently returning ``written=False`` (the op-944c /
bt-iso-1783130209 failure shape)."""
from __future__ import annotations

import json
from pathlib import Path

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_append_lines,
)


def test_append_reanchors_overlay_path(monkeypatch, tmp_path):
    """The bt-iso-1783130209 failure shape: a writer targets the overlay
    root; with the pair declared, the write LANDS in the durable root."""
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path / "troot"))
    ok = flock_append_line(
        "/opt/trinity/jarvis/.jarvis/test_ledger.jsonl",
        json.dumps({"op": "x", "state": "applied"}),
    )
    assert ok is True  # written=True -- the 944c failure mode killed
    landed = tmp_path / "troot" / ".jarvis" / "test_ledger.jsonl"
    assert landed.exists()
    assert json.loads(landed.read_text().splitlines()[0])["state"] == "applied"
    # the flock lock co-locates with the resolved target, never the overlay
    lock_path = landed.with_suffix(landed.suffix + ".lock")
    assert lock_path.exists()
    assert not Path("/opt/trinity").exists()  # nothing touched the overlay root


def test_append_lines_reanchors_and_batches(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path / "troot"))
    ok = flock_append_lines(
        "/opt/trinity/jarvis/.jarvis/batch.jsonl", ["{}", "{}"],
    )
    assert ok is True
    landed = tmp_path / "troot" / ".jarvis" / "batch.jsonl"
    assert len(landed.read_text().splitlines()) == 2
    lock_path = landed.with_suffix(landed.suffix + ".lock")
    assert lock_path.exists()
    assert not Path("/opt/trinity").exists()


def test_identity_when_pair_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_DURABLE_REROOT_FROM", raising=False)
    monkeypatch.delenv("JARVIS_TRINITY_ROOT", raising=False)
    target = tmp_path / "plain.jsonl"
    assert flock_append_line(str(target), "{}") is True
    assert target.exists()  # legacy path byte-identical
