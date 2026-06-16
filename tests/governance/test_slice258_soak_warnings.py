"""Slice 258 — soak-warning fixes.

Covers the behaviour changes that quiet a wave of battle-test warnings while
fixing their root causes:

  §1  SemanticTriageEngine.verify_model retries on transient 401/403 (the
      /v1/models probe outracing the Aegis credential proxy at boot) and
      genuinely verifies once creds land — instead of logging a spurious
      warning and proceeding unverified.
  §2  git_momentum.compute_recent_momentum_async runs the git subprocess on a
      worker thread (the fork never blocks the event loop) — and
      StrategicDirectionService._extract_git_themes_async does too.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.semantic_triage import SemanticTriageEngine
import backend.core.ouroboros.governance.semantic_triage as st
from backend.core.ouroboros.governance.git_momentum import (
    compute_recent_momentum_async,
)
from backend.core.ouroboros.governance.strategic_direction import (
    StrategicDirectionService,
)


# ── fakes for the DW provider + aiohttp session ─────────────────────────
class _FakeResp:
    def __init__(self, status: int, body=None):
        self.status = status
        self._body = body if body is not None else {"data": []}

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _FakeSession:
    """Returns the i-th status from ``statuses`` on the i-th GET."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls = 0

    def get(self, url, timeout=None):
        i = self.calls
        self.calls += 1
        status = self._statuses[min(i, len(self._statuses) - 1)]
        body = {"data": [{"id": "triage-model"}]} if status == 200 else {}
        return _FakeResp(status, body)


class _FakeDW:
    def __init__(self, session, model="dw-default"):
        self._sess = session
        self._base_url = "http://fake/v1"
        self._model = model
        self.is_available = True

    async def _get_session(self):
        return self._sess

    def _request_timeout(self):
        return 5.0


def _engine(session) -> SemanticTriageEngine:
    eng = SemanticTriageEngine(dw_provider=_FakeDW(session), project_root=Path("."))
    eng._effective_model = "triage-model"
    return eng


# ── §1 verify_model retry ───────────────────────────────────────────────
def test_verify_model_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(st, "_TRIAGE_VERIFY_BACKOFF_S", 0.0)
    monkeypatch.setattr(st, "_TRIAGE_VERIFY_MAX_ATTEMPTS", 4)
    sess = _FakeSession([401, 401, 200])  # proxy warms up on the 3rd probe
    eng = _engine(sess)
    ok = asyncio.run(eng.verify_model())
    assert ok is True
    assert eng._model_verified is True
    assert sess.calls == 3  # retried twice, verified on the third


def test_verify_model_exhausts_then_proceeds_optimistically(monkeypatch):
    monkeypatch.setattr(st, "_TRIAGE_VERIFY_BACKOFF_S", 0.0)
    monkeypatch.setattr(st, "_TRIAGE_VERIFY_MAX_ATTEMPTS", 3)
    sess = _FakeSession([401, 401, 401, 401])  # never recovers
    eng = _engine(sess)
    ok = asyncio.run(eng.verify_model())
    assert ok is True  # non-fatal: proceed unverified
    assert eng._model_verified is True
    assert sess.calls == 3  # capped at max attempts, no infinite retry


def test_verify_model_no_retry_on_real_config_error(monkeypatch):
    monkeypatch.setattr(st, "_TRIAGE_VERIFY_BACKOFF_S", 0.0)
    monkeypatch.setattr(st, "_TRIAGE_VERIFY_MAX_ATTEMPTS", 4)
    sess = _FakeSession([404])  # 404 is not a transient — must not retry
    eng = _engine(sess)
    ok = asyncio.run(eng.verify_model())
    assert ok is True
    assert sess.calls == 1  # single attempt, no retry on non-transient status


# ── §2 git reads run off the event loop ─────────────────────────────────
def test_momentum_async_runs_off_event_loop():
    loop_ident = {}
    git_ident = {}

    import backend.core.ouroboros.governance.git_momentum as gm
    orig = gm.compute_recent_momentum

    def _spy(*a, **k):
        git_ident["t"] = threading.get_ident()
        return orig(*a, **k)

    async def _run():
        loop_ident["t"] = threading.get_ident()
        gm.compute_recent_momentum = _spy
        try:
            await compute_recent_momentum_async(project_root=Path("."), max_commits=10)
        finally:
            gm.compute_recent_momentum = orig

    asyncio.run(_run())
    assert git_ident.get("t") is not None
    assert git_ident["t"] != loop_ident["t"]  # git ran on a worker thread


def test_extract_git_themes_async_returns_list():
    themes = asyncio.run(
        StrategicDirectionService._extract_git_themes_async(Path("."), max_commits=20)
    )
    assert isinstance(themes, list)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
