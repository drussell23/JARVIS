"""Ephemeral IAM lifecycle orchestration -- create -> bind -> build -> GUARANTEED
teardown. The temp SA MUST be deleted whether the build succeeds OR fails, and the
WAL must record GOLDEN IMAGE READY / failure + the teardown.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.cloud_build_baker import CloudBuildBaker


def _baker(tmp_path):
    spec = tmp_path / "x.pkr.hcl"
    spec.write_text('source "googlecompute" "x" {}\n')
    wal = tmp_path / "wal.log"
    b = CloudBuildBaker(spec_path=str(spec), project="proj", image_family="fam")
    b._iam_settle_s = 0
    return b, wal


async def _wire(monkeypatch, b, *, calls, poll_status="SUCCESS", sa="baker@proj.iam.gserviceaccount.com"):
    async def auth():
        return "tok", "proj"
    async def create_sa(project, token, account_id):
        calls.append("create_sa"); return sa
    async def bind(project, token, member, roles):
        calls.append("bind"); return True
    async def submit():
        calls.append("submit"); return "build-123"
    async def poll(build_id, **kw):
        calls.append("poll"); return poll_status
    async def unbind(project, token, member):
        calls.append("unbind"); return True
    async def delete_sa(project, token, email):
        calls.append("delete_sa"); return True
    monkeypatch.setattr(b, "_auth", auth)
    monkeypatch.setattr(b, "_create_temp_sa", create_sa)
    monkeypatch.setattr(b, "_bind_roles", bind)
    monkeypatch.setattr(b, "submit", submit)
    monkeypatch.setattr(b, "poll", poll)
    monkeypatch.setattr(b, "_unbind_member", unbind)
    monkeypatch.setattr(b, "_delete_temp_sa", delete_sa)


async def test_full_lifecycle_success(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BAKE_WAL", str(tmp_path / "wal.log"))
    b, wal = _baker(tmp_path)
    calls = []
    await _wire(monkeypatch, b, calls=calls)
    ok = await b.bake_with_ephemeral_iam()
    assert ok is True
    # Order: create SA -> bind -> submit -> poll -> (teardown) unbind -> delete.
    assert calls == ["create_sa", "bind", "submit", "poll", "unbind", "delete_sa"]
    wal_text = wal.read_text()
    assert "GOLDEN IMAGE READY" in wal_text
    assert "EPHEMERAL SA TORN DOWN" in wal_text


async def test_teardown_runs_even_on_build_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BAKE_WAL", str(tmp_path / "wal.log"))
    b, wal = _baker(tmp_path)
    calls = []
    await _wire(monkeypatch, b, calls=calls, poll_status="FAILURE")
    ok = await b.bake_with_ephemeral_iam()
    assert ok is False
    assert "delete_sa" in calls          # SA STILL deleted on failure
    assert "BAKE FAILED" in wal.read_text()
    assert "EPHEMERAL SA TORN DOWN" in wal.read_text()


async def test_teardown_runs_even_if_poll_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BAKE_WAL", str(tmp_path / "wal.log"))
    b, wal = _baker(tmp_path)
    calls = []
    await _wire(monkeypatch, b, calls=calls)

    async def boom(build_id, **kw):
        calls.append("poll"); raise RuntimeError("network died mid-poll")
    monkeypatch.setattr(b, "poll", boom)

    with pytest.raises(RuntimeError):
        await b.bake_with_ephemeral_iam()
    assert "delete_sa" in calls           # finally still tore the SA down
    assert "EPHEMERAL SA TORN DOWN" in wal.read_text()


async def test_sa_create_denied_aborts_no_teardown(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BAKE_WAL", str(tmp_path / "wal.log"))
    b, wal = _baker(tmp_path)
    calls = []
    await _wire(monkeypatch, b, calls=calls, sa=None)  # create returns None (denied)
    ok = await b.bake_with_ephemeral_iam()
    assert ok is False
    assert "delete_sa" not in calls       # nothing to tear down (none created)
    assert "BAKE ABORT" in wal.read_text()
