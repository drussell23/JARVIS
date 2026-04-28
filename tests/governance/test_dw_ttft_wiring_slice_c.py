"""Phase 12.2 Slice C — TTFT wiring regression spine.

Wires the Slice B ``TtftObserver`` into the Phase 12 cognitive
substrate: ``PromotionLedger`` (gate replacement), ``DwCatalogClassifier``
(cold-storage demotion), ``dw_discovery_runner`` (singleton + thread-
through), and ``DoublewordProvider`` (first-chunk recording site).

Pins:
  §1  ``ttft_demotion_enabled()`` flag — default false; case-tolerance
  §2  PromotionLedger gate — TTFT mode bypasses count when flag on
  §3  PromotionLedger gate — flag-off legacy count gate preserved
  §4  PromotionLedger gate — observer=None falls through to legacy
  §5  PromotionLedger gate — broken observer faults fall through
  §6  DwCatalogClassifier — cold-storage demotes to SPECULATIVE only
  §7  DwCatalogClassifier — auto-recovery on next stable observation
  §8  DwCatalogClassifier — flag-off ignores cold-storage signal
  §9  DwCatalogClassifier — NON_CHAT trumps cold-storage
  §10 DwCatalogClassifier — broken observer doesn't take down classify
  §11 dw_discovery_runner — TtftObserver singleton + reset
  §12 dw_discovery_runner — observer threaded through classify()
  §13 dw_discovery_runner — get_ttft_observer() public accessor
  §14 Authority invariants — Slice C never mutates outside its surface
"""
from __future__ import annotations

import time
from typing import Any, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.dw_catalog_classifier import (
    DwCatalogClassifier,
)
from backend.core.ouroboros.governance.dw_catalog_client import (
    CatalogSnapshot, ModelCard,
)
from backend.core.ouroboros.governance.dw_promotion_ledger import (
    PromotionLedger,
)
from backend.core.ouroboros.governance.dw_ttft_observer import (
    TtftObserver, ttft_demotion_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch) -> PromotionLedger:
    monkeypatch.setenv(
        "JARVIS_DW_PROMOTION_LEDGER_PATH",
        str(tmp_path / "ledger.json"),
    )
    led = PromotionLedger()
    led.load()
    return led


@pytest.fixture
def isolated_observer(tmp_path, monkeypatch) -> TtftObserver:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_TTFT_STATE_PATH",
        str(tmp_path / "ttft.json"),
    )
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "true")
    obs = TtftObserver()
    obs.load()
    return obs


def _make_card(
    model_id: str,
    *,
    params_b: Optional[float] = 30.0,
    out_price: Optional[float] = 1.0,
) -> ModelCard:
    return ModelCard(
        model_id=model_id,
        family=model_id.split("/")[0] if "/" in model_id else "unknown",
        parameter_count_b=params_b,
        context_window=128_000,
        pricing_in_per_m_usd=0.5 if out_price is not None else None,
        pricing_out_per_m_usd=out_price,
        supports_streaming=True,
        raw_metadata_json="{}",
    )


def _make_snapshot(*cards: ModelCard) -> CatalogSnapshot:
    return CatalogSnapshot(
        models=tuple(cards),
        fetched_at_unix=time.time(),
        fetch_latency_ms=10,
        fetch_failure_reason=None,
    )


# ---------------------------------------------------------------------------
# §1 — ttft_demotion_enabled flag
# ---------------------------------------------------------------------------


def test_demotion_flag_default_false(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", raising=False)
    assert ttft_demotion_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "YES", "on", "On"])
