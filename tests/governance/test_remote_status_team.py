"""Tests for RemoteStatusAPI._handle_team — Tier-2 batch 1 row 25.

``GET /team`` enumerates ``~/.jarvis/ouroboros/teams`` synchronously
via ``Path.iterdir()``. Since this handler runs on the shared aiohttp
event loop, an external request could stall it. This spine pins:

  (a) the dir-scan routes through the ``cooperative_fs_io`` substrate
      (spy on ``offload``);
  (b) correctness parity — the handler still returns the same team
      names / JSON shape as the pre-offload synchronous behavior;
  (c) fail-soft — an ``OffloadError`` degrades to ``{"teams": []}``
      instead of raising into the HTTP response.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.remote_status import RemoteStatusAPI


class _FakeCoordinator:
    """Decouples the test from AgentTeamCoordinator's real disk
    persistence — only ``_handle_team``'s dir-scan is under test."""

    def __init__(self, name: str) -> None:
        self._name = name

    def get_progress(self) -> Dict[str, Any]:
        return {"team_name": self._name}


def _body(resp: Any) -> Dict[str, Any]:
    return json.loads(resp.text or "{}")


@pytest.fixture
def fake_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(
        pathlib.Path, "home", classmethod(lambda cls: tmp_path),
    )
    return tmp_path


async def test_handle_team_routes_iterdir_through_offload(fake_home):
    """(a) Spy: the teams-dir scan must route through offload()."""
    import backend.core.ouroboros.governance.cooperative_fs_io as fsio

    teams_dir = fake_home / ".jarvis" / "ouroboros" / "teams"
    teams_dir.mkdir(parents=True)
    (teams_dir / "team-a").mkdir()
    (teams_dir / "team-b").mkdir()

    calls = []
    real_offload = fsio.offload

    async def _spy_offload(fn, *a, **k):
        calls.append(1)
        return await real_offload(fn, *a, **k)

    with (
        patch.object(fsio, "offload", _spy_offload),
        patch(
            "backend.core.ouroboros.governance.agent_team.AgentTeamCoordinator",
            _FakeCoordinator,
        ),
    ):
        api = RemoteStatusAPI()
        resp = await api._handle_team(None)

    assert calls, "_handle_team did not route the iterdir through offload()"
    body = _body(resp)
    assert {t["team_name"] for t in body["teams"]} == {"team-a", "team-b"}


async def test_handle_team_parity_ignores_non_dirs(fake_home):
    """(b) Correctness parity: only directories are listed as teams —
    same filter the pre-offload synchronous ``iterdir`` used."""
    teams_dir = fake_home / ".jarvis" / "ouroboros" / "teams"
    teams_dir.mkdir(parents=True)
    (teams_dir / "team-only").mkdir()
    (teams_dir / "stray_file.txt").write_text("not a team")

    with patch(
        "backend.core.ouroboros.governance.agent_team.AgentTeamCoordinator",
        _FakeCoordinator,
    ):
        api = RemoteStatusAPI()
        resp = await api._handle_team(None)

    body = _body(resp)
    assert {t["team_name"] for t in body["teams"]} == {"team-only"}


async def test_handle_team_missing_teams_dir_returns_empty(fake_home):
    """Parity: no teams dir on disk → ``{"teams": []}`` (unchanged
    from the pre-offload behavior's ``if teams_dir.exists()`` guard)."""
    api = RemoteStatusAPI()
    resp = await api._handle_team(None)
    body = _body(resp)
    assert body == {"teams": []}


async def test_handle_team_fail_soft_on_offload_error(fake_home):
    """(c) Fail-soft: an OffloadError degrades to an empty team list
    instead of raising into the HTTP response."""
    import backend.core.ouroboros.governance.cooperative_fs_io as fsio

    teams_dir = fake_home / ".jarvis" / "ouroboros" / "teams"
    teams_dir.mkdir(parents=True)
    (teams_dir / "team-a").mkdir()

    async def _boom_offload(fn, *a, **k):
        return fsio.OffloadError(
            fn_name="iterdir", exc_type="OSError",
            message="simulated", cpu_bound=False,
        )

    with patch.object(fsio, "offload", _boom_offload):
        api = RemoteStatusAPI()
        resp = await api._handle_team(None)

    body = _body(resp)
    assert body == {"teams": []}
