"""Phase 12 Slice A — DwCatalogClient regression spine.

Pins:
  §1 Master flag — JARVIS_DW_CATALOG_DISCOVERY_ENABLED default false +
                   truthy/falsy parsing
  §2 ModelCard parsing — id-required, optional fields, pricing shapes,
                         streaming default, raw metadata preservation
  §3 Parameter-count regex — Qwen/Gemma id heuristics, conservative None
  §4 has_ambiguous_metadata — Zero-Trust §3.6 quarantine rule
  §5 CatalogSnapshot — freshness, JSON round-trip, schema version
  §6 Disk cache — atomic write, atomic read, version mismatch, missing
  §7 DwCatalogClient.fetch() — clean fetch, OpenAI envelope vs bare list
  §8 fetch() failure paths — http 5xx, timeout, transport, malformed JSON,
                              all NEVER raise + populate fetch_failure_reason
  §9 fetch() with no cache — failure returns empty snapshot, not exception
  §10 cached() + stale() — memory hydration, disk fallback
  §11 Source-level pins — fetch never raises (try/except contract)
"""
from __future__ import annotations

import asyncio  # noqa: F401  — pytest-asyncio plugin contract
import json
import os  # noqa: F401  — env-var reads in fixtures
import time
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance import dw_catalog_client as dcc
from backend.core.ouroboros.governance.dw_catalog_client import (
    CATALOG_SCHEMA_VERSION,
    CatalogSnapshot,
    DwCatalogClient,
    ModelCard,
    discovery_enabled,
    load_cached_snapshot,
    parse_family,
    parse_parameter_count,
    save_snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the disk cache to a tmpdir-scoped path."""
    cache = tmp_path / "dw_catalog.json"
    monkeypatch.setenv("JARVIS_DW_CATALOG_PATH", str(cache))
    yield cache


def _mock_session(json_body: Any = None, status: int = 200,
                  raise_exc: Optional[Exception] = None) -> Any:
    """Build a mock aiohttp.ClientSession that yields the configured
    response from a single ``session.get()`` call."""
    session = MagicMock()

    class _Resp:
        def __init__(self) -> None:
            self.status = status

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def json(self) -> Any:
            return json_body

    def _get(url: str, **kwargs: Any) -> Any:  # noqa: ARG001
        if raise_exc is not None:
            raise raise_exc
        return _Resp()

    session.get = _get
    return session


def _client(session: Any, cache_path: Optional[Path] = None) -> DwCatalogClient:
    return DwCatalogClient(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        cache_path=cache_path,
    )


# ===========================================================================
# §1 — Master flag
# ===========================================================================


def test_discovery_default_on_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice E graduation flip: unset/empty env returns True."""
    monkeypatch.delenv("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", raising=False)
    assert discovery_enabled() is True


def test_discovery_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", val)
        assert discovery_enabled() is True


def test_discovery_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-graduation: empty string is the unset-marker for default
    True, so it's NOT in the falsy list. Hot-revert requires an
    explicit ``false``-class string."""
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", val)
        assert discovery_enabled() is False


# ===========================================================================
# §2 — ModelCard parsing
# ===========================================================================


def test_model_card_requires_id() -> None:
    """No ``id`` → None. Empty string id → None."""
    assert ModelCard.from_api_dict({}) is None
    assert ModelCard.from_api_dict({"id": ""}) is None
    assert ModelCard.from_api_dict({"id": "   "}) is None
    assert ModelCard.from_api_dict({"id": None}) is None


def test_model_card_minimal_id_only(monkeypatch) -> None:
    """Just ``id`` produces a card; param count parsed from id.

    Pricing is intentionally blank from the API. Under Option α the
    Pricing Oracle fills the missing pricing for known families — this
    test pins the LEGACY (master-off) behavior so the catalog parser's
    "no API pricing → both None" path stays covered. The Oracle hit
    case is pinned in test_dw_catalog_client_pricing_oracle_hook.py.
    """
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", "false")
    card = ModelCard.from_api_dict({"id": "Qwen/Qwen3.5-397B-A17B"})
    assert card is not None
    assert card.model_id == "Qwen/Qwen3.5-397B-A17B"
    assert card.family == "qwen"
    assert card.parameter_count_b == 397.0
    assert card.context_window is None
    assert card.pricing_in_per_m_usd is None
    assert card.pricing_out_per_m_usd is None
    assert card.supports_streaming is True


def test_model_card_pricing_nested_shape() -> None:
    """``pricing: {input, output}`` is the OpenAI-compat shape."""
    card = ModelCard.from_api_dict({
        "id": "moonshotai/Kimi-K2.6",
        "pricing": {"input": 0.10, "output": 0.40},
    })
    assert card is not None
    assert card.pricing_in_per_m_usd == 0.10
    assert card.pricing_out_per_m_usd == 0.40


def test_model_card_pricing_top_level_shape() -> None:
    """Top-level ``pricing_in_per_m_usd`` / ``pricing_out_per_m_usd``."""
    card = ModelCard.from_api_dict({
        "id": "Qwen/Qwen3.5-9B",
        "pricing_in_per_m_usd": 0.04,
        "pricing_out_per_m_usd": 0.06,
    })
    assert card is not None
    assert card.pricing_in_per_m_usd == 0.04
    assert card.pricing_out_per_m_usd == 0.06


def test_model_card_context_window() -> None:
    card = ModelCard.from_api_dict({
        "id": "moonshotai/Kimi-K2.6",
        "context_window": 200000,
    })
    assert card is not None
    assert card.context_window == 200000


def test_model_card_streaming_explicit_false() -> None:
    card = ModelCard.from_api_dict({
        "id": "fake/model-1B",
        "supports_streaming": False,
    })
    assert card is not None
    assert card.supports_streaming is False


def test_model_card_api_param_count_takes_precedence_over_heuristic() -> None:
    """When API exposes ``parameter_count_b``, use that even if id
    suggests otherwise. Treats API as ground truth."""
    card = ModelCard.from_api_dict({
        "id": "weird-id-without-bn-suffix",
        "parameter_count_b": 70.0,
    })
    assert card is not None
    assert card.parameter_count_b == 70.0


def test_model_card_invalid_pricing_ignored() -> None:
    """Negative or non-numeric pricing must NOT poison the card."""
    card = ModelCard.from_api_dict({
        "id": "fake/model-7B",
        "pricing": {"input": "not-a-number", "output": -5.0},
    })
    assert card is not None
    assert card.pricing_in_per_m_usd is None  # string rejected
    # Negative pricing is not >= 0 in our gate, so... actually we
    # allow >= 0, so let's check the strict gate worked.
    assert card.pricing_out_per_m_usd is None  # -5.0 doesn't pass >= 0


def test_model_card_raw_metadata_preserved() -> None:
    raw = {
        "id": "Qwen/Qwen3.6-35B-A3B-FP8",
        "exotic_field": [1, 2, 3],
    }
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    parsed_back = json.loads(card.raw_metadata_json)
    assert parsed_back["id"] == "Qwen/Qwen3.6-35B-A3B-FP8"
    assert parsed_back["exotic_field"] == [1, 2, 3]


# ===========================================================================
# §3 — Parameter-count regex
# ===========================================================================


@pytest.mark.parametrize("model_id,expected", [
    ("Qwen/Qwen3.5-397B-A17B", 397.0),
    ("google/gemma-4-31B-it", 31.0),
    ("Qwen/Qwen3.6-35B-A3B-FP8", 35.0),
    ("Qwen/Qwen3-14B-FP8", 14.0),
    ("Qwen/Qwen3.5-9B", 9.0),
    ("Qwen/Qwen3.5-4B", 4.0),
    ("fake/model-7B-instruct", 7.0),
    ("fake/model-1.5B", 1.5),
])
def test_parse_parameter_count_known_ids(
    model_id: str, expected: float,
) -> None:
    assert parse_parameter_count(model_id) == expected


@pytest.mark.parametrize("model_id", [
    "moonshotai/Kimi-K2.6",   # no Bn suffix
    "zai-org/GLM-5.1-FP8",    # version dot, no Bn
    "",
    None,
    "no-slash-no-suffix",
    "fake/no-numeric-marker",
])
def test_parse_parameter_count_returns_none_for_unparseable(model_id: Any) -> None:
    """Conservative — when in doubt, return None (→ Zero-Trust quarantine)."""
    assert parse_parameter_count(model_id) is None


def test_parse_family() -> None:
    assert parse_family("Qwen/Qwen3.5-397B") == "qwen"
    assert parse_family("moonshotai/Kimi-K2.6") == "moonshotai"
    assert parse_family("zai-org/GLM-5.1-FP8") == "zai-org"
    assert parse_family("google/gemma-4-31B-it") == "google"
    assert parse_family("no-slash") == "unknown"
    assert parse_family("") == "unknown"


# ===========================================================================
# §4 — has_ambiguous_metadata (Zero-Trust §3.6)
# ===========================================================================


def test_ambiguous_metadata_when_both_missing() -> None:
    """No param count AND no out-pricing → SPECULATIVE quarantine signal."""
    card = ModelCard.from_api_dict({"id": "moonshotai/Kimi-K2.6"})
    assert card is not None
    assert card.parameter_count_b is None
    assert card.pricing_out_per_m_usd is None
    assert card.has_ambiguous_metadata() is True


def test_not_ambiguous_with_param_count_alone() -> None:
    """Param count parsed from id is enough — not ambiguous."""
    card = ModelCard.from_api_dict({"id": "Qwen/Qwen3.5-397B-A17B"})
    assert card is not None
    assert card.has_ambiguous_metadata() is False


def test_not_ambiguous_with_pricing_alone() -> None:
    card = ModelCard.from_api_dict({
        "id": "moonshotai/Kimi-K2.6",
        "pricing": {"input": 0.10, "output": 0.40},
    })
    assert card is not None
    assert card.has_ambiguous_metadata() is False


# ===========================================================================
# §5 — CatalogSnapshot freshness + JSON round-trip
# ===========================================================================


def test_snapshot_is_fresh_within_window() -> None:
    snap = CatalogSnapshot(
        fetched_at_unix=time.time() - 10,
        models=(),
    )
    assert snap.is_fresh(max_age_s=60) is True


def test_snapshot_not_fresh_past_window() -> None:
    snap = CatalogSnapshot(
        fetched_at_unix=time.time() - 7300,
        models=(),
    )
    assert snap.is_fresh(max_age_s=7200) is False


def test_snapshot_json_roundtrip() -> None:
    original = CatalogSnapshot(
        fetched_at_unix=1777_333_000.0,
        models=(
            ModelCard(
                model_id="Qwen/Qwen3.5-397B-A17B",
                family="qwen",
                parameter_count_b=397.0,
                context_window=128000,
                pricing_in_per_m_usd=0.10,
                pricing_out_per_m_usd=0.40,
                supports_streaming=True,
                raw_metadata_json='{"id":"Qwen/Qwen3.5-397B-A17B"}',
            ),
            ModelCard(
                model_id="moonshotai/Kimi-K2.6",
                family="moonshotai",
                parameter_count_b=None,
                context_window=None,
                pricing_in_per_m_usd=None,
                pricing_out_per_m_usd=None,
                supports_streaming=True,
                raw_metadata_json="{}",
            ),
        ),
        fetch_latency_ms=234,
        fetch_failure_reason=None,
    )
    text = original.to_json()
    parsed = CatalogSnapshot.from_json(text)
    assert parsed is not None
    assert parsed.fetched_at_unix == original.fetched_at_unix
    assert parsed.fetch_latency_ms == 234
    assert parsed.fetch_failure_reason is None
    assert len(parsed.models) == 2
    assert parsed.models[0].parameter_count_b == 397.0
    assert parsed.models[1].parameter_count_b is None
    assert parsed.models[1].has_ambiguous_metadata() is True


def test_snapshot_from_json_rejects_wrong_schema_version() -> None:
    """Future-version cache → treat as missing (forces re-fetch)."""
    payload = json.dumps({
        "schema_version": "dw_catalog.99",
        "fetched_at_unix": 1.0,
        "models": [],
    })
    assert CatalogSnapshot.from_json(payload) is None


def test_snapshot_from_json_rejects_garbage() -> None:
    assert CatalogSnapshot.from_json("not json at all") is None
    assert CatalogSnapshot.from_json("[]") is None  # not a dict
    assert CatalogSnapshot.from_json("null") is None


def test_snapshot_from_json_skips_malformed_entries() -> None:
    """One bad entry doesn't blow up the whole snapshot — load the rest."""
    payload = json.dumps({
        "schema_version": CATALOG_SCHEMA_VERSION,
        "fetched_at_unix": 1.0,
        "models": [
            {"model_id": "good/model-7B", "family": "good",
             "parameter_count_b": 7.0, "context_window": None,
             "pricing_in_per_m_usd": None, "pricing_out_per_m_usd": None,
             "supports_streaming": True, "raw_metadata_json": "{}"},
            "this is not a dict",  # malformed
            {"model_id": "", "family": "x"},  # empty id, filtered
        ],
    })
    parsed = CatalogSnapshot.from_json(payload)
    assert parsed is not None
    assert len(parsed.models) == 1
    assert parsed.models[0].model_id == "good/model-7B"


# ===========================================================================
# §6 — Disk cache (atomic write/read)
# ===========================================================================


def test_save_and_load_roundtrip(isolated_cache: Path) -> None:
    snap = CatalogSnapshot(
        fetched_at_unix=time.time(),
        models=(ModelCard(
            model_id="x/y-3B", family="x", parameter_count_b=3.0,
            context_window=None, pricing_in_per_m_usd=None,
            pricing_out_per_m_usd=None, supports_streaming=True,
            raw_metadata_json="{}",
        ),),
    )
    save_snapshot(snap)
    loaded = load_cached_snapshot()
    assert loaded is not None
    assert len(loaded.models) == 1
    assert loaded.models[0].model_id == "x/y-3B"


def test_load_missing_cache_returns_none(isolated_cache: Path) -> None:
    """No file → None, no exception."""
    assert load_cached_snapshot() is None


def test_load_corrupt_cache_returns_none(isolated_cache: Path) -> None:
    """Corrupt JSON → None, no exception (caller treats as cache-miss)."""
    isolated_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_cache.write_text("{this is not valid json", encoding="utf-8")
    assert load_cached_snapshot() is None


def test_save_creates_parent_dirs(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Atomic write must create the parent directory tree."""
    deep = tmp_path / "a" / "b" / "c" / "dw_catalog.json"
    monkeypatch.setenv("JARVIS_DW_CATALOG_PATH", str(deep))
    snap = CatalogSnapshot(fetched_at_unix=1.0, models=())
    save_snapshot(snap)
    assert deep.exists()


# ===========================================================================
# §7 — DwCatalogClient.fetch() — clean fetch
# ===========================================================================


@pytest.mark.asyncio
async def test_fetch_openai_envelope(isolated_cache: Path) -> None:
    body = {
        "data": [
            {"id": "Qwen/Qwen3.5-397B-A17B"},
            {"id": "moonshotai/Kimi-K2.6"},
            {"id": "Qwen/Qwen3.5-9B",
             "pricing": {"input": 0.04, "output": 0.06}},
        ],
    }
    client = _client(_mock_session(body))
    snap = await client.fetch()
    assert snap.fetch_failure_reason is None
    assert len(snap.models) == 3
    assert snap.models[0].parameter_count_b == 397.0
    assert snap.models[1].has_ambiguous_metadata() is True
    assert snap.models[2].pricing_out_per_m_usd == 0.06
    # Disk cache populated
    assert load_cached_snapshot() is not None


@pytest.mark.asyncio
async def test_fetch_bare_list_envelope(isolated_cache: Path) -> None:
    """Some servers return a bare list (non-OpenAI shape) — still works."""
    body = [{"id": "fake/model-7B"}]
    client = _client(_mock_session(body))
    snap = await client.fetch()
    assert snap.fetch_failure_reason is None
    assert len(snap.models) == 1


@pytest.mark.asyncio
async def test_fetch_22_models_simulation(isolated_cache: Path) -> None:
    """Mirrors the 22-model count surfaced in bt-2026-04-27-235708 soak.
    Pins that the parser tolerates a realistic catalog size."""
    body = {"data": [
        {"id": f"vendor/model-{i}-{(i*3) % 50 + 1}B"}
        for i in range(22)
    ]}
    client = _client(_mock_session(body))
    snap = await client.fetch()
    assert len(snap.models) == 22


# ===========================================================================
# §8 — fetch() failure paths — never raise
# ===========================================================================


@pytest.mark.asyncio
async def test_fetch_http_5xx(isolated_cache: Path) -> None:
    """HTTP 500 → failure reason populated, NEVER raises."""
    client = _client(_mock_session(json_body={}, status=500))
    snap = await client.fetch()
    assert snap.fetch_failure_reason == "http_500"
    assert snap.models == ()


@pytest.mark.asyncio
async def test_fetch_http_429_rate_limit(isolated_cache: Path) -> None:
    client = _client(_mock_session(json_body={}, status=429))
    snap = await client.fetch()
    assert snap.fetch_failure_reason == "http_429"


@pytest.mark.asyncio
async def test_fetch_timeout(isolated_cache: Path) -> None:
    client = _client(_mock_session(raise_exc=asyncio.TimeoutError()))
    snap = await client.fetch()
    assert snap.fetch_failure_reason == "timeout"
    assert snap.models == ()


@pytest.mark.asyncio
async def test_fetch_transport_error(isolated_cache: Path) -> None:
    """Generic transport-level exception (DNS, conn reset, etc.)."""
    client = _client(_mock_session(raise_exc=RuntimeError("connection reset")))
    snap = await client.fetch()
    assert snap.fetch_failure_reason is not None
    assert "RuntimeError" in snap.fetch_failure_reason


@pytest.mark.asyncio
async def test_fetch_malformed_json_body(isolated_cache: Path) -> None:
    """``body`` is not a dict or list → empty snapshot with reason
    NOT raising on the parse path. (We test the parser separately;
    here we ensure the client returns something usable.)"""
    client = _client(_mock_session(json_body="just a string"))
    snap = await client.fetch()
    # Body wasn't a dict/list, parser returned () — but fetch
    # itself succeeded (200 OK), so this is a "valid empty" outcome.
    assert snap.fetch_failure_reason is None
    assert snap.models == ()


# ===========================================================================
# §9 — fetch() failure with no cache → empty snapshot, not exception
# ===========================================================================


@pytest.mark.asyncio
async def test_fetch_failure_no_cache_returns_empty(
    isolated_cache: Path,
) -> None:
    """First-ever fetch fails, no cache exists yet → empty snapshot."""
    assert not isolated_cache.exists()
    client = _client(_mock_session(raise_exc=RuntimeError("conn refused")))
    snap = await client.fetch()
    assert snap.fetch_failure_reason is not None
    assert snap.models == ()


@pytest.mark.asyncio
async def test_fetch_failure_with_cache_returns_cached(
    isolated_cache: Path,
) -> None:
    """Cached snapshot exists, live fetch fails → return cache with
    failure_reason annotated."""
    # Pre-populate cache
    cached = CatalogSnapshot(
        fetched_at_unix=1234567890.0,
        models=(ModelCard(
            model_id="cached/model-7B", family="cached",
            parameter_count_b=7.0, context_window=None,
            pricing_in_per_m_usd=None, pricing_out_per_m_usd=None,
            supports_streaming=True, raw_metadata_json="{}",
        ),),
    )
    save_snapshot(cached)

    client = _client(_mock_session(raise_exc=RuntimeError("dns failure")))
    snap = await client.fetch()
    # Failure reason populated, but models came from cache
    assert snap.fetch_failure_reason is not None
    assert len(snap.models) == 1
    assert snap.models[0].model_id == "cached/model-7B"
    # fetched_at_unix preserved from cache (not the failure moment)
    assert snap.fetched_at_unix == 1234567890.0


# ===========================================================================
# §10 — cached() + stale()
# ===========================================================================


@pytest.mark.asyncio
async def test_cached_returns_none_with_no_cache(
    isolated_cache: Path,
) -> None:
    client = _client(_mock_session({"data": []}))
    assert client.cached() is None


@pytest.mark.asyncio
async def test_cached_loads_from_disk_on_first_call(
    isolated_cache: Path,
) -> None:
    snap = CatalogSnapshot(
        fetched_at_unix=time.time(), models=(),
    )
    save_snapshot(snap)
    client = _client(_mock_session({"data": []}))
    loaded = client.cached()
    assert loaded is not None


@pytest.mark.asyncio
async def test_cached_uses_memory_after_fetch(
    isolated_cache: Path,
) -> None:
    """After fetch(), cached() returns the in-memory snapshot."""
    body = {"data": [{"id": "x/model-7B"}]}
    client = _client(_mock_session(body))
    fetched = await client.fetch()
    assert client.cached() is fetched  # same object identity


def test_stale_when_no_cache(isolated_cache: Path) -> None:
    client = _client(_mock_session({"data": []}))
    assert client.stale() is True


def test_stale_returns_false_for_fresh_snapshot(
    isolated_cache: Path,
) -> None:
    snap = CatalogSnapshot(fetched_at_unix=time.time(), models=())
    save_snapshot(snap)
    client = _client(_mock_session({"data": []}))
    assert client.stale(max_age_s=3600) is False


def test_stale_returns_true_for_old_snapshot(
    isolated_cache: Path,
) -> None:
    snap = CatalogSnapshot(
        fetched_at_unix=time.time() - 10000, models=(),
    )
    save_snapshot(snap)
    client = _client(_mock_session({"data": []}))
    assert client.stale(max_age_s=3600) is True


# ===========================================================================
# §11 — Source-level pins
# ===========================================================================


def test_source_fetch_uses_try_except() -> None:
    """fetch() must wrap network calls in try/except — Zero-Trust §4
    requires it never raise to the caller. Pins the contract source-
    level so a future refactor can't silently drop the safety net."""
    import inspect
    src = inspect.getsource(DwCatalogClient.fetch)
    assert "try:" in src
    assert "except asyncio.TimeoutError" in src
    assert "except Exception" in src
    # Both failure paths must call the fallback helper (returns
    # cached snapshot or empty snapshot — never raises).
    assert "_failure_fallback" in src


def test_source_failure_fallback_prefers_cache() -> None:
    """The failure-fallback helper must check the cache before
    returning an empty snapshot. Pins Zero-Trust §4: ``Catalog API
    down → fall back to a cached catalog``."""
    import inspect
    src = inspect.getsource(DwCatalogClient._failure_fallback)
    cache_idx = src.index("self.cached()")
    empty_idx = src.index("models=()")
    assert cache_idx < empty_idx, (
        "Failure fallback must check cache before returning empty"
    )


def test_source_no_top_level_yaml_imports() -> None:
    """Slice A is catalog discovery — MUST NOT import provider_topology
    or YAML helpers (those are Slice C wiring concerns). Check only
    actual ``import`` lines, not docstring mentions."""
    import inspect
    src = inspect.getsource(dcc)
    import_lines = [
        ln for ln in src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    blob = "\n".join(import_lines)
    assert "provider_topology" not in blob
    assert "brain_selection_policy" not in blob
    assert "topology_sentinel" not in blob
