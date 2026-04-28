"""Phase 12 Slice E — graduation pin suite.

Pins the graduated state of the Phase 12 catalog discovery pipeline.
Two flags flip together:

  1. JARVIS_DW_CATALOG_DISCOVERY_ENABLED   — does discovery RUN?
  2. JARVIS_DW_CATALOG_AUTHORITATIVE        — does dispatcher READ holder?

Pin sections:
  §1 All defaults are True (delenv → True)
  §2 Empty-string env reads as default-True (the unset marker)
  §3 Each ``"false"``-class override returns False (hot-revert path)
  §4 Full-revert matrix — flipping each flag independently
  §5 YAML purge invariant — dw_models arrays are empty for generative routes
  §6 YAML policy fields preserved — fallback_tolerance, dw_allowed,
                                     block_mode, reason all still YAML
  §7 Boot-hook idempotence — second call in same process is a no-op
  §8 Boot-hook respects discovery flag — disabled → returns None
  §9 Boot-hook NEVER raises on transport failure
  §10 Module-level public API surface — graduated readers callable
"""
from __future__ import annotations

import os  # noqa: F401
from pathlib import Path
from typing import Any  # noqa: F401
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import (
    dw_catalog_client as dcc,
    dw_discovery_runner as ddr,
    provider_topology as pt,
)
from backend.core.ouroboros.governance.dw_catalog_client import (
    discovery_enabled,
)
from backend.core.ouroboros.governance.provider_topology import (
    catalog_authoritative_enabled,
    clear_dynamic_catalog,
    get_topology,
)


