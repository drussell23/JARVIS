"""trinity doctor — environmental self-validation spine.

Mandate 4 (verbatim): simulate an orphaned UDS left by a terminated
process; assert the doctor identifies the socket as DEAD, unlinks it
WITHOUT raising, and flags the UDS environment as Healthy/Ready.

Plus the surrounding contract: root-cause PID identification, config-
aware gating, and zero-load model verification (no torch import).
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import trinity_doctor as doc


def _short_sock(name: str) -> Path:
    """A socket path short enough for macOS's 104-char AF_UNIX limit
    (pytest's tmp_path is far too long to bind under)."""
    d = tempfile.mkdtemp(prefix="jd_", dir="/tmp")
    return Path(d) / name


# ---------------------------------------------------------------------------
# MANDATE 4 — the orphaned-UDS remediation contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orphaned_uds_is_identified_dead_unlinked_and_ready(
    tmp_path, monkeypatch,
):
    """A SIGKILL'd daemon leaves a .sock inode with NOTHING listening.
    trinity doctor must: (1) classify it dead via a real connect,
    (2) unlink it without raising, (3) report READY."""
    ghost = _short_sock("cockpit_attach.sock")
    # A SIGKILL'd daemon leaves the inode with NO listener accepting on it.
    # We bind+close to create a genuine AF_UNIX socket inode, then (if the
    # platform reaped it on close) recreate the orphan file — either way the
    # end state is exactly the mandate's condition: inode present, connect
    # refused.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(ghost))
    s.close()
    if not ghost.exists():
        ghost.write_bytes(b"")            # a stale inode with no listener
    assert ghost.exists()                 # precondition: orphan present

    # Point the doctor's socket contract at our ghost (reuse the real
    # thin_client primitives — DRY).
    monkeypatch.setattr(
        "backend.core.ouroboros.battle_test.cockpit_attach.attach_socket_path",
        lambda: ghost, raising=False,
    )

    result = await doc.check_attach_socket()      # MUST NOT raise

    assert result.status is doc.Status.READY      # Healthy/Ready
    assert not ghost.exists()                     # unlinked
    assert result.remediation                     # it recorded the fix
    assert "unlink" in result.remediation.lower()


@pytest.mark.asyncio
async def test_absent_socket_is_ready_no_remediation(tmp_path, monkeypatch):
    missing = tmp_path / "never_existed.sock"
    monkeypatch.setattr(
        "backend.core.ouroboros.battle_test.cockpit_attach.attach_socket_path",
        lambda: missing, raising=False,
    )
    r = await doc.check_attach_socket()
    assert r.status is doc.Status.READY
    assert not r.remediation                       # nothing to fix


@pytest.mark.asyncio
async def test_live_socket_is_left_untouched(tmp_path, monkeypatch):
    """A LIVE daemon socket must NOT be unlinked — only dead ghosts are."""
    live = _short_sock("live.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(live))
    server.listen(1)                               # a real listener is home
    try:
        monkeypatch.setattr(
            "backend.core.ouroboros.battle_test.cockpit_attach.attach_socket_path",
            lambda: live, raising=False,
        )
        r = await doc.check_attach_socket()
        assert r.status is doc.Status.READY
        assert live.exists()                       # NOT unlinked
        assert not r.remediation
    finally:
        server.close()
        live.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# MANDATE 1 — root-cause PID identification (not a bind-probe)
# ---------------------------------------------------------------------------

def test_find_port_holder_names_the_real_pid():
    """A live listener must be resolved to THIS process's real PID."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        holder = doc.find_port_holder(port)
        # lsof (or psutil fallback) must find US holding the port.
        assert holder is not None
        assert holder.pid == __import__("os").getpid()
    finally:
        srv.close()


def test_find_port_holder_none_when_free():
    # An unbound ephemeral port has no holder.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()                                      # release it
    assert doc.find_port_holder(free_port) is None


# ---------------------------------------------------------------------------
# MANDATE 2 — config-aware gating + zero-load model verification
# ---------------------------------------------------------------------------

def test_disabled_subsystem_is_skipped_not_failed(monkeypatch):
    monkeypatch.delenv("JARVIS_AUDIO_BUS_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_VISION_LOOP_ENABLED", raising=False)
    results = doc.check_subsystem_models()
    assert all(r.status is doc.Status.SKIPPED for r in results)


def test_enabled_voice_missing_model_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_AUDIO_BUS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SPEAKER_DB", str(tmp_path / "nope.db"))
    results = doc.check_subsystem_models()
    voice = [r for r in results if r.name.startswith("model-voice")]
    assert voice and voice[0].status is doc.Status.FAIL


def test_enabled_voice_present_model_ready_without_torch(monkeypatch, tmp_path):
    db = tmp_path / "speaker.db"
    db.write_bytes(b"x" * 4096)                     # > min_bytes
    monkeypatch.setenv("JARVIS_AUDIO_BUS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SPEAKER_DB", str(db))
    results = doc.check_subsystem_models()
    voice = [r for r in results if r.name.startswith("model-voice")]
    assert voice and voice[0].status is doc.Status.READY
    assert "hdr=" in voice[0].detail                # lightweight checksum ran
    # ZERO-LOAD invariant: verifying a model must NOT import torch.
    assert "torch" not in sys.modules


def test_zero_load_source_never_imports_torch():
    """Static guard: the doctor module must contain no torch IMPORT
    statement (prose mentioning the ban is fine — that's the point)."""
    src = Path(doc.__file__).read_text()
    import re
    assert not re.search(r"^\s*import\s+torch", src, re.MULTILINE)
    assert not re.search(r"^\s*from\s+torch", src, re.MULTILINE)


def test_placeholder_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-your-key-here")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "real-looking-key-abc123")
    results = doc.check_provider_keys()
    anthropic = [r for r in results if r.name == "provider-anthropic"][0]
    dw = [r for r in results if r.name == "provider-doubleword"][0]
    assert anthropic.status is doc.Status.FAIL      # placeholder = missing
    assert dw.status is doc.Status.READY


def test_missing_dw_key_is_warn_not_fail(monkeypatch):
    """DoubleWord degrades to Claude — its absence must not block boot."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-000")
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    results = doc.check_provider_keys()
    dw = [r for r in results if r.name == "provider-doubleword"][0]
    assert dw.status is doc.Status.WARN


def test_redis_skipped_when_disabled(monkeypatch):
    monkeypatch.delenv("REDIS_ENABLED", raising=False)
    r = doc.check_redis()
    assert r.status is doc.Status.SKIPPED


# ---------------------------------------------------------------------------
# Orchestration + exit contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_doctor_never_raises_and_reports(monkeypatch):
    report = await doc.run_doctor()
    assert report.results                          # produced checks
    assert isinstance(report.ok, bool)


def test_doctor_ok_property_reflects_fails():
    rep = doc.DoctorReport()
    rep.add(doc.CheckResult("a", doc.Status.READY))
    assert rep.ok is True
    rep.add(doc.CheckResult("b", doc.Status.FAIL))
    assert rep.ok is False


def test_doctor_main_exit_code(monkeypatch):
    class _C:
        def print(self, *a, **k): pass
    # Force a clean report → rc 0
    async def _clean():
        r = doc.DoctorReport()
        r.add(doc.CheckResult("x", doc.Status.READY))
        return r
    monkeypatch.setattr(doc, "run_doctor", _clean)
    assert doc.doctor_main(_C()) == 0
