"""HUD ↔ unified_supervisor local-first bridge spine (Phase 9)."""
from __future__ import annotations

import pytest

from backend.api import hud_local_bridge as br


# ---- loopback trust ----

def test_loopback_hosts_accepted():
    for h in ("127.0.0.1", "::1", "localhost", "", "::ffff:127.0.0.1"):
        assert br.is_loopback_host(h) is True


def test_remote_hosts_rejected():
    for h in ("10.0.0.5", "192.168.1.20", "8.8.8.8", "example.com"):
        assert br.is_loopback_host(h) is False


# ---- token ----

def test_token_response_has_swift_contract_keys():
    r = br.build_stream_token_response({"device_id": "mac-1"})
    assert r["token"].startswith("local-")
    assert r["stream_url"] == "/api/stream/mac-1"      # points at the SSE endpoint
    assert r["mode"] == "local"


def test_token_defaults_device_when_absent():
    r = br.build_stream_token_response({})
    assert r["stream_url"] == "/api/stream/mac-local"


def test_token_never_raises_on_garbage():
    assert br.build_stream_token_response(None)["token"]


# ---- command translation ----

def test_translate_maps_swift_to_ws_command_frame():
    frame = br.translate_hud_command({
        "text": "what's on my screen?",
        "command_id": "cmd-42",
        "device_id": "mac-1",
        "context": {"active_app": "Xcode"},
        "response_mode": "stream",
    })
    assert frame["type"] == "command"                  # routes to command channel
    assert frame["command"] == "what's on my screen?"
    assert frame["text"] == "what's on my screen?"
    assert frame["command_id"] == "cmd-42"
    assert frame["context"] == {"active_app": "Xcode"}
    assert frame["source"] == "hud_local"


def test_translate_accepts_camelcase_defensively():
    frame = br.translate_hud_command({"text": "hi", "commandId": "c1"})
    assert frame["command_id"] == "c1"


def test_translate_never_raises():
    frame = br.translate_hud_command(None)
    assert frame["type"] == "command"


# ---- response shaping ----

def test_response_shape_accepted_by_default():
    r = br.shape_command_response({"success": True}, "cmd-1")
    assert r["status"] == "accepted"
    assert r["command_id"] == "cmd-1"
    assert r["success"] is True


def test_response_shape_reflects_error():
    r = br.shape_command_response({"success": False}, "cmd-2")
    assert r["status"] == "error"
    assert r["success"] is False


def test_response_shape_passes_explicit_status():
    r = br.shape_command_response({"status": "queued"}, "cmd-3")
    assert r["status"] == "queued"
