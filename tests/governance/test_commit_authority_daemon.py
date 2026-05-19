"""Slice 4 #1 spine — commit-authority Unix-socket daemon.

Real socket lifecycle (short path for the macOS AF_UNIX 104-byte
sun_path limit), filesystem-permission enforcement (0o600),
refresh→valid-presence composition, malformed-input rejection,
NEVER-raises, closed verb taxonomy, AST pin self-validate.
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    commit_authority_daemon as d,
)
from backend.core.ouroboros.governance import (
    operator_commit_authority as oca,
)


def _short_sock() -> str:
    # macOS sun_path max ~104 bytes; pytest tmp_path is too long.
    return f"/tmp/ocad_{os.getpid()}_{uuid.uuid4().hex[:8]}.sock"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        str(tmp_path / "secret"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_PRESENCE_FILE",
        str(tmp_path / "presence.json"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        str(tmp_path / "grants.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true",
    )
    monkeypatch.delenv("JARVIS_COMMIT_CHANNEL", raising=False)
    yield


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED", raising=False,
    )
    assert d.daemon_enabled() is False


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 5.0), ("", 5.0), ("x", 5.0),
     ("0.1", 0.5), ("999", 30.0), ("8", 8.0)],
)
def test_conn_timeout_clamp(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(
            "JARVIS_COMMIT_AUTHORITY_DAEMON_TIMEOUT_S",
            raising=False,
        )
    else:
        monkeypatch.setenv(
            "JARVIS_COMMIT_AUTHORITY_DAEMON_TIMEOUT_S", raw,
        )
    assert d.conn_timeout_s() == expected


def test_socket_path_env_override(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SOCK", "/tmp/x/y.sock",
    )
    assert d.socket_path() == Path("/tmp/x/y.sock")


def test_serve_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED", "false",
    )
    assert asyncio.run(d.serve()) is None


# --------------------------------------------------------------------------
# handle_request — pure composition
# --------------------------------------------------------------------------


def test_unknown_verb_rejected():
    r = d.handle_request({"verb": "exec", "repo_root": "/r"})
    assert r["ok"] is False and "closed set" in r["error"]


def test_missing_repo_root():
    r = d.handle_request({"verb": "status"})
    assert r["ok"] is False and "repo_root required" in r["error"]


def test_status_composes(tmp_path):
    r = d.handle_request(
        {"verb": "status", "repo_root": str(tmp_path),
         "branch": "feat"}
    )
    assert r["ok"] is True and r["verb"] == "status"
    assert "master_enabled" in r and "dry_verdict" in r
    assert "resolved_channel" in r


def test_refresh_issues_grant_and_mints_presence(tmp_path):
    r = d.handle_request(
        {"verb": "refresh", "repo_root": str(tmp_path),
         "branch": "feat", "minutes": 30}
    )
    assert r["ok"] is True, r
    assert r["channel"] == "ide" and r["branch"] == "feat"
    assert r["grant_id"] and r["expires_at_unix"] > 0
    # Composition proof: presence is now valid for that repo+branch.
    assert oca.valid_operator_presence(tmp_path, "feat") is True


def test_refresh_empty_branch_refused(tmp_path):
    # No branch + a non-git tmp dir → cannot resolve → refuse
    # (no empty whole-repo grant).
    r = d.handle_request(
        {"verb": "refresh", "repo_root": str(tmp_path)}
    )
    assert r["ok"] is False
    assert "empty whole-repo grant" in r["error"]


def test_refresh_bad_minutes(tmp_path):
    r = d.handle_request(
        {"verb": "refresh", "repo_root": str(tmp_path),
         "branch": "b", "minutes": "lots"}
    )
    assert r["ok"] is False and "bad minutes" in r["error"]


# --------------------------------------------------------------------------
# Real socket lifecycle + perms + protocol robustness
# --------------------------------------------------------------------------


async def _roundtrip(sock: str, line: str) -> dict:
    reader, writer = await asyncio.open_unix_connection(sock)
    writer.write((line + "\n").encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    return json.loads(raw.decode())


async def test_socket_serve_perms_and_refresh(monkeypatch, tmp_path):
    sock = _short_sock()
    monkeypatch.setenv("JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COMMIT_AUTHORITY_SOCK", sock)
    server = await d.serve()
    assert server is not None
    try:
        # Socket inode locked to owning uid (0o600).
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        assert mode == 0o600, oct(mode)
        resp = await _roundtrip(
            sock,
            json.dumps({"verb": "refresh",
                        "repo_root": str(tmp_path),
                        "branch": "feat"}),
        )
        assert resp["ok"] is True and resp["channel"] == "ide"
        assert oca.valid_operator_presence(tmp_path, "feat")
        # malformed JSON → structured error, server survives.
        bad = await _roundtrip(sock, "{not json")
        assert bad["ok"] is False
        # server still alive after a bad client.
        ok2 = await _roundtrip(
            sock,
            json.dumps({"verb": "status",
                        "repo_root": str(tmp_path),
                        "branch": "feat"}),
        )
        assert ok2["ok"] is True
    finally:
        await d.shutdown(server)
    assert not Path(sock).exists()  # unlinked on shutdown


async def test_oversized_request_rejected(monkeypatch, tmp_path):
    sock = _short_sock()
    monkeypatch.setenv("JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COMMIT_AUTHORITY_SOCK", sock)
    server = await d.serve()
    try:
        huge = json.dumps(
            {"verb": "status", "repo_root": "x",
             "pad": "A" * 9000}
        )
        resp = await _roundtrip(sock, huge)
        assert resp["ok"] is False and "too large" in resp["error"]
    finally:
        await d.shutdown(server)


async def test_live_socket_not_clobbered(monkeypatch, tmp_path):
    sock = _short_sock()
    monkeypatch.setenv("JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COMMIT_AUTHORITY_SOCK", sock)
    s1 = await d.serve()
    try:
        # A second serve() must NOT rebind over a live socket.
        s2 = await d.serve()
        assert s2 is None
    finally:
        await d.shutdown(s1)


def test_shutdown_idempotent_and_never_raises():
    asyncio.run(d.shutdown(None))  # no server → no raise


# --------------------------------------------------------------------------
# AST pin + flags
# --------------------------------------------------------------------------


def test_shipped_invariant_self_validates_green():
    invs = d.register_shipped_invariants()
    assert len(invs) == 1
    src = Path(d.__file__).read_text(encoding="utf-8")
    assert invs[0].validate(ast.parse(src), src) == ()


def test_register_flags_three_seeds():
    seen = []

    class _R:
        def register(self, s):
            seen.append(s.name)

    assert d.register_flags(_R()) == 3
    assert "JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED" in seen