def test_demotion_flag_truthy_values(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", val)
    assert ttft_demotion_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", "", " "])
def test_demotion_flag_falsy_values(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", val)
    assert ttft_demotion_enabled() is False


# ---------------------------------------------------------------------------
# §2-§5 — PromotionLedger gate dispatch
# ---------------------------------------------------------------------------


def test_gate_ttft_mode_when_flag_on(
    isolated_ledger: PromotionLedger,
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """When TTFT flag on AND observer provided, gate defers to observer.
    Even with NO count history, a TTFT-ready model graduates."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    isolated_ledger.register_quarantine("vendor/m-7B")
    # Feed observer with consistent low-variance TTFT samples.
    # 4 samples of 100±2ms → CV ≈ 0.02, rel_SEM ≈ 0.01 → both well
    # below default thresholds (0.15, 0.05).
    for ms in (100, 102, 99, 101, 100, 102):
        isolated_observer.record_ttft("vendor/m-7B", ms)
    assert isolated_observer.is_promotion_ready("vendor/m-7B") is True
    # Legacy count gate: 0/10 successes → would say no
    assert isolated_ledger.is_eligible_for_promotion("vendor/m-7B") is False
    # TTFT gate: observer says yes → eligible
    eligible = isolated_ledger.is_eligible_for_promotion(
        "vendor/m-7B", observer=isolated_observer,
    )
    assert eligible is True


def test_gate_legacy_count_when_flag_off(
    isolated_ledger: PromotionLedger,
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """Flag off → observer ignored, legacy count gate runs."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "3")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "200")
    isolated_ledger.register_quarantine("vendor/m-7B")
    # Feed observer with TTFT-ready samples
    for ms in (100, 102, 99, 101):
        isolated_observer.record_ttft("vendor/m-7B", ms)
    # Legacy count requires 3 successes + max-latency check; not done.
    assert isolated_ledger.is_eligible_for_promotion(
        "vendor/m-7B", observer=isolated_observer,
    ) is False
    # Now feed legacy gate
    for ms in (100, 102, 99):
        isolated_ledger.record_success("vendor/m-7B", ms)
    # Legacy gate satisfied
    assert isolated_ledger.is_eligible_for_promotion(
        "vendor/m-7B", observer=isolated_observer,
    ) is True


def test_gate_observer_none_falls_through_to_legacy(
    isolated_ledger: PromotionLedger,
    monkeypatch,
) -> None:
    """observer=None → legacy gate regardless of flag."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "2")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "200")
    isolated_ledger.register_quarantine("vendor/m-7B")
    isolated_ledger.record_success("vendor/m-7B", 100)
    isolated_ledger.record_success("vendor/m-7B", 110)
    assert isolated_ledger.is_eligible_for_promotion(
        "vendor/m-7B", observer=None,
    ) is True


def test_gate_broken_observer_falls_through_to_legacy(
    isolated_ledger: PromotionLedger,
    monkeypatch,
) -> None:
    """Observer that raises on is_promotion_ready → legacy gate.
    Defensive try/except must not let a faulty observer break the gate."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "2")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "200")
    isolated_ledger.register_quarantine("vendor/m-7B")
    isolated_ledger.record_success("vendor/m-7B", 100)
    isolated_ledger.record_success("vendor/m-7B", 110)

    class _BrokenObs:
        def is_promotion_ready(self, mid):  # noqa: ARG002
            raise RuntimeError("observer faulted")

    # Falls through to legacy → succeeds because count gate is satisfied.
    assert isolated_ledger.is_eligible_for_promotion(
        "vendor/m-7B", observer=_BrokenObs(),
    ) is True


def test_promote_threads_observer_kwarg(
    isolated_ledger: PromotionLedger,
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """promote() forwards observer to is_eligible_for_promotion."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    isolated_ledger.register_quarantine("vendor/m-7B")
    for ms in (100, 102, 99, 101, 100):
        isolated_observer.record_ttft("vendor/m-7B", ms)
    assert isolated_ledger.promote(
        "vendor/m-7B", observer=isolated_observer,
    ) is True
    assert isolated_ledger.is_promoted("vendor/m-7B") is True


# ---------------------------------------------------------------------------
# §6-§10 — DwCatalogClassifier cold-storage demotion
# ---------------------------------------------------------------------------


def test_classifier_cold_storage_demotes_to_speculative(
    isolated_ledger: PromotionLedger,
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """Model in cold-storage state is excluded from BG/STANDARD/COMPLEX
    and admitted to SPECULATIVE only."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    # Build a stable mean of ~100ms then a cold-storage spike at 1000ms
    for ms in (100, 102, 99, 101, 100):
        isolated_observer.record_ttft("vendor/m-7B", ms)
    isolated_observer.record_ttft("vendor/m-7B", 1000)
    assert isolated_observer.is_cold_storage("vendor/m-7B") is True

    snap = _make_snapshot(
        _make_card("vendor/m-7B", params_b=7.0, out_price=0.05),
    )
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(
        snap, isolated_ledger, ttft_observer=isolated_observer,
    )
    # Cold storage → SPECULATIVE only
    assert "vendor/m-7B" in outcome.for_route("speculative")
    assert "vendor/m-7B" not in outcome.for_route("background")
    assert "vendor/m-7B" not in outcome.for_route("standard")
    assert "vendor/m-7B" not in outcome.for_route("complex")


def test_classifier_cold_storage_auto_recovery(
    isolated_ledger: PromotionLedger,
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """Once TTFT normalizes, cold-storage demotion lifts on next classify."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    for ms in (100, 102, 99, 101, 100):
        isolated_observer.record_ttft("vendor/m-7B", ms)
    isolated_observer.record_ttft("vendor/m-7B", 1000)  # spike
    assert isolated_observer.is_cold_storage("vendor/m-7B") is True

    # Now feed a normal sample → spike no longer "latest", spike still
    # in mean but the test on `latest > mean+2σ` returns False.
    isolated_observer.record_ttft("vendor/m-7B", 105)
    assert isolated_observer.is_cold_storage("vendor/m-7B") is False

    snap = _make_snapshot(
        _make_card("vendor/m-7B", params_b=7.0, out_price=0.05),
    )
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(
        snap, isolated_ledger, ttft_observer=isolated_observer,
    )
    # No demotion → BACKGROUND eligible (cheap + small)
    assert "vendor/m-7B" in outcome.for_route("background")


def test_classifier_flag_off_ignores_cold_storage(
    isolated_ledger: PromotionLedger,
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """When demotion flag off, cold-storage signal is ignored."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "false")
    for ms in (100, 102, 99, 101, 100):
        isolated_observer.record_ttft("vendor/m-7B", ms)
    isolated_observer.record_ttft("vendor/m-7B", 1000)
    # Observer still SAYS it's cold storage…
    assert isolated_observer.is_cold_storage("vendor/m-7B") is True

    snap = _make_snapshot(
        _make_card("vendor/m-7B", params_b=7.0, out_price=0.05),
    )
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(
        snap, isolated_ledger, ttft_observer=isolated_observer,
    )
    # …but classifier ignores it under flag-off and routes normally
    assert "vendor/m-7B" in outcome.for_route("background")


def test_classifier_non_chat_trumps_cold_storage(
    isolated_ledger: PromotionLedger,
    isolated_observer: TtftObserver,
    monkeypatch,
) -> None:
    """A NON_CHAT modality verdict excludes from EVERY route — even
    SPECULATIVE — regardless of cold-storage state."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    for ms in (100, 102, 99, 101, 100):
        isolated_observer.record_ttft("vendor/embed-1B", ms)
    isolated_observer.record_ttft("vendor/embed-1B", 1000)
    assert isolated_observer.is_cold_storage("vendor/embed-1B") is True

    # Mock modality ledger that says NON_CHAT
    mod_ledger = MagicMock()
    mod_ledger.has_record = lambda mid: True
    mod_ledger.is_non_chat = lambda mid: True
    mod_ledger.is_unknown = lambda mid: False

    snap = _make_snapshot(
        _make_card("vendor/embed-1B", params_b=1.0, out_price=0.02),
    )
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(
        snap, isolated_ledger,
        modality_ledger=mod_ledger,
        ttft_observer=isolated_observer,
    )
    # Excluded from EVERY route — NON_CHAT is hard gate
    for route in ("speculative", "background", "standard", "complex"):
        assert "vendor/embed-1B" not in outcome.for_route(route)


def test_classifier_broken_observer_doesnt_break_classify(
    isolated_ledger: PromotionLedger,
    monkeypatch,
) -> None:
    """Observer that raises on cold_storage_models() → classify()
    proceeds without cold-storage signal. Defense-in-depth."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")

    class _BrokenObs:
        def cold_storage_models(self):
            raise RuntimeError("observer faulted")

    snap = _make_snapshot(
        _make_card("vendor/m-7B", params_b=7.0, out_price=0.05),
    )
    classifier = DwCatalogClassifier()
    # Should NOT raise. Classifier continues with empty cold-storage set.
    outcome = classifier.classify(
        snap, isolated_ledger, ttft_observer=_BrokenObs(),
    )
    # Without the cold-storage signal, normal gates admit the model
    assert "vendor/m-7B" in outcome.for_route("background")


def test_classifier_observer_none_legacy_behavior(
    isolated_ledger: PromotionLedger,
) -> None:
    """ttft_observer=None preserves Phase 12 Slice G behavior bit-for-bit."""
    snap = _make_snapshot(
        _make_card("vendor/m-7B", params_b=7.0, out_price=0.05),
    )
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(
        snap, isolated_ledger, ttft_observer=None,
    )
    # Standard gates apply → eligible for BACKGROUND
    assert "vendor/m-7B" in outcome.for_route("background")


# ---------------------------------------------------------------------------
# §11-§13 — dw_discovery_runner integration
# ---------------------------------------------------------------------------


def test_get_ttft_observer_returns_none_when_tracking_off(
    monkeypatch, tmp_path,
) -> None:
    """Tracking flag off → singleton getter returns None."""
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "false")
    ddr.reset_boot_state_for_tests()
    assert ddr.get_ttft_observer() is None


def test_get_ttft_observer_singleton_when_tracking_on(
    monkeypatch, tmp_path,
) -> None:
    """Tracking flag on → returns same instance across calls."""
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_TTFT_STATE_PATH", str(tmp_path / "ttft.json"),
    )
    ddr.reset_boot_state_for_tests()
    obs1 = ddr.get_ttft_observer()
    obs2 = ddr.get_ttft_observer()
    assert obs1 is not None
    assert obs1 is obs2


def test_reset_boot_state_drops_observer_singleton(
    monkeypatch, tmp_path,
) -> None:
    """reset_boot_state_for_tests clears the observer singleton."""
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_TTFT_STATE_PATH", str(tmp_path / "ttft.json"),
    )
    ddr.reset_boot_state_for_tests()
    obs1 = ddr.get_ttft_observer()
    ddr.reset_boot_state_for_tests()
    obs2 = ddr.get_ttft_observer()
    assert obs1 is not None and obs2 is not None
    # Different instances — the reset dropped the first
    assert obs1 is not obs2


def test_run_discovery_threads_ttft_observer(
    monkeypatch, tmp_path,
) -> None:
    """Discovery runner accepts ttft_observer kwarg and threads it into
    classifier.classify()."""
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    import asyncio

    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DW_PROMOTION_LEDGER_PATH", str(tmp_path / "led.json"),
    )

    captured = {}

    class _SpyClassifier:
        def classify(self, snap, ledger, *,
                     modality_ledger=None, ttft_observer=None):
            captured["observer"] = ttft_observer
            from backend.core.ouroboros.governance.dw_catalog_classifier import (
                ClassificationOutcome,
                DwCatalogClassifier,
            )
            return DwCatalogClassifier().classify(
                snap, ledger,
                modality_ledger=modality_ledger,
                ttft_observer=ttft_observer,
            )

    fake_obs = MagicMock()
    fake_obs.cold_storage_models = MagicMock(return_value=())

    # Mock session that returns an empty catalog
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = MagicMock(
        return_value=_make_async(mock_resp),
    )
    mock_resp.__aexit__ = MagicMock(
        return_value=_make_async(None),
    )

    async def _empty_catalog():
        return {"data": []}

    mock_resp.json = _empty_catalog
    mock_session.get = MagicMock(return_value=_AsyncCM(mock_resp))

    led = PromotionLedger()
    led.load()

    asyncio.run(ddr.run_discovery(
        session=mock_session,
        base_url="https://test.example",
        api_key="test-key",
        ledger=led,
        cache_path=tmp_path / "cache.json",
        classifier=_SpyClassifier(),
        ttft_observer=fake_obs,
    ))

    assert captured.get("observer") is fake_obs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager wrapper for the mock session."""
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return None


async def _make_async(value):
    return value


# ---------------------------------------------------------------------------
# §14 — Authority invariants
# ---------------------------------------------------------------------------


def test_observer_never_mutates_ledger() -> None:
    """The Slice C wiring reads observer state — it never feeds back
    into the observer from the ledger / classifier surfaces. Authority
    invariant: observer is read-only from these consumers."""
    import inspect
    from backend.core.ouroboros.governance import dw_promotion_ledger
    from backend.core.ouroboros.governance import dw_catalog_classifier
    src1 = inspect.getsource(dw_promotion_ledger)
    src2 = inspect.getsource(dw_catalog_classifier)
    # No call to observer.record_ttft / observer.clear from these modules
    for src in (src1, src2):
        assert "observer.record_ttft" not in src
        assert "observer.clear(" not in src


def test_classifier_does_not_call_ledger_mutators() -> None:
    """Classifier reads ledger but never mutates it. Same invariant as
    Phase 12 Slice B — pure ranking function. Slice C must not break it.

    Walks the AST instead of grepping source text — docstring examples
    that mention ``ledger.register_quarantine`` are not real call sites."""
    import ast
    import inspect
    from backend.core.ouroboros.governance import dw_catalog_classifier
    src = inspect.getsource(dw_catalog_classifier)
    tree = ast.parse(src)
    forbidden = {
        "register_quarantine",
        "record_success",
        "record_failure",
        "promote",
        "demote",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func
            if (
                isinstance(target.value, ast.Name)
                and target.value.id == "ledger"
                and target.attr in forbidden
            ):
                raise AssertionError(
                    f"classifier calls forbidden ledger.{target.attr} "
                    f"at line {node.lineno}"
                )
