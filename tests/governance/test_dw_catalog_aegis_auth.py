from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import dw_catalog_client as dcc


def _client():
    return dcc.DwCatalogClient(object(), "https://x/v1", "RAWKEY")


@pytest.mark.asyncio
async def test_auth_falls_back_to_raw_bearer_when_aegis_off(monkeypatch):
    import backend.core.ouroboros.aegis.client as ac
    monkeypatch.setattr(ac, "is_enabled", lambda: False)
    h = await _client()._auth_headers()
    assert h["Authorization"] == "Bearer RAWKEY"


@pytest.mark.asyncio
async def test_auth_uses_vault_header_when_aegis_on(monkeypatch):
    import backend.core.ouroboros.aegis.client as ac
    monkeypatch.setattr(ac, "is_enabled", lambda: True)
    import backend.core.ouroboros.governance.aegis_provider_bridge as br
    async def _fake_vault():
        return {"Authorization": "Bearer VAULT_INJECTED"}
    monkeypatch.setattr(br, "dw_session_auth_header", _fake_vault)
    h = await _client()._auth_headers()
    assert h["Authorization"] == "Bearer VAULT_INJECTED"   # NOT the scrubbed raw key


@pytest.mark.asyncio
async def test_auth_failsoft_to_raw_when_vault_empty(monkeypatch):
    import backend.core.ouroboros.aegis.client as ac
    monkeypatch.setattr(ac, "is_enabled", lambda: True)
    import backend.core.ouroboros.governance.aegis_provider_bridge as br
    async def _empty_vault():
        return {}
    monkeypatch.setattr(br, "dw_session_auth_header", _empty_vault)
    h = await _client()._auth_headers()
    assert h["Authorization"] == "Bearer RAWKEY"            # vault empty -> legacy fallback
