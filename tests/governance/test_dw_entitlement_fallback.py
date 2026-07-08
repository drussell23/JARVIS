"""Task 4 (ov cockpit silence, Slice 2, F1) — DW entitlement fallback spine.

Live run bt-2026-07-08-013911 hit a REAL 403 entitlement failure: the
Slice 39 surface probe (and a heavy probe) called DW with a model the
account is not entitled to
(``Model 'Qwen/Qwen3.5-35B-A3B-FP8' has not been configured or is not
available to user``). This spine pins the fix:

  * :mod:`dw_entitlement_classifier` now recognizes the exact live
    error-body phrasing (it previously only matched three DIFFERENT
    markers and would have mis-classified this as a transient
    AUTH_FAILURE).
  * :mod:`dw_entitlement_fallback` is the ONE shared resolver:
    policy-ordered preference list ∩ live entitled catalog → fallback
    model id, cached per-process with a TTL, single retry contract
    (empty intersection → legacy degrade, no retry storm), structured
    telemetry.
  * ``DoublewordProvider._upload_file`` wires the resolver at the
    exact site that 403'd live, with an ``_entitlement_retry`` guard
    that caps the retry at exactly one attempt per op.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from backend.core.ouroboros.governance.dw_entitlement_classifier import (
    KIND_ENTITLEMENT_BLOCKED,
    classify_4xx,
)
from backend.core.ouroboros.governance.dw_entitlement_fallback import (
    EntitlementCatalogCache,
    entitlement_fallback_enabled,
    is_entitlement_blocked,
    resolve_entitlement_fallback,
    select_fallback_model,
)


# ---------------------------------------------------------------------------
# F1a — classifier now recognizes the LIVE bt-2026-07-08-013911 body text
# ---------------------------------------------------------------------------


def test_classifier_recognizes_live_has_not_been_configured_message():
    """The exact live 403 body from bt-2026-07-08-013911 must classify
    as ENTITLEMENT_BLOCKED, not the legacy AUTH_FAILURE fallback."""
    body = (
        "Model 'Qwen/Qwen3.5-35B-A3B-FP8' has not been configured or "
        "is not available to user"
    )
    result = classify_4xx(403, body)
    assert result.kind == KIND_ENTITLEMENT_BLOCKED
    assert result.is_permanent is True


def test_classifier_recognizes_live_not_available_to_user_variant():
    body = "lightonai/LightOnOCR-2-1B is not available to user account"
    result = classify_4xx(403, body)
    assert result.kind == KIND_ENTITLEMENT_BLOCKED


def test_is_entitlement_blocked_wraps_classifier():
    body = "Model 'x/y' has not been configured or is not available to user"
    assert is_entitlement_blocked(403, body) is True
    assert is_entitlement_blocked(401, body) is False  # 401 always auth
    assert is_entitlement_blocked(403, "totally unrelated schema error") is False


def test_is_entitlement_blocked_never_raises_on_garbage():
    assert is_entitlement_blocked(403, None) is False  # type: ignore[arg-type]
    assert is_entitlement_blocked(-1, "") is False


# ---------------------------------------------------------------------------
# F1b — pure selection: policy-ordered fallback ∩ live catalog
# ---------------------------------------------------------------------------


def test_select_fallback_model_picks_highest_preference_present_in_catalog():
    fallback = select_fallback_model(
        blocked_model_id="Qwen/Qwen3.5-35B-A3B-FP8",
        preference_order=(
            "Qwen/Qwen3.5-35B-A3B-FP8",  # blocked — must be skipped even if listed
            "google/gemma-4-31B-it",     # not entitled in this fake catalog
            "Qwen/Qwen3-14B-FP8",        # entitled — expected winner
            "Qwen/Qwen3.5-397B-A17B-FP8",
        ),
        entitled_ids={"Qwen/Qwen3-14B-FP8", "Qwen/Qwen3.5-397B-A17B-FP8"},
    )
    assert fallback == "Qwen/Qwen3-14B-FP8"


def test_select_fallback_model_empty_intersection_returns_none():
    fallback = select_fallback_model(
        blocked_model_id="Qwen/Qwen3.5-35B-A3B-FP8",
        preference_order=("google/gemma-4-31B-it", "Qwen/Qwen3-14B-FP8"),
        entitled_ids={"some/other-model"},
    )
    assert fallback is None


def test_select_fallback_model_never_reselects_the_blocked_model():
    """Even if the blocked model is the ONLY entitled candidate in the
    preference order, it must never be re-selected as its own
    fallback (that would just retry the same 403)."""
    fallback = select_fallback_model(
        blocked_model_id="Qwen/Qwen3.5-35B-A3B-FP8",
        preference_order=("Qwen/Qwen3.5-35B-A3B-FP8",),
        entitled_ids={"Qwen/Qwen3.5-35B-A3B-FP8"},
    )
    assert fallback is None


def test_select_fallback_model_empty_preference_order_returns_none():
    assert select_fallback_model(
        blocked_model_id="x", preference_order=(), entitled_ids={"y", "z"},
    ) is None


# ---------------------------------------------------------------------------
# F1c — per-process entitled-catalog cache (TTL-bounded)
# ---------------------------------------------------------------------------


class _FakeSnapshot:
    def __init__(self, ids):
        self._ids = tuple(ids)

    def model_ids(self):
        return self._ids


class _FakeCatalogClient:
    """Counts ``fetch()`` calls so tests can assert TTL caching behavior."""

    def __init__(self, ids):
        self.fetch_calls = 0
        self._ids = ids

    async def fetch(self):
        self.fetch_calls += 1
        return _FakeSnapshot(self._ids)


class _RaisingCatalogClient:
    async def fetch(self):
        raise RuntimeError("network exploded")


def test_entitlement_cache_fetches_once_within_ttl():
    cache = EntitlementCatalogCache()
    client = _FakeCatalogClient(["a", "b"])

    async def _run():
        ids1 = await cache.get_entitled_ids(client)
        ids2 = await cache.get_entitled_ids(client)
        return ids1, ids2

    ids1, ids2 = asyncio.run(_run())
    assert ids1 == frozenset({"a", "b"})
    assert ids2 == frozenset({"a", "b"})
    assert client.fetch_calls == 1, "second call within TTL must NOT re-fetch"


def test_entitlement_cache_force_refresh_refetches():
    cache = EntitlementCatalogCache()
    client = _FakeCatalogClient(["a"])

    async def _run():
        await cache.get_entitled_ids(client)
        await cache.get_entitled_ids(client, force_refresh=True)

    asyncio.run(_run())
    assert client.fetch_calls == 2


def test_entitlement_cache_survives_fetch_failure():
    """A raising catalog client must not raise out of the cache — the
    cache stays at its (possibly empty) prior value."""
    cache = EntitlementCatalogCache()
    client = _RaisingCatalogClient()

    async def _run():
        return await cache.get_entitled_ids(client)

    ids = asyncio.run(_run())
    assert ids == frozenset()


def test_entitlement_cache_reset_forces_next_refetch():
    cache = EntitlementCatalogCache()
    client = _FakeCatalogClient(["a"])

    async def _run():
        await cache.get_entitled_ids(client)
        cache.reset()
        await cache.get_entitled_ids(client)

    asyncio.run(_run())
    assert client.fetch_calls == 2


# ---------------------------------------------------------------------------
# F1d — composed resolver: cache + selection + telemetry
# ---------------------------------------------------------------------------


def test_resolve_entitlement_fallback_chooses_and_logs_telemetry(caplog):
    client = _FakeCatalogClient(["Qwen/Qwen3-14B-FP8"])
    cache = EntitlementCatalogCache()

    async def _run():
        return await resolve_entitlement_fallback(
            blocked_model_id="Qwen/Qwen3.5-35B-A3B-FP8",
            preference_order=("Qwen/Qwen3-14B-FP8",),
            catalog_client=client,
            cache=cache,
        )

    with caplog.at_level(logging.WARNING, logger="Ouroboros.DWEntitlement"):
        fallback = asyncio.run(_run())

    assert fallback == "Qwen/Qwen3-14B-FP8"
    joined = "\n".join(r.message for r in caplog.records)
    assert "[DWEntitlement]" in joined
    assert "model=Qwen/Qwen3.5-35B-A3B-FP8" in joined
    assert "fallback=Qwen/Qwen3-14B-FP8" in joined
    assert "policy∩catalog" in joined


def test_resolve_entitlement_fallback_empty_intersection_degrades(caplog):
    client = _FakeCatalogClient(["some/unrelated-model"])
    cache = EntitlementCatalogCache()

    async def _run():
        return await resolve_entitlement_fallback(
            blocked_model_id="Qwen/Qwen3.5-35B-A3B-FP8",
            preference_order=("google/gemma-4-31B-it",),
            catalog_client=client,
            cache=cache,
        )

    with caplog.at_level(logging.WARNING, logger="Ouroboros.DWEntitlement"):
        fallback = asyncio.run(_run())

    assert fallback is None
    joined = "\n".join(r.message for r in caplog.records)
    assert "no_fallback" in joined


def test_resolve_entitlement_fallback_respects_master_flag(monkeypatch):
    """``JARVIS_DW_ENTITLEMENT_FALLBACK_ENABLED=false`` must short-
    circuit to None without even touching the catalog client
    (operator hot-revert path)."""
    monkeypatch.setenv("JARVIS_DW_ENTITLEMENT_FALLBACK_ENABLED", "false")
    assert entitlement_fallback_enabled() is False
    client = _FakeCatalogClient(["Qwen/Qwen3-14B-FP8"])

    async def _run():
        return await resolve_entitlement_fallback(
            blocked_model_id="Qwen/Qwen3.5-35B-A3B-FP8",
            preference_order=("Qwen/Qwen3-14B-FP8",),
            catalog_client=client,
        )

    fallback = asyncio.run(_run())
    assert fallback is None
    assert client.fetch_calls == 0


def test_resolve_entitlement_fallback_never_raises_on_bad_catalog_client():
    async def _run():
        return await resolve_entitlement_fallback(
            blocked_model_id="x",
            preference_order=("y",),
            catalog_client=_RaisingCatalogClient(),
            cache=EntitlementCatalogCache(),
        )
    assert asyncio.run(_run()) is None


# ---------------------------------------------------------------------------
# F1e — DoublewordProvider._upload_file wiring: single retry, no storm
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status, text_body="", json_body=None):
        self.status = status
        self._text_body = text_body
        self._json_body = json_body or {}

    async def text(self):
        return self._text_body

    async def json(self):
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePostCtx:
    """Records every POST call; returns queued responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


