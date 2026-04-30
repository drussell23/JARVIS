"""Transport Resilience Layer regression — explicit ``httpx.Limits`` +
segmented ``httpx.Timeout`` on ClaudeProvider's httpx transport.

Pins:
  * Explicit ``httpx.Limits`` is constructed and attached to the actual
    ``AsyncConnectionPool`` (not just dropped on the AsyncAnthropic wrapper).
  * Default caps are tight: max_connections=10, max_keepalive_connections=5,
    keepalive_expiry=30s.
  * Env overrides honored: ``JARVIS_CLAUDE_HTTP_MAX_CONNECTIONS``,
    ``JARVIS_CLAUDE_HTTP_MAX_KEEPALIVE``,
    ``JARVIS_CLAUDE_HTTP_KEEPALIVE_EXPIRY_S``.
  * Segmented ``httpx.Timeout`` retains tight connect (10s) + generous read.
  * Recycle path constructs a fresh httpx client with the same Limits.

These pins close the regression vector observed in soak
``bt-2026-04-30-021210``: under sustained load the SDK's default 1000/100
pool caps allowed stale keepalives to accumulate, masquerading as
``ConnectTimeout`` and ``SSLWantReadError`` failures and idle-locking the
session at 17 ops / 0 completions / 1h.

Authority Invariant
-------------------
Tests import only from the providers module under test plus stdlib +
httpx. No orchestrator / phase_runners / iron_gate imports.
"""
from __future__ import annotations

import importlib

import httpx
import pytest


def _fresh_provider(monkeypatch, **env):
    """Construct a ClaudeProvider with a fresh module reload so that
    env-driven module-level constants are re-read."""
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    # Reload the module so module-level _CLAUDE_HTTP_* constants pick up env.
    import backend.core.ouroboros.governance.providers as _providers
    importlib.reload(_providers)
    return _providers.ClaudeProvider(api_key="sk-ant-test")


def _pool_caps(client) -> tuple:
    """Walk SDK -> httpx -> transport -> pool to read the actual caps."""
    http_client = client._client
    assert isinstance(http_client, httpx.AsyncClient)
    transport = http_client._transport
    pool = transport._pool
    return (
        pool._max_connections,
        pool._max_keepalive_connections,
        pool._keepalive_expiry,
    )


def _timeout(client) -> httpx.Timeout:
    return client._client.timeout


# -----------------------------------------------------------------------
# § A — Default Limits + Timeout pins
# -----------------------------------------------------------------------


def test_default_max_connections_pinned(monkeypatch):
    p = _fresh_provider(monkeypatch)
    max_conn, _, _ = _pool_caps(p._ensure_client())
    assert max_conn == 10


def test_default_max_keepalive_pinned(monkeypatch):
    p = _fresh_provider(monkeypatch)
    _, max_keepalive, _ = _pool_caps(p._ensure_client())
    assert max_keepalive == 5


def test_default_keepalive_expiry_pinned(monkeypatch):
    p = _fresh_provider(monkeypatch)
    _, _, expiry = _pool_caps(p._ensure_client())
    assert expiry == 30.0


def test_default_connect_timeout_tight(monkeypatch):
    p = _fresh_provider(monkeypatch)
    timeout = _timeout(p._ensure_client())
    assert timeout.connect == 10.0


def test_read_timeout_remains_generous(monkeypatch):
    p = _fresh_provider(monkeypatch)
    timeout = _timeout(p._ensure_client())
    # Default (non-thinking) read = 120s. Thinking path = 600s. Either is
    # generous relative to the 10s connect.
    assert timeout.read in (120.0, 600.0)
    assert timeout.read >= 10 * timeout.connect


# -----------------------------------------------------------------------
# § B — Env override honored
# -----------------------------------------------------------------------


def test_env_max_connections_override(monkeypatch):
    p = _fresh_provider(
        monkeypatch, JARVIS_CLAUDE_HTTP_MAX_CONNECTIONS="25",
    )
    max_conn, _, _ = _pool_caps(p._ensure_client())
    assert max_conn == 25


def test_env_max_keepalive_override(monkeypatch):
    p = _fresh_provider(
        monkeypatch, JARVIS_CLAUDE_HTTP_MAX_KEEPALIVE="2",
    )
    _, max_keepalive, _ = _pool_caps(p._ensure_client())
    assert max_keepalive == 2


def test_env_keepalive_expiry_override(monkeypatch):
    p = _fresh_provider(
        monkeypatch, JARVIS_CLAUDE_HTTP_KEEPALIVE_EXPIRY_S="5.0",
    )
    _, _, expiry = _pool_caps(p._ensure_client())
    assert expiry == 5.0


# -----------------------------------------------------------------------
# § C — Recycle path preserves Limits
# -----------------------------------------------------------------------


def test_recycle_constructs_new_client_with_same_caps(monkeypatch):
    p = _fresh_provider(monkeypatch)
    client_a = p._ensure_client()
    caps_a = _pool_caps(client_a)
    p._recycle_client("test_trigger")
    client_b = p._ensure_client()
    caps_b = _pool_caps(client_b)
    assert caps_a == caps_b
    assert client_a is not client_b


# -----------------------------------------------------------------------
# § D — Authority invariant: stdlib + httpx + provider only
# -----------------------------------------------------------------------


def test_authority_invariant_no_orchestrator_imports():
    """This test module must not pull in orchestrator / phase_runners /
    iron_gate. Bytes-pinned at the source-file level."""
    import pathlib
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "orchestrator", "phase_runners", "iron_gate",
        "change_engine", "candidate_generator", "policy",
    )
    for tok in forbidden:
        assert f"import {tok}" not in src, f"forbidden import: {tok}"
        assert f"from backend.core.ouroboros.governance.{tok}" not in src
