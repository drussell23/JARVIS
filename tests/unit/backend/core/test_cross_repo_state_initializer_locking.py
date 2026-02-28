from __future__ import annotations

from dataclasses import dataclass

import backend.core.cross_repo_state_initializer as crsi


@dataclass
class _DummyLockCtx:
    acquired: bool = True

    async def __aenter__(self):
        return self.acquired

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def test_emit_event_uses_file_lock_fallback_when_dlm_unavailable(monkeypatch):
    initializer = crsi.CrossRepoStateInitializer()

    async def _force_no_dlm(*, reason: str, required: bool = False):
        return False

    writes = {}

    async def _fake_read(_path, default=None):
        return list(default or [])

    async def _fake_write(_path, data):
        writes["data"] = data

    monkeypatch.setattr(initializer, "_ensure_lock_manager_initialized", _force_no_dlm)
    monkeypatch.setattr(initializer, "_read_json_file", _fake_read)
    monkeypatch.setattr(initializer, "_write_json_file", _fake_write)
    monkeypatch.setattr(crsi, "RobustFileLock", lambda *args, **kwargs: _DummyLockCtx(acquired=True))

    event = crsi.VBIAEvent(
        event_type=crsi.EventType.SYSTEM_READY,
        source_repo=crsi.RepoType.JARVIS,
        payload={"test": True},
    )
    await initializer.emit_event(event)

    assert "data" in writes
    assert len(writes["data"]) == 1
    assert writes["data"][0]["event_type"] == crsi.EventType.SYSTEM_READY.value


async def test_ensure_lock_manager_initialized_sets_manager(monkeypatch):
    initializer = crsi.CrossRepoStateInitializer()

    class _FakeManager:
        pass

    fake_manager = _FakeManager()

    async def _fake_get_lock_manager():
        return fake_manager

    monkeypatch.setattr(crsi, "get_lock_manager", _fake_get_lock_manager)

    ok = await initializer._ensure_lock_manager_initialized(reason="test", required=False)
    assert ok is True
    assert initializer._lock_manager is fake_manager