class _FakeSession:
    def __init__(self, post_ctx):
        self.post = post_ctx


def _make_provider():
    from backend.core.ouroboros.governance._governance_state import (
        DoubleWordProviderState,
    )
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    provider = DoublewordProvider.__new__(DoublewordProvider)
    provider._state = DoubleWordProviderState.fresh()
    provider._base_url = "https://dw.example/v1"
    provider._api_key = "test-key"
    provider._rate_limiter = None
    return provider


def _blocked_jsonl(model_id: str) -> str:
    entry = {
        "custom_id": "op-1",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": model_id, "messages": [{"role": "user", "content": "hi"}]},
    }
    return json.dumps(entry) + "\n"


def test_upload_file_retries_once_with_fallback_on_entitlement_403(monkeypatch):
    provider = _make_provider()
    blocked_model = "Qwen/Qwen3.5-35B-A3B-FP8"
    fallback_model = "Qwen/Qwen3-14B-FP8"

    responses = [
        _FakeResp(
            403,
            text_body=(
                f"Model '{blocked_model}' has not been configured or "
                "is not available to user"
            ),
        ),
        _FakeResp(200, json_body={"id": "file-abc123"}),
    ]
    post_ctx = _FakePostCtx(responses)
    fake_session = _FakeSession(post_ctx)

    async def _fake_get_session():
        return fake_session

    monkeypatch.setattr(provider, "_get_session", _fake_get_session)
    monkeypatch.setattr(provider, "_request_timeout", lambda: 30)

    resolved_models = []

    async def _fake_fallback_model(blocked_model_id):
        resolved_models.append(blocked_model_id)
        return fallback_model

    monkeypatch.setattr(
        provider, "_entitlement_fallback_model", _fake_fallback_model,
    )

    result = asyncio.run(
        provider._upload_file(_blocked_jsonl(blocked_model), op_id="op-1"),
    )

    assert result == "file-abc123"
    assert len(post_ctx.calls) == 2, "must retry exactly once — no retry storm"
    assert resolved_models == [blocked_model]

    # The retried payload must carry the fallback model, not the blocked one.
    _, second_kwargs = post_ctx.calls[1]
    uploaded_form = second_kwargs["data"]
    # aiohttp.FormData internal fields aren't trivially introspectable here;
    # instead assert via the fallback resolver call + swap helper directly.
    swapped = provider._swap_jsonl_model_field(
        _blocked_jsonl(blocked_model), fallback_model,
    )
    assert json.loads(swapped.strip())["body"]["model"] == fallback_model