# Centralized list of all 2 graduated flags + their reader callable.
_GRADUATED_FLAGS = [
    ("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", discovery_enabled),
    ("JARVIS_DW_CATALOG_AUTHORITATIVE", catalog_authoritative_enabled),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate():
    clear_dynamic_catalog()
    ddr.reset_boot_state_for_tests()
    yield
    clear_dynamic_catalog()
    ddr.reset_boot_state_for_tests()


@pytest.fixture
def isolated_cache(tmp_path: Path,
                   monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "dw_catalog.json"
    monkeypatch.setenv("JARVIS_DW_CATALOG_PATH", str(cache))
    return cache


@pytest.fixture
def isolated_ledger_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    p = tmp_path / "ledger.json"
    monkeypatch.setenv("JARVIS_DW_PROMOTION_LEDGER_PATH", str(p))
    return p


def _mock_session(json_body: Any = None, status: int = 200,
                  raise_exc=None) -> Any:
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


# ===========================================================================
# §1 — All defaults are True
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_default_is_true_when_env_unset(
    env_name: str, reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delenv → reader returns True."""
    monkeypatch.delenv(env_name, raising=False)
    assert reader() is True, (
        f"Slice E graduation: {env_name} must default True"
    )


# ===========================================================================
# §2 — Empty string is the unset marker
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_empty_string_reads_as_default_true(
    env_name: str, reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setenv("", "")`` matches delenv. Operators commonly clear via
    shell ``export FOO=`` (which sets to empty string)."""
    monkeypatch.setenv(env_name, "")
    assert reader() is True


# ===========================================================================
# §3 — Hot-revert: each false-class string returns False
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
@pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "FALSE"])
def test_false_class_string_reverts(
    env_name: str, reader, falsy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(env_name, falsy)
    assert reader() is False, (
        f"{env_name}={falsy!r} should disable the feature"
    )


# ===========================================================================
# §4 — Full-revert matrix
# ===========================================================================


def test_full_revert_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip each flag off in turn; verify ONLY that one flag flips
    while the other stays True. Catches accidental cross-coupling."""
    for env_off, _ in _GRADUATED_FLAGS:
        # Reset all to default (delenv) — graduated state
        for env_name, _r in _GRADUATED_FLAGS:
            monkeypatch.delenv(env_name, raising=False)
        # Flip exactly one off
        monkeypatch.setenv(env_off, "false")
        # Verify the flipped one is off, the rest still True
        for env_name, reader in _GRADUATED_FLAGS:
            expected = (env_name != env_off)
            actual = reader()
            assert actual is expected, (
                f"After flipping {env_off}=false, {env_name} reader "
                f"returned {actual} (expected {expected})"
            )


# ===========================================================================
# §5 — YAML purge invariant
# ===========================================================================


def test_yaml_dw_models_purged_for_generative_routes() -> None:
    """Slice E PURGE: dw_models arrays in YAML are () for the four
    generative routes. The catalog supplies them dynamically."""
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    for route in ("standard", "complex", "background", "speculative"):
        # Pre-graduation YAML had non-empty lists. Post-graduation:
        # empty (catalog is the source of truth).
        # We can't read effective_dw_models here directly because
        # the catalog might be populated. Instead, verify the YAML
        # entry's raw dw_models field is empty.
        entry = topo.routes.get(route)
        assert entry is not None, f"route {route} missing from yaml"
        assert entry.dw_models == (), (
            f"Slice E purge: yaml's dw_models for {route!r} should be "
            f"empty post-graduation, got {entry.dw_models}"
        )


def test_yaml_immediate_route_stays_empty() -> None:
    """IMMEDIATE was empty pre-purge AND post-purge — Manifesto §5
    Claude-direct route. Pinned to prevent accidental re-population."""
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    entry = topo.routes.get("immediate")
    assert entry is not None
    assert entry.dw_models == ()


# ===========================================================================
# §6 — YAML policy fields preserved (fallback_tolerance / block_mode / ...)
# ===========================================================================


def test_yaml_fallback_tolerance_preserved() -> None:
    """Cost contract policy: fallback_tolerance stays YAML-authored
    even after the dw_models purge. BG/SPEC must still be ``queue``;
    STANDARD/COMPLEX must still be ``cascade_to_claude``."""
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    assert topo.fallback_tolerance_for_route("background") == "queue"
    assert topo.fallback_tolerance_for_route("speculative") == "queue"
    assert (
        topo.fallback_tolerance_for_route("complex")
        == "cascade_to_claude"
    )
    assert (
        topo.fallback_tolerance_for_route("standard")
        == "cascade_to_claude"
    )


def test_yaml_dw_allowed_preserved() -> None:
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    # All routes block legacy DW cascade — operator-controlled gate
    # untouched by Slice E
    for route in ("standard", "complex", "background", "speculative",
                  "immediate"):
        assert topo.dw_allowed_for_route(route) is False, (
            f"dw_allowed for {route} should still be False post-purge"
        )


def test_yaml_block_mode_preserved() -> None:
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    assert topo.block_mode_for_route("background") == "skip_and_queue"
    assert topo.block_mode_for_route("speculative") == "skip_and_queue"
    assert topo.block_mode_for_route("standard") == "cascade_to_claude"
    assert topo.block_mode_for_route("complex") == "cascade_to_claude"


def test_yaml_reason_strings_present() -> None:
    """``reason`` strings remain populated — they're operator-facing
    documentation. Slice E refreshed them; pin they're non-empty."""
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    for route in ("standard", "complex", "background", "speculative"):
        reason = topo.reason_for_route(route)
        assert reason and len(reason) > 10, (
            f"reason for {route!r} suspiciously short: {reason!r}"
        )


# ===========================================================================
# §7 — Boot-hook idempotence
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_discovery_only_runs_once(
    isolated_cache: Path, isolated_ledger_path: Path,
) -> None:
    body = {"data": [
        {"id": "vendor/m-50B", "parameter_count_b": 50,
         "pricing": {"output": 0.5}},
    ]}
    session = _mock_session(body)
    first = await ddr.boot_discovery_once(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
    )
    assert first is not None
    assert first.ok is True
    assert first.model_count == 1

    # Second call must be a no-op (idempotent)
    second = await ddr.boot_discovery_once(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
    )
    assert second is None  # idempotent skip marker


@pytest.mark.asyncio
async def test_boot_discovery_resets_via_test_helper(
    isolated_cache: Path, isolated_ledger_path: Path,
) -> None:
    """``reset_boot_state_for_tests`` clears the boot flag so a
    subsequent call runs again. Production code MUST NOT call this;
    test pin only."""
    session = _mock_session({"data": []})
    first = await ddr.boot_discovery_once(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
    )
    assert first is not None
    ddr.reset_boot_state_for_tests()
    # After reset, boot fires again
    second = await ddr.boot_discovery_once(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
    )
    assert second is not None


# ===========================================================================
# §8 — Boot-hook respects discovery flag
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_discovery_skipped_when_disabled(
    isolated_cache: Path, isolated_ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot-revert path: discovery off → boot returns None, never
    issues the /models GET, never spawns refresh task."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_DISCOVERY_ENABLED", "false")
    session = _mock_session({"data": []})
    result = await ddr.boot_discovery_once(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
    )
    assert result is None


# ===========================================================================
# §9 — Boot-hook NEVER raises
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_discovery_handles_transport_failure(
    isolated_cache: Path, isolated_ledger_path: Path,
) -> None:
    """Transport-level exception during /models GET → boot still
    returns a DiscoveryResult with failure_reason populated. NEVER
    raises to caller (dispatcher safety pin)."""
    session = _mock_session(raise_exc=RuntimeError("conn refused"))
    result = await ddr.boot_discovery_once(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
    )
    assert result is not None
    assert result.ok is False
    assert result.fetch_failure_reason is not None


@pytest.mark.asyncio
async def test_boot_discovery_handles_5xx(
    isolated_cache: Path, isolated_ledger_path: Path,
) -> None:
    session = _mock_session(json_body={}, status=503)
    result = await ddr.boot_discovery_once(
        session=session,
        base_url="https://api.doubleword.ai",
        api_key="test-key",
    )
    assert result is not None
    assert result.ok is False
    assert result.fetch_failure_reason == "http_503"


# ===========================================================================
# §10 — Module-level public API surface
# ===========================================================================


def test_module_level_readers_callable_with_no_args() -> None:
    """Each reader is a zero-arg callable returning bool. Pins the
    contract that the dispatcher relies on for read-time flag
    introspection (no captured-at-init values that go stale on
    hot-revert)."""
    for _, reader in _GRADUATED_FLAGS:
        result = reader()
        assert isinstance(result, bool)


def test_discovery_default_docstring_references_graduation() -> None:
    """The reader's docstring must call out the Slice E graduation
    flip — operator-facing documentation is the surface that
    explains why the default changed."""
    assert discovery_enabled.__doc__ is not None
    assert "Slice E" in discovery_enabled.__doc__
    assert "true" in discovery_enabled.__doc__.lower()


def test_authoritative_default_docstring_references_graduation() -> None:
    assert catalog_authoritative_enabled.__doc__ is not None
    assert "Slice E" in catalog_authoritative_enabled.__doc__


def test_boot_discovery_once_in_module_exports() -> None:
    """The boot hook must be in __all__ — pinning the public API
    surface so future refactors don't accidentally hide it."""
    assert "boot_discovery_once" in ddr.__all__
    assert "reset_boot_state_for_tests" in ddr.__all__
