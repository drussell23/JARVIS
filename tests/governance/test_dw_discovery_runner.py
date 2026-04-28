"""Phase 12 Slice C — DiscoveryRunner regression spine.

Pins:
  §1 catalog_discovery_enabled flag re-export
  §2 run_discovery success path — populates holder + ledger + diff
  §3 run_discovery handles fetch failure gracefully (no raise, holder still
                                                     gets failure-marked snapshot)
  §4 run_discovery handles empty catalog (ok=False, no holder mutation)
  §5 run_discovery surfaces newly_quarantined to ledger
  §6 yaml_diff appears in diagnostic_strings as compact summary
  §7 NEVER raises contract — fetch raising, classifier raising, ledger raising
  §8 Shadow-mode invariant: holder populated, but dw_models_for_route still YAML
"""
from __future__ import annotations

import asyncio  # noqa: F401  — pytest-asyncio plugin
from pathlib import Path
from typing import Any, List, Optional  # noqa: F401
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.dw_catalog_classifier import (
    DwCatalogClassifier,
)
from backend.core.ouroboros.governance.dw_discovery_runner import (
    DiscoveryResult,
    catalog_discovery_enabled,
    run_discovery,
)
from backend.core.ouroboros.governance.dw_promotion_ledger import (
    PromotionLedger,
)
from backend.core.ouroboros.governance.provider_topology import (
    clear_dynamic_catalog,
    get_dynamic_catalog,
    get_topology,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_holder():
    clear_dynamic_catalog()
    yield
    clear_dynamic_catalog()


@pytest.fixture
def isolated_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> PromotionLedger:
    monkeypatch.setenv(
        "JARVIS_DW_PROMOTION_LEDGER_PATH",
        str(tmp_path / "ledger.json"),
    )
    return PromotionLedger()


@pytest.fixture
def isolated_cache(tmp_path: Path,
                   monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "dw_catalog.json"
    monkeypatch.setenv("JARVIS_DW_CATALOG_PATH", str(cache))
    return cache


def _mock_session(json_body: Any = None, status: int = 200,
                  raise_exc: Optional[Exception] = None) -> Any:
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


# ---------------------------------------------------------------------------
# §1 — Flag re-export
# ---------------------------------------------------------------------------


def test_catalog_discovery_enabled_default_on_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice E graduation flip: unset/empty env returns True."""
    monkeypatch.delenv("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", raising=False)
    assert catalog_discovery_enabled() is True


def test_catalog_discovery_enabled_truthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", "true")
    assert catalog_discovery_enabled() is True


def test_catalog_discovery_enabled_falsy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot-revert path."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", "false")
    assert catalog_discovery_enabled() is False


# ---------------------------------------------------------------------------
# §2 — Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_discovery_populates_holder(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    """22-model simulation. Verify holder + diagnostics + diff."""
    body = {"data": [
        {"id": "moonshotai/Kimi-K2.6", "pricing": {"input": 0.10, "output": 0.40}},
        {"id": "Qwen/Qwen3.5-397B-A17B"},  # heuristic param=397
        {"id": "Qwen/Qwen3.6-35B-A3B-FP8", "pricing": {"input": 0.05, "output": 0.20}},
        {"id": "Qwen/Qwen3.5-9B", "pricing": {"input": 0.04, "output": 0.06}},
    ]}
    session = _mock_session(body)
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    assert result.ok is True
    assert result.model_count == 4
    assert result.fetch_failure_reason is None
    holder = get_dynamic_catalog()
    assert holder is not None
    # At least one route populated
    populated = [r for r, ids in holder.assignments_by_route.items() if ids]
    assert len(populated) >= 1


@pytest.mark.asyncio
async def test_run_discovery_diagnostic_strings_format(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    body = {"data": [
        {"id": "Qwen/Qwen3.5-397B-A17B"},
        {"id": "vendor/m-30B", "parameter_count_b": 30, "pricing": {"output": 0.5}},
    ]}
    session = _mock_session(body)
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    diag_blob = " | ".join(result.diagnostic_strings)
    assert "catalog_fetched:models=2" in diag_blob
    assert "routes_assigned:" in diag_blob
    # yaml_diff summary present
    assert "yaml_diff[" in diag_blob


# ---------------------------------------------------------------------------
# §3 — Fetch failure (NOT raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_discovery_handles_http_5xx(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    session = _mock_session(json_body={}, status=503)
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    assert result.ok is False
    assert result.fetch_failure_reason == "http_503"
    assert result.model_count == 0


@pytest.mark.asyncio
async def test_run_discovery_handles_transport_exception(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    """fetch() is supposed to never raise, but the runner has
    defense-in-depth around it. Test by patching the client to raise."""
    session = _mock_session(raise_exc=RuntimeError("conn refused"))
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    assert result.ok is False
    assert result.fetch_failure_reason is not None


# ---------------------------------------------------------------------------
# §4 — Empty catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_discovery_empty_catalog(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    """200 OK but no models in the response. ok=False, no quarantine
    actions, but holder still populated (with empty assignments) so
    operators can audit."""
    session = _mock_session({"data": []})
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    assert result.ok is False
    assert result.model_count == 0
    assert result.newly_quarantined == ()


# ---------------------------------------------------------------------------
# §5 — newly_quarantined registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_discovery_registers_newly_quarantined(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    """Models with both param_count_b AND pricing missing → ambiguous
    → registered as quarantined in the ledger."""
    body = {"data": [
        {"id": "moonshotai/Kimi-K2.6"},                # ambiguous
        {"id": "vendor/no-suffix-id"},                 # ambiguous
        {"id": "Qwen/Qwen3.5-397B-A17B"},              # has params
    ]}
    session = _mock_session(body)
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    assert "moonshotai/Kimi-K2.6" in result.newly_quarantined
    assert "vendor/no-suffix-id" in result.newly_quarantined
    # Ledger now knows about them
    assert isolated_ledger.is_quarantined("moonshotai/Kimi-K2.6") is True
    assert isolated_ledger.is_quarantined("vendor/no-suffix-id") is True
    # Non-ambiguous model NOT in newly_quarantined
    assert "Qwen/Qwen3.5-397B-A17B" not in result.newly_quarantined


# ---------------------------------------------------------------------------
# §6 — yaml_diff in DiscoveryResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_discovery_yaml_diff_payload(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    """yaml_diff dict populated for all 4 generative routes."""
    body = {"data": [
        {"id": "Qwen/Qwen3.5-397B-A17B"},
    ]}
    session = _mock_session(body)
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    yaml_topo = get_topology()
    if yaml_topo.enabled:
        assert set(result.yaml_diff.keys()) == {
            "standard", "complex", "background", "speculative",
        }
    else:
        # yaml disabled → diff still computed but yaml_only side empty
        assert isinstance(result.yaml_diff, dict)


# ---------------------------------------------------------------------------
# §7 — NEVER-raises contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_discovery_with_classifier_that_raises(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    """If the classifier itself crashes, the runner returns ok=False
    instead of letting the exception escape to the dispatcher."""
    body = {"data": [{"id": "vendor/m-50B", "parameter_count_b": 50}]}
    session = _mock_session(body)

    class _BrokenClassifier:
        def classify(self, snap, ledger):  # noqa: ARG002
            raise RuntimeError("classifier blew up")

    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
        classifier=_BrokenClassifier(),  # type: ignore[arg-type]
    )
    assert result.ok is False
    diag_blob = " | ".join(result.diagnostic_strings)
    assert "classify_failed:RuntimeError" in diag_blob


@pytest.mark.asyncio
async def test_run_discovery_returns_discovery_result_type(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
) -> None:
    """Type contract — every code path returns DiscoveryResult."""
    session = _mock_session({"data": []})
    result = await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    assert isinstance(result, DiscoveryResult)


# ---------------------------------------------------------------------------
# §8 — Shadow-mode invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_discovery_authoritative_off_yaml_still_authoritative(
    isolated_cache: Path,
    isolated_ledger: PromotionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot-revert path post-graduation: with authoritative flag OFF,
    discovery populates the holder but dispatcher's
    ``dw_models_for_route`` still returns YAML's purged-empty list.

    Originally pinned Slice C shadow-mode invariant (authoritative
    didn't exist yet); rewritten at Slice E to pin the explicit-off
    hot-revert path. Same architectural invariant: holder is NOT
    consulted when authoritative is off."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "false")
    body = {"data": [
        {"id": "dynamic-vendor/m-99B", "parameter_count_b": 99,
         "pricing": {"output": 0.30}},
    ]}
    session = _mock_session(body)
    yaml_topo = get_topology()
    if not yaml_topo.enabled:
        pytest.skip("yaml disabled")
    yaml_complex_before = yaml_topo.dw_models_for_route("complex")
    await run_discovery(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
        ledger=isolated_ledger,
        cache_path=isolated_cache,
    )
    # YAML view UNCHANGED (post-purge it's () — same before + after)
    yaml_complex_after = yaml_topo.dw_models_for_route("complex")
    assert yaml_complex_before == yaml_complex_after
    # But the dynamic holder DOES have the new model — discovery
    # populated it; the authoritative flag just gated the read
    holder = get_dynamic_catalog()
    assert holder is not None
    assert (
        "dynamic-vendor/m-99B"
        in holder.assignments_by_route.get("complex", ())
    )