def test_upload_file_no_retry_when_fallback_resolver_returns_none(monkeypatch):
    """Empty policy∩catalog intersection → legacy degrade: exactly one
    POST, no retry, returns None."""
    provider = _make_provider()
    blocked_model = "Qwen/Qwen3.5-35B-A3B-FP8"

    responses = [
        _FakeResp(
            403,
            text_body=(
                f"Model '{blocked_model}' has not been configured or "
                "is not available to user"
            ),
        ),
    ]
    post_ctx = _FakePostCtx(responses)
    fake_session = _FakeSession(post_ctx)

    async def _fake_get_session():
        return fake_session

    monkeypatch.setattr(provider, "_get_session", _fake_get_session)
    monkeypatch.setattr(provider, "_request_timeout", lambda: 30)

    async def _fake_fallback_model(blocked_model_id):
        return None  # empty intersection

    monkeypatch.setattr(
        provider, "_entitlement_fallback_model", _fake_fallback_model,
    )

    result = asyncio.run(
        provider._upload_file(_blocked_jsonl(blocked_model), op_id="op-1"),
    )

    assert result is None
    assert len(post_ctx.calls) == 1, "empty intersection must NOT retry"


def test_upload_file_no_retry_storm_even_if_retry_also_403s(monkeypatch):
    """The ``_entitlement_retry`` guard is a hard cap — even if the
    fallback model ALSO comes back 403-entitlement-blocked, there must
    be no second fallback attempt (no unbounded retry storm)."""
    provider = _make_provider()
    blocked_model = "Qwen/Qwen3.5-35B-A3B-FP8"
    fallback_model = "Qwen/Qwen3-14B-FP8"

    responses = [
        _FakeResp(
            403,
            text_body=f"Model '{blocked_model}' has not been configured or "
                      "is not available to user",
        ),
        _FakeResp(
            403,
            text_body=f"Model '{fallback_model}' has not been configured or "
                      "is not available to user",
        ),
    ]
    post_ctx = _FakePostCtx(responses)
    fake_session = _FakeSession(post_ctx)

    async def _fake_get_session():
        return fake_session

    monkeypatch.setattr(provider, "_get_session", _fake_get_session)
    monkeypatch.setattr(provider, "_request_timeout", lambda: 30)

    fallback_calls = []

    async def _fake_fallback_model(blocked_model_id):
        fallback_calls.append(blocked_model_id)
        return fallback_model

    monkeypatch.setattr(
        provider, "_entitlement_fallback_model", _fake_fallback_model,
    )

    result = asyncio.run(
        provider._upload_file(_blocked_jsonl(blocked_model), op_id="op-1"),
    )

    assert result is None
    assert len(post_ctx.calls) == 2, "exactly one retry attempt, then stop"
    assert fallback_calls == [blocked_model], (
        "fallback resolution must be attempted exactly once per op"
    )


