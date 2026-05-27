"""Slice 31 — Aegis Session-Bearer Lifecycle Synchronization.

Closes the v24 (``bt-2026-05-27-183704``) upstream coordination
roadblock surfaced by forensic audit: every outbound DW HTTP call
through the Aegis passthrough returned ``401 missing_session_bearer``.

# Root cause

``aegis_provider_bridge.dw_authorization_header()`` (legacy sync) was
returning ``{}`` whenever Aegis was enabled, on the now-falsified
assumption that the Aegis daemon would inject the bearer
server-side. The actual passthrough endpoint
(``aegis/passthrough.py:_bearer_session``) extracts
``Authorization: Bearer <session_token>`` from the *client* request;
absent that header it returns 401 with
``error="missing_session_bearer"``. Result: every /files upload,
/batches POST, batches GET poll, /files retrieve, /chat/completions
(streaming AND non-streaming), /models probe — all died at the gate
without ever reaching DW.

Per operator binding: "We do not compromise our security posture."
The fix is bidirectional: the client MUST present a valid session
bearer; the daemon MUST keep validating it.

# Slice 31 substrate

New async helper ``dw_session_auth_header()`` in
``aegis_provider_bridge.py``:

  * **Aegis enabled** → ``{"Authorization": "Bearer <session_token>"}``
    where ``session_token`` comes from
    ``AegisClient._ensure_session_token()`` (cached after first call).
  * **Aegis disabled** → byte-identical to the legacy non-Aegis
    branch: ``{"Authorization": "Bearer <DOUBLEWORD_API_KEY>"}``.
  * On any Aegis client error → ``{}`` (defensive — caller's existing
    401 path surfaces the real error rather than the helper raising
    inside a critical transport path).

# Slice 31 wiring (8 sites)

Every outbound DW HTTP call site in ``doubleword_provider.py``
composes the new helper BEFORE acquiring a per-call lease:

  1. ``_streaming chat completions`` (RT path, ~line 1935)
  2. ``_non-streaming chat completions`` (~line 2270)
  3. ``_upload_file`` (multi-part /files POST, ~line 2634)
  4. ``_create_batch`` (/batches POST, ~line 2679)
  5. ``_await_batch_result`` (/batches GET poll, ~line 2790)
  6. ``_retrieve_result`` (/files content GET, ~line 2863)
  7. ``complete()`` sync chat completions (~line 3240)
  8. ``health_probe`` (/models GET, ~line 3348)

The per-call Aegis lease (``X-JARVIS-Lease``, Slice 2B-ii) layers
on top via ``merge_lease_into_session_headers``. Per-call headers
override session headers in aiohttp — session-level
``_aegis_dw_auth_header()`` returning ``{}`` is fine; the per-call
bearer takes precedence.

# Test surface (4 AST pins + 7 spine)
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "aegis_provider_bridge.py"
)
DW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "doubleword_provider.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 4
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_dw_session_auth_header_helper_present() -> None:
    """``dw_session_auth_header`` MUST be an async function defined
    in ``aegis_provider_bridge.py``. Without it, none of the wiring
    sites compile, and the v24 401 wedge re-opens."""
    src = BRIDGE_FILE.read_text()
    tree = ast.parse(src, filename=str(BRIDGE_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "dw_session_auth_header"
        ):
            found = True
            # Must accept zero positional args (helper signature)
            assert len(node.args.args) == 0, (
                "dw_session_auth_header must take no positional args"
            )
            break
    assert found, (
        "dw_session_auth_header missing from aegis_provider_bridge.py — "
        "Slice 31 helper reverted; v24 401 wedge re-opens"
    )
    # Slice 31 attribution in raw source
    assert "Slice 31" in src, (
        "aegis_provider_bridge.py missing Slice 31 attribution"
    )


def test_ast_pin_doubleword_imports_session_auth_helper() -> None:
    """``doubleword_provider`` MUST import the new helper via the
    canonical alias ``_aegis_dw_session_auth_header``. Without the
    import, no call site can compose it."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "aegis_provider_bridge" in node.module:
                for alias in node.names:
                    if alias.name == "dw_session_auth_header":
                        assert alias.asname == "_aegis_dw_session_auth_header", (
                            "dw_session_auth_header must be imported as "
                            "_aegis_dw_session_auth_header (canonical alias)"
                        )
                        imported = True
                        break
    assert imported, (
        "doubleword_provider doesn't import dw_session_auth_header — "
        "Slice 31 wiring incomplete; the 8 outbound sites can't compose it"
    )


