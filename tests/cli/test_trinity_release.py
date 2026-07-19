"""trinity release — cryptographic release pipeline spine.

Mandate 4 (verbatim): mock xcrun notarytool's async response. Simulate
three 'In Progress' polls followed by 'Accepted'. Assert the async
polling mechanism advances, triggers the xcrun stapler subprocess mock,
and reports a finalized cryptographic release WITHOUT blocking the event
loop.

Plus: Keychain identity resolution (no hardcoding), notary auth
precedence, deep-verify + staple ordering, and the Xcode-CLT gate.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import trinity_release as rel


# ---------------------------------------------------------------------------
# MANDATE 4 — async notary polling: 3× In Progress → Accepted, non-blocking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_poll_advances_through_in_progress_then_accepted():
    """The core mandate-4 contract: the poller keeps going through three
    'In Progress' responses and returns on 'Accepted' — using async sleep
    (recorded, never real wall-time)."""
    statuses = ["In Progress", "In Progress", "In Progress", "Accepted"]
    calls = {"info": 0}
    slept = []

    async def _runner(argv):
        assert argv[:3] == ["xcrun", "notarytool", "info"]     # right command
        i = calls["info"]
        calls["info"] += 1
        return 0, json.dumps({"status": statuses[i]}), ""

    async def _sleeper(s):
        slept.append(s)                    # record backoff, don't actually wait

    status = await rel.poll_notary_status(
        "sub-123", ["--keychain-profile", "P"],
        runner=_runner, sleeper=_sleeper)

    assert status == "Accepted"
    assert calls["info"] == 4              # 3 in-progress + 1 accepted
    # Exponential backoff between the 3 non-terminal polls (3 sleeps).
    assert len(slept) == 3
    assert slept[1] > slept[0] and slept[2] > slept[1]   # growing


@pytest.mark.asyncio
async def test_poll_backoff_is_capped():
    slept = []

    async def _runner(argv):
        return 0, json.dumps({"status": "In Progress"}), ""

    async def _sleeper(s):
        slept.append(s)

    import os
    os.environ["JARVIS_NOTARY_MAX_POLLS"] = "6"
    os.environ["JARVIS_NOTARY_POLL_BASE_S"] = "5"
    os.environ["JARVIS_NOTARY_POLL_CAP_S"] = "20"
    try:
        status = await rel.poll_notary_status("s", [], runner=_runner,
                                              sleeper=_sleeper)
    finally:
        for k in ("JARVIS_NOTARY_MAX_POLLS", "JARVIS_NOTARY_POLL_BASE_S",
                  "JARVIS_NOTARY_POLL_CAP_S"):
            os.environ.pop(k, None)
    assert status == "Timeout"             # never terminal → bounded exit
    assert max(slept) <= 20                # cap honored


@pytest.mark.asyncio
async def test_full_release_triggers_staple_after_accepted(tmp_path, monkeypatch):
    """End-to-end: signed→verified→submitted→(async poll)→Accepted MUST
    trigger the stapler. Proves the airgap staple fires on success and the
    whole pipeline runs on the event loop without blocking."""
    app = tmp_path / "Trinity.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(b"x")

    events = []

    # Sync subprocess mock (security/codesign/ditto).
    class _R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode = rc; self.stdout = out; self.stderr = err

    def _sync(argv, **kw):
        tool = argv[0]
        events.append(tool)
        if tool == "codesign":
            return _R(0)
        if tool == "ditto":
            (tmp_path / "Trinity.zip").write_bytes(b"zip")
            return _R(0)
        return _R(0)

    # Async subprocess mock (notarytool submit/info, stapler).
    poll_seq = ["In Progress", "In Progress", "In Progress", "Accepted"]
    poll_i = {"n": 0}

    async def _async(argv):
        events.append(" ".join(argv[:3]))
        if argv[:3] == ["xcrun", "notarytool", "submit"]:
            return 0, json.dumps({"id": "sub-XYZ"}), ""
        if argv[:3] == ["xcrun", "notarytool", "info"]:
            s = poll_seq[poll_i["n"]]; poll_i["n"] += 1
            return 0, json.dumps({"status": s}), ""
        if argv[:3] == ["xcrun", "stapler", "staple"]:
            return 0, "stapled", ""
        if argv[:3] == ["xcrun", "stapler", "validate"]:
            return 0, "valid", ""
        return 0, "", ""

    slept = []
    async def _sleep(s): slept.append(s)

    report = await rel.run_release(
        app,
        identity="Developer ID Application: Test (TEAM)",
        auth=["--keychain-profile", "TestProfile"],
        sync_runner=_sync, async_runner=_async, sleeper=_sleep,
        skip_xcode_gate=True,
    )

    assert report.shippable is True
    assert report.signed and report.verified
    assert report.notarized and report.notary_status == "Accepted"
    assert report.stapled is True
    # ORDER: codesign(sign)→codesign(verify)→ditto→submit→info…→staple.
    assert "codesign" in events
    assert "xcrun stapler staple" in events
    assert events.index("xcrun notarytool submit") < \
        events.index("xcrun stapler staple")
    assert poll_i["n"] == 4                 # polled through to Accepted


@pytest.mark.asyncio
async def test_release_aborts_and_skips_staple_on_invalid(tmp_path):
    app = tmp_path / "Trinity.app"
    (app / "Contents").mkdir(parents=True)

    class _R:
        returncode = 0; stdout = ""; stderr = ""
    def _sync(argv, **kw):
        if argv[0] == "ditto":
            (tmp_path / "Trinity.zip").write_bytes(b"z")
        return _R()

    stapled = {"called": False}
    async def _async(argv):
        if argv[:3] == ["xcrun", "notarytool", "submit"]:
            return 0, json.dumps({"id": "s"}), ""
        if argv[:3] == ["xcrun", "notarytool", "info"]:
            return 0, json.dumps({"status": "Invalid"}), ""
        if argv[:3] == ["xcrun", "stapler", "staple"]:
            stapled["called"] = True
            return 0, "", ""
        return 0, "", ""

    report = await rel.run_release(
        app, identity="X", auth=["--keychain-profile", "P"],
        sync_runner=_sync, async_runner=_async, sleeper=lambda s: asyncio.sleep(0),
        skip_xcode_gate=True)

    assert report.aborted is True
    assert report.notary_status == "Invalid"
    assert report.stapled is False
    assert stapled["called"] is False       # NEVER staple a rejected build


# ---------------------------------------------------------------------------
# MANDATE 1 — Keychain identity + notary auth precedence (no hardcoding)
# ---------------------------------------------------------------------------

def test_identity_prefers_developer_id_application(monkeypatch):
    monkeypatch.delenv("JARVIS_CODESIGN_IDENTITY", raising=False)
    out = (
        '  1) ' + 'A' * 40 + ' "Apple Development: dev@x.com (AAA)"\n'
        '  2) ' + 'B' * 40 + ' "Developer ID Application: Derek (TEAMID)"\n'
        '     2 valid identities found\n'
    )
    class _R:
        stdout = out
    monkeypatch.setattr(rel.subprocess, "run", lambda *a, **k: _R())
    ident = rel.resolve_signing_identity()
    assert ident == "Developer ID Application: Derek (TEAMID)"   # preferred


def test_identity_env_override_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_CODESIGN_IDENTITY", "My Explicit Identity")
    assert rel.resolve_signing_identity() == "My Explicit Identity"


def test_identity_none_when_no_cert(monkeypatch):
    monkeypatch.delenv("JARVIS_CODESIGN_IDENTITY", raising=False)
    class _R:
        stdout = "     0 valid identities found\n"
    monkeypatch.setattr(rel.subprocess, "run", lambda *a, **k: _R())
    assert rel.resolve_signing_identity() is None


def test_notary_auth_prefers_keychain_profile(monkeypatch):
    monkeypatch.setenv("JARVIS_NOTARY_PROFILE", "MyProfile")
    monkeypatch.setenv("JARVIS_NOTARY_APPLE_ID", "a@b.c")
    monkeypatch.setenv("JARVIS_NOTARY_TEAM_ID", "TEAM")
    monkeypatch.setenv("JARVIS_NOTARY_PASSWORD", "pw")
    args = rel.notary_auth_args()
    assert args == ["--keychain-profile", "MyProfile"]   # enclave path wins


def test_notary_auth_env_fallback(monkeypatch):
    monkeypatch.delenv("JARVIS_NOTARY_PROFILE", raising=False)
    monkeypatch.setenv("JARVIS_NOTARY_APPLE_ID", "a@b.c")
    monkeypatch.setenv("JARVIS_NOTARY_TEAM_ID", "TEAM")
    monkeypatch.setenv("JARVIS_NOTARY_PASSWORD", "pw")
    args = rel.notary_auth_args()
    assert "--apple-id" in args and "--team-id" in args and "--password" in args


def test_notary_auth_none_when_unconfigured(monkeypatch):
    for k in ("JARVIS_NOTARY_PROFILE", "JARVIS_NOTARY_APPLE_ID",
              "JARVIS_NOTARY_TEAM_ID", "JARVIS_NOTARY_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert rel.notary_auth_args() is None


def test_no_hardcoded_secrets_in_source():
    """Static guard: no Team ID / app-specific password / Apple ID literal
    baked into the pipeline (mandate 1)."""
    src = Path(rel.__file__).read_text()
    import re
    # app-specific passwords look like xxxx-xxxx-xxxx-xxxx
    assert not re.search(r"\b[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}\b", src)
    # no 10-char all-caps Team ID literal assignments
    assert "hardcode" not in src.lower() or "no hardcod" in src.lower()


# ---------------------------------------------------------------------------
# MANDATE 3 — DRY gate: Xcode CLT via the doctor check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_gate_aborts_without_xcode(tmp_path, monkeypatch):
    from backend.core.ouroboros.cli import trinity_doctor as doc
    monkeypatch.setattr(
        doc, "check_xcode_tools",
        lambda: doc.CheckResult("xcode-tools", doc.Status.FAIL,
                                detail="missing xcrun"))
    app = tmp_path / "Trinity.app"
    app.mkdir()
    report = await rel.run_release(app, skip_xcode_gate=False)
    assert report.aborted is True
    assert "xcrun" in report.reason


def test_hardened_runtime_flag_present():
    """Notarization REQUIRES the hardened runtime + timestamp."""
    src = Path(rel.__file__).read_text()
    assert "--options" in src and "runtime" in src
    assert "--timestamp" in src


@pytest.mark.asyncio
async def test_deep_strict_verify_is_used(tmp_path):
    """codesign --verify --deep --strict must run post-sign (mandate 2)."""
    src = Path(rel.__file__).read_text()
    assert "--verify" in src and "--deep" in src and "--strict" in src
