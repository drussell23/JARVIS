"""Sovereign Ephemeral Self-Termination Matrix tests (2026-06-21).

The crucible node self-deletes the instant a graduation PR opens — gated, idempotent,
fail-soft, fires only on a genuine pr_url."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import backend.core.ouroboros.governance.sovereign_self_termination as ST


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ST._fired = False
    monkeypatch.delenv("JARVIS_SOVEREIGN_SELF_TERMINATE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_SOVEREIGN_SELF_TERMINATE_GRACE_S", "0")


def test_disabled_by_default():
    assert ST.self_terminate_enabled() is False


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_SOVEREIGN_SELF_TERMINATE_ENABLED", "true")
    assert ST.self_terminate_enabled() is True


def test_trigger_noop_when_disabled():
    assert ST.trigger_self_termination("https://github.com/x/pr/1") is False


def test_trigger_noop_without_pr_url(monkeypatch):
    monkeypatch.setenv("JARVIS_SOVEREIGN_SELF_TERMINATE_ENABLED", "true")
    assert ST.trigger_self_termination("") is False


def test_sever_off_gce_is_failsoft(monkeypatch):
    # off-GCE (no metadata server) → returns False, never raises
    monkeypatch.setattr(ST, "_metadata", lambda path: None)
    assert ST.sever_compute() is False


def test_mark_terminal_success_writes_sentinel(monkeypatch):
    p = Path(tempfile.mktemp())
    monkeypatch.setenv("JARVIS_SOVEREIGN_TERMINAL_SENTINEL", str(p))
    ST.mark_terminal_success("https://github.com/x/pr/42")
    payload = json.loads(p.read_text())
    assert payload["state"] == "TERMINAL_SUCCESS"
    assert payload["pr_url"] == "https://github.com/x/pr/42"


def test_trigger_full_sequence_and_idempotent(monkeypatch):
    monkeypatch.setenv("JARVIS_SOVEREIGN_SELF_TERMINATE_ENABLED", "true")
    p = Path(tempfile.mktemp())
    monkeypatch.setenv("JARVIS_SOVEREIGN_TERMINAL_SENTINEL", str(p))
    calls = {"flush": 0, "sever": 0}
    monkeypatch.setattr(ST, "flush_state_vault", lambda: calls.__setitem__("flush", calls["flush"] + 1) or True)
    monkeypatch.setattr(ST, "sever_compute", lambda: calls.__setitem__("sever", calls["sever"] + 1) or True)
    # first success fires the full sequence
    assert ST.trigger_self_termination("https://github.com/x/pr/7") is True
    assert calls == {"flush": 1, "sever": 1}
    assert json.loads(p.read_text())["pr_url"] == "https://github.com/x/pr/7"
    # second success is a no-op (idempotent — can't double-fire)
    assert ST.trigger_self_termination("https://github.com/x/pr/8") is False
    assert calls == {"flush": 1, "sever": 1}


def test_self_delete_url_shape(monkeypatch):
    """sever_compute builds the correct Compute REST DELETE against the SELF instance."""
    monkeypatch.setattr(ST, "_instance_identity", lambda: ("proj", "us-central1-a", "node-x"))
    monkeypatch.setattr(ST, "_sa_token", lambda: "tok")
    captured = {}
    class _Resp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def _fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        return _Resp()
    monkeypatch.setattr(ST.urllib.request, "urlopen", _fake_urlopen)
    ok = ST.sever_compute()
    assert ok is True
    assert captured["url"] == (
        "https://compute.googleapis.com/compute/v1/projects/proj/zones/us-central1-a/instances/node-x"
    )
    assert captured["method"] == "DELETE"
    assert captured["auth"] == "Bearer tok"