def test_ast_pin_all_eight_outbound_sites_compose_session_bearer() -> None:
    """Every async function in ``doubleword_provider`` that acquires
    a per-call Aegis lease (``_aegis_acquire_call_lease``) MUST also
    await ``_aegis_dw_session_auth_header()`` within the SAME
    function body. The lease without the session bearer reproduces
    the v24 401 wedge exactly."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    lease_funcs: list[str] = []
    bearer_funcs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            body = ast.unparse(node)
            if "_aegis_acquire_call_lease" in body:
                lease_funcs.append(node.name)
            if "_aegis_dw_session_auth_header" in body:
                bearer_funcs.append(node.name)
    missing = set(lease_funcs) - set(bearer_funcs)
    assert not missing, (
        f"Slice 31 wiring incomplete — functions acquire a lease but "
        f"never compose the session bearer: {sorted(missing)}. Each "
        f"such function reproduces the v24 401 missing_session_bearer "
        f"wedge on every call."
    )
    # Sanity: at least 5 such functions exist (we wired 8 sites across
    # multiple top-level methods; lower bound generous against
    # refactors).
    assert len(lease_funcs) >= 5, (
        f"Expected ≥5 lease-acquiring functions in doubleword_provider, "
        f"found {len(lease_funcs)} — refactor may have collapsed sites; "
        f"re-audit wiring."
    )


def test_ast_pin_legacy_helper_still_present_for_session_creation() -> None:
    """Legacy ``dw_authorization_header`` (sync) MUST remain importable
    in ``doubleword_provider`` — it's still used by ``_get_session``
    for the aiohttp session-level header (returning ``{}`` under
    Aegis is correct; per-call headers override). Removing it would
    break legacy session construction."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "aegis_provider_bridge" in node.module:
                for alias in node.names:
                    if alias.name == "dw_authorization_header":
                        imported = True
                        break
    assert imported, (
        "Legacy dw_authorization_header import dropped — _get_session "
        "session-construction path broken"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 7
# ──────────────────────────────────────────────────────────────────────


def test_spine_session_helper_returns_legacy_bearer_when_aegis_disabled() -> None:
    """When Aegis is disabled (the legacy path), the helper MUST
    return the DW API key as a Bearer — byte-identical to
    ``dw_authorization_header()``'s non-Aegis branch. No regression
    for operators who never enable Aegis."""
    from backend.core.ouroboros.governance import aegis_provider_bridge

    with mock.patch.object(
        aegis_provider_bridge.aegis_client_mod,
        "is_enabled",
        return_value=False,
    ), mock.patch.dict(
        os.environ, {"DOUBLEWORD_API_KEY": "sk-test-legacy-key"},
    ):
        headers = asyncio.run(aegis_provider_bridge.dw_session_auth_header())
        assert headers == {"Authorization": "Bearer sk-test-legacy-key"}


def test_spine_session_helper_returns_empty_when_aegis_disabled_no_key() -> None:
    """Aegis disabled + no DOUBLEWORD_API_KEY → empty dict. The
    caller's existing transport path surfaces the credential error;
    the helper does not raise into the upload site."""
    from backend.core.ouroboros.governance import aegis_provider_bridge

    with mock.patch.object(
        aegis_provider_bridge.aegis_client_mod,
        "is_enabled",
        return_value=False,
    ), mock.patch.dict(os.environ, {"DOUBLEWORD_API_KEY": ""}, clear=False):
        # Clear the key explicitly (mock.patch.dict with empty string)
        os.environ.pop("DOUBLEWORD_API_KEY", None)
        try:
            headers = asyncio.run(
                aegis_provider_bridge.dw_session_auth_header()
            )
            assert headers == {}
        finally:
            os.environ["DOUBLEWORD_API_KEY"] = ""  # restore for other tests


def test_spine_session_helper_fetches_session_token_when_aegis_enabled() -> None:
    """Aegis enabled → helper MUST fetch the session token via
    ``AegisClient._ensure_session_token()`` and return it as a
    Bearer header. This is the v24 wedge fix."""
    from backend.core.ouroboros.governance import aegis_provider_bridge

    fake_client = mock.MagicMock()
    fake_client._ensure_session_token = mock.AsyncMock(
        return_value="session-token-abc123",
    )

    with mock.patch.object(
        aegis_provider_bridge.aegis_client_mod,
        "is_enabled",
        return_value=True,
    ), mock.patch.object(
        aegis_provider_bridge.aegis_client_mod.AegisClient,
        "get",
        new=mock.AsyncMock(return_value=fake_client),
    ):
        headers = asyncio.run(aegis_provider_bridge.dw_session_auth_header())
        assert headers == {"Authorization": "Bearer session-token-abc123"}
        fake_client._ensure_session_token.assert_awaited_once()


def test_spine_session_helper_returns_empty_when_aegis_client_raises() -> None:
    """If the Aegis client raises (daemon down, network blip,
    credential rotation in flight), the helper MUST swallow and
    return ``{}``. Rationale: the existing 401 error path is the
    operator-visible surface; raising inside the helper would
    short-circuit observability."""
    from backend.core.ouroboros.governance import aegis_provider_bridge

    with mock.patch.object(
        aegis_provider_bridge.aegis_client_mod,
        "is_enabled",
        return_value=True,
    ), mock.patch.object(
        aegis_provider_bridge.aegis_client_mod.AegisClient,
        "get",
        new=mock.AsyncMock(side_effect=RuntimeError("daemon down")),
    ):
        headers = asyncio.run(aegis_provider_bridge.dw_session_auth_header())
        assert headers == {}


def test_spine_legacy_dw_authorization_header_unchanged() -> None:
    """The legacy sync helper ``dw_authorization_header`` retains
    its byte-identical behavior — returns ``{}`` under Aegis (which
    is correct for session-level headers, since per-call now
    provides the bearer), returns Bearer-key without Aegis."""
    from backend.core.ouroboros.governance import aegis_provider_bridge

    with mock.patch.object(
        aegis_provider_bridge.aegis_client_mod,
        "is_enabled",
        return_value=True,
    ):
        assert aegis_provider_bridge.dw_authorization_header() == {}

    with mock.patch.object(
        aegis_provider_bridge.aegis_client_mod,
        "is_enabled",
        return_value=False,
    ), mock.patch.dict(
        os.environ, {"DOUBLEWORD_API_KEY": "sk-legacy"},
    ):
        assert aegis_provider_bridge.dw_authorization_header() == {
            "Authorization": "Bearer sk-legacy",
        }


def test_spine_helper_is_async() -> None:
    """``dw_session_auth_header`` MUST be an async coroutine (the
    AegisClient session-token fetch is async; a sync helper couldn't
    await it). The legacy ``dw_authorization_header`` stays sync —
    they are deliberately split."""
    from backend.core.ouroboros.governance import aegis_provider_bridge

    assert asyncio.iscoroutinefunction(
        aegis_provider_bridge.dw_session_auth_header,
    ), "dw_session_auth_header must be async — it fetches via AegisClient"
    assert not asyncio.iscoroutinefunction(
        aegis_provider_bridge.dw_authorization_header,
    ), "legacy dw_authorization_header must remain sync (no breaking change)"


def test_spine_compose_bearer_uses_canonical_helper() -> None:
    """The helper MUST compose the bearer via ``_compose_bearer`` (the
    same canonical helper the legacy path uses), guaranteeing
    identical token formatting. Without this, the daemon's bearer
    parser could fail on a malformed header."""
    src = BRIDGE_FILE.read_text()
    tree = ast.parse(src, filename=str(BRIDGE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "dw_session_auth_header"
        ):
            body_src = ast.unparse(node)
            # Two _compose_bearer calls expected (Aegis-enabled +
            # legacy-fallback paths)
            count = body_src.count("_compose_bearer")
            assert count >= 2, (
                f"dw_session_auth_header should use _compose_bearer in "
                f"both paths (Aegis + legacy), found {count} call(s)"
            )
            return
    pytest.fail("dw_session_auth_header not found in AST walk")