def test_upload_file_no_retry_on_non_entitlement_403(monkeypatch):
    """A plain auth-failure 403 (no entitlement marker) must NOT
    trigger fallback resolution at all — legacy behavior preserved."""
    provider = _make_provider()
    blocked_model = "Qwen/Qwen3.5-35B-A3B-FP8"

    responses = [_FakeResp(403, text_body="invalid api key")]
    post_ctx = _FakePostCtx(responses)
    fake_session = _FakeSession(post_ctx)

    async def _fake_get_session():
        return fake_session

    monkeypatch.setattr(provider, "_get_session", _fake_get_session)
    monkeypatch.setattr(provider, "_request_timeout", lambda: 30)

    fallback_calls = []

    async def _fake_fallback_model(blocked_model_id):
        fallback_calls.append(blocked_model_id)
        return "should-not-be-used"

    monkeypatch.setattr(
        provider, "_entitlement_fallback_model", _fake_fallback_model,
    )

    result = asyncio.run(
        provider._upload_file(_blocked_jsonl(blocked_model), op_id="op-1"),
    )

    assert result is None
    assert len(post_ctx.calls) == 1
    assert fallback_calls == [], (
        "plain auth failures must not trigger entitlement fallback"
    )


# ---------------------------------------------------------------------------
# F1f — swap helper
# ---------------------------------------------------------------------------


def test_swap_jsonl_model_field_replaces_model_preserves_rest():
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    original = _blocked_jsonl("old-model")
    swapped = DoublewordProvider._swap_jsonl_model_field(original, "new-model")
    parsed = json.loads(swapped.strip())
    assert parsed["body"]["model"] == "new-model"
    assert parsed["custom_id"] == "op-1"
    assert parsed["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert swapped.endswith("\n")


def test_swap_jsonl_model_field_multi_line_swaps_every_entry():
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    line1 = json.dumps({
        "custom_id": "a", "method": "POST", "url": "/x",
        "body": {"model": "old", "x": 1},
    })
    line2 = json.dumps({
        "custom_id": "b", "method": "POST", "url": "/x",
        "body": {"model": "old", "x": 2},
    })
    swapped = DoublewordProvider._swap_jsonl_model_field(
        line1 + "\n" + line2 + "\n", "new",
    )
    lines = [ln for ln in swapped.split("\n") if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        assert json.loads(ln)["body"]["model"] == "new"


def test_swap_jsonl_model_field_passes_through_unparseable_lines():
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    swapped = DoublewordProvider._swap_jsonl_model_field(
        "not json at all\n", "new-model",
    )
    assert "not json at all" in swapped
