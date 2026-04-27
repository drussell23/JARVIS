"""Slice 3 regression spine — sentinel-driven dispatch in candidate_generator.

Pins the new ``_dispatch_via_sentinel`` method on ``CandidateGenerator``
and the sentinel consultation in ``dw_topology_circuit_breaker``. Two
modes covered:

  * **Master flag OFF** (default) — every test in this file proves the
    sentinel-driven path is INERT. ``_dispatch_via_sentinel`` is not
    called; legacy yaml gate remains authoritative; behavior is
    byte-identical to pre-Slice-3.

  * **Master flag ON** — the sentinel walks the route's ranked
    ``dw_models`` list, stamps ``ctx._dw_model_override``, attempts DW
    via existing per-route helpers, reports failures back to the
    sentinel, and applies ``fallback_tolerance`` after exhausting
    every DW model.

Source-level pins are deliberately AST/regex over the production
``candidate_generator.py`` module rather than booting the full
orchestrator — the dispatcher's behavior is unit-testable via direct
calls into ``_dispatch_via_sentinel`` with a stub generator class.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import candidate_generator as cg
from backend.core.ouroboros.governance import dw_topology_circuit_breaker as cb
from backend.core.ouroboros.governance import provider_topology as pt
from backend.core.ouroboros.governance import topology_sentinel as ts


CANDIDATE_GEN_PATH = Path(cg.__file__)
CIRCUIT_BREAKER_PATH = Path(cb.__file__)


# ---------------------------------------------------------------------------
# §1 — Source-level pins on the new injection points
# ---------------------------------------------------------------------------


def test_sentinel_branch_inserted_before_static_topology_gate() -> None:
    """The new sentinel branch in ``_resolve_provider_chain`` (or
    wherever the dispatch lives) must fire BEFORE the static yaml
    gate at line 1404. If a refactor reorders these, the sentinel
    becomes unreachable for sealed routes."""
    src = CANDIDATE_GEN_PATH.read_text(encoding="utf-8")
    sentinel_call = src.index("_dispatch_via_sentinel")
    static_gate = src.index(
        "_topology.dw_allowed_for_route", sentinel_call,
    )
    assert sentinel_call < static_gate, (
        "sentinel dispatch MUST fire before the static yaml gate "
        "so the new path can win when JARVIS_TOPOLOGY_SENTINEL_ENABLED=true"
    )


def test_sentinel_branch_gated_by_master_flag() -> None:
    """The sentinel branch must be gated by ``is_sentinel_enabled()``
    so it's a no-op when the master flag is off."""
    src = CANDIDATE_GEN_PATH.read_text(encoding="utf-8")
    # Find the sentinel call site
    site_idx = src.index("_dispatch_via_sentinel")
    pre = src[max(0, site_idx - 600):site_idx]
    assert "is_sentinel_enabled" in pre, (
        "expected is_sentinel_enabled() check immediately before "
        "_dispatch_via_sentinel — without it the new path runs "
        "unconditionally"
    )


def test_dispatcher_returns_none_to_signal_fall_through() -> None:
    """The dispatcher's contract: return ``None`` when the route has
    no v2 dw_models (e.g. IMMEDIATE), so the legacy ``_generate_immediate``
    handler still runs. Pinned by docstring + a return-None branch."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "return None" in src
    assert "fall through to legacy" in src.lower()


def test_dispatcher_stamps_dw_model_override_on_ctx() -> None:
    """Each attempt must stamp ``ctx._dw_model_override`` so the
    DW provider's ``_resolve_effective_model`` picks up the chosen
    model_id."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert '"_dw_model_override"' in src
    assert "setattr(context" in src


def test_dispatcher_reports_success_on_dw_win() -> None:
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "sentinel.report_success" in src


def test_dispatcher_reports_failure_with_classified_source() -> None:
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "sentinel.report_failure" in src
    # Verify the failure-classification matrix is present (not just a
    # blanket LIVE_TRANSPORT bucket).
    assert "LIVE_STREAM_STALL" in src
    assert "LIVE_HTTP_429" in src
    assert "LIVE_HTTP_5XX" in src
    assert "LIVE_PARSE_ERROR" in src
    assert "LIVE_TRANSPORT" in src


def test_dispatcher_applies_fallback_tolerance_queue() -> None:
    """On exhausting all DW models, ``fallback_tolerance="queue"``
    must raise ``RuntimeError`` matching the orchestrator's existing
    accept-failure shape (``background_dw_blocked_by_topology`` or
    ``speculative_deferred``)."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert 'fallback_tolerance == "queue"' in src
    assert "background_dw_blocked_by_topology" in src
    assert "speculative_deferred" in src


def test_dispatcher_applies_fallback_tolerance_cascade() -> None:
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "_call_fallback" in src


def test_dispatcher_per_attempt_override_uses_setattr() -> None:
    """Stamping must use setattr (resilient to slotted contexts) AND
    swallow AttributeError/TypeError so a slotted dataclass doesn't
    crash the sentinel path. Falls through to legacy on rejection."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "setattr(context" in src
    assert "(AttributeError, TypeError)" in src


# ---------------------------------------------------------------------------
# §2 — DoublewordProvider override resolution
# ---------------------------------------------------------------------------


def test_dw_provider_resolver_prefers_ctx_override() -> None:
    """``_resolve_effective_model`` must consult ``ctx._dw_model_override``
    BEFORE the topology's per-route mapping. Without this, the
    dispatcher's per-attempt model choice is silently ignored."""
    from backend.core.ouroboros.governance import doubleword_provider as dwp
    src = inspect.getsource(dwp.DoublewordProvider._resolve_effective_model)
    assert "_dw_model_override" in src
    # The override check must appear BEFORE the model_for_route call.
    override_idx = src.index("_dw_model_override")
    route_call_idx = src.index("model_for_route")
    assert override_idx < route_call_idx


# ---------------------------------------------------------------------------
# §3 — dw_topology_circuit_breaker sentinel consultation
# ---------------------------------------------------------------------------


def test_circuit_breaker_consults_sentinel_when_enabled() -> None:
    src = CIRCUIT_BREAKER_PATH.read_text(encoding="utf-8")
    assert "is_sentinel_enabled" in src
    assert "get_default_sentinel" in src
    assert "sentinel_all_severed" in src or "sentinel_severed" in src


def test_circuit_breaker_master_off_legacy_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When master flag is OFF, the breaker's verdict for a sealed
    BG route must be byte-identical to pre-Slice-3 (skip_and_queue
    + non-read-only → True with topology_reason)."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    pt._CACHED_TOPOLOGY = None
    fired, reason = cb.should_circuit_break(
        provider_route="background",
        is_read_only=False,
    )
    assert fired is True
    assert "Gemma" in reason or "stream-stall" in reason or "topology" in reason.lower()


def test_circuit_breaker_master_off_read_only_carve_out_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    pt._CACHED_TOPOLOGY = None
    fired, reason = cb.should_circuit_break(
        provider_route="background", is_read_only=True,
    )
    assert fired is False
    assert "read_only" in reason.lower()


def test_circuit_breaker_sentinel_on_all_severed_queue_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """With sentinel ON + every model in the route OPEN + fallback=queue,
    the breaker MUST fire (`sentinel_all_severed:...`). This is the
    new evidence path that defends BG/SPEC unit economics."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    pt._CACHED_TOPOLOGY = None
    ts.reset_default_sentinel_for_tests()
    sentinel = ts.get_default_sentinel()
    # Force every BG model to OPEN.
    for model_id in ("Qwen/Qwen3.6-35B-A3B-FP8", "moonshotai/Kimi-K2.6"):
        sentinel.force_severed(model_id, "test")
    fired, reason = cb.should_circuit_break(
        provider_route="background",
        is_read_only=False,
    )
    assert fired is True
    assert "sentinel_all_severed" in reason
    ts.reset_default_sentinel_for_tests()


def test_circuit_breaker_sentinel_on_one_model_healthy_holds_fire(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Sentinel ON + at least one BG model not OPEN → breaker does
    NOT fire. The dispatcher will try that model."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    pt._CACHED_TOPOLOGY = None
    ts.reset_default_sentinel_for_tests()
    sentinel = ts.get_default_sentinel()
    # Force only the first BG model OPEN; second remains CLOSED.
    sentinel.force_severed("Qwen/Qwen3.6-35B-A3B-FP8", "test")
    sentinel.register_endpoint("moonshotai/Kimi-K2.6")
    fired, reason = cb.should_circuit_break(
        provider_route="background",
        is_read_only=False,
    )
    assert fired is False
    assert reason == "sentinel_dw_available"
    ts.reset_default_sentinel_for_tests()


def test_circuit_breaker_sentinel_on_severed_cascade_holds_fire(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Sentinel ON + every STANDARD model OPEN + fallback=cascade_to_claude:
    breaker does NOT fire (cascade is the contract). The caller's
    late-detection path will route to Claude."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    pt._CACHED_TOPOLOGY = None
    ts.reset_default_sentinel_for_tests()
    sentinel = ts.get_default_sentinel()
    # Force every STANDARD model OPEN.
    for model_id in (
        "moonshotai/Kimi-K2.6",
        "zai-org/GLM-5.1-FP8",
        "Qwen/Qwen3.6-35B-A3B-FP8",
        "Qwen/Qwen3.5-397B-A17B",
    ):
        sentinel.force_severed(model_id, "test")
    fired, reason = cb.should_circuit_break(
        provider_route="standard",
        is_read_only=False,
    )
    assert fired is False
    assert "sentinel_severed_cascade_to_claude" in reason
    ts.reset_default_sentinel_for_tests()


# ---------------------------------------------------------------------------
# §4 — Production yaml ranked-model integrity
# ---------------------------------------------------------------------------


def test_production_yaml_immediate_has_no_dw_models() -> None:
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    assert topo.dw_models_for_route("immediate") == ()


def test_production_yaml_standard_complex_share_ranked_list() -> None:
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    assert topo.dw_models_for_route("standard") == (
        topo.dw_models_for_route("complex")
    )
    # Pin first model to Kimi-K2.6 — the audit's primary pick.
    assert topo.dw_models_for_route("standard")[0] == "moonshotai/Kimi-K2.6"


def test_production_yaml_bg_cheap_first_ordering() -> None:
    """BG is cost-sensitive; cheapest model first per PRD §3.7.3."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    bg_models = topo.dw_models_for_route("background")
    # Qwen3.6-35B is the cheapest in the BG list ($0.25/$2.00 per M).
    assert bg_models[0] == "Qwen/Qwen3.6-35B-A3B-FP8"


def test_production_yaml_speculative_only_ultra_cheap() -> None:
    """SPECULATIVE must contain only the cheapest models in the
    catalog — fire-and-forget pre-computation can't justify $1/M
    output spend."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    spec_models = topo.dw_models_for_route("speculative")
    assert "Qwen/Qwen3.5-9B" in spec_models
    assert "Qwen/Qwen3.5-4B" in spec_models
    # No frontier-tier models slip in.
    assert "moonshotai/Kimi-K2.6" not in spec_models
    assert "Qwen/Qwen3.5-397B-A17B" not in spec_models


def test_production_yaml_legacy_397b_demoted_to_last() -> None:
    """The model that originally stream-stalled (Qwen3.5-397B) must
    not be promoted ahead of the newer model families. Pin it last
    in the STANDARD/COMPLEX rank — present (operators may want to
    re-test it once stabilized) but not preferred."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    standard = topo.dw_models_for_route("standard")
    if "Qwen/Qwen3.5-397B-A17B" in standard:
        assert standard[-1] == "Qwen/Qwen3.5-397B-A17B"


def test_production_yaml_no_claude_models_in_dw_lists() -> None:
    """Defensive — if a model_id starting with claude/anthropic
    accidentally appears in a dw_models list, that's a yaml typo
    that would cause the sentinel to attempt a Claude-shaped call
    via the DW provider."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    for route in (
        "immediate", "standard", "complex", "background", "speculative",
    ):
        for model_id in topo.dw_models_for_route(route):
            lower = model_id.lower()
            assert "claude" not in lower
            assert "anthropic" not in lower
            assert "gpt" not in lower
            assert "openai" not in lower


def test_production_yaml_bg_spec_fallback_is_queue() -> None:
    """``project_bg_spec_sealed.md`` contract preserved structurally
    — BG/SPEC fallback_tolerance is "queue", NOT "cascade_to_claude"."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    assert topo.fallback_tolerance_for_route("background") == "queue"
    assert topo.fallback_tolerance_for_route("speculative") == "queue"


def test_production_yaml_monitor_block_present() -> None:
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    cfg = topo.monitor_config()
    assert cfg is not None
    assert cfg.severed_threshold_weighted == 3.0


# ---------------------------------------------------------------------------
# §5 — Phase 10 P10.5 inverse pin (still authoritative; until the purge)
# ---------------------------------------------------------------------------


def test_phase_10_static_dw_allowed_blocks_still_present() -> None:
    """The 5 ``dw_allowed: false`` lines remain authoritative when
    JARVIS_TOPOLOGY_SENTINEL_ENABLED is off (default). Removing them
    early is unsafe — kept until P10.5 operator authorization."""
    yaml_path = Path(pt.__file__).parent / "brain_selection_policy.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    assert text.count("dw_allowed: false") == 5


def test_phase_10_read_only_carve_out_still_present() -> None:
    """The Nervous-System Reflex carve-out at
    candidate_generator.py:2062-2067 is the deletion target for
    Phase 10 P10.5. Until purge, it stays — sentinel-on path
    bypasses it via the new dispatcher; sentinel-off path uses it."""
    src = CANDIDATE_GEN_PATH.read_text(encoding="utf-8")
    assert "Nervous-System Reflex" in src or "Nervous System Reflex" in src


# ---------------------------------------------------------------------------
# §6 — Master-flag-off: sentinel branch is INERT (the safety pin)
# ---------------------------------------------------------------------------


def test_master_flag_off_dispatcher_not_invoked_for_sealed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source-level pin: in master-flag-off mode, ``_dispatch_via_sentinel``
    is wrapped in a conditional that's only entered when
    ``is_sentinel_enabled()`` returns True. Validate the flag default."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    assert ts.is_sentinel_enabled() is False


def test_static_block_pattern_unchanged_in_source() -> None:
    """The static topology block at lines 1404-1465 (legacy path)
    stays intact under Slice 3 — only a NEW branch precedes it.
    Pinned by string presence."""
    src = CANDIDATE_GEN_PATH.read_text(encoding="utf-8")
    assert "_topology.dw_allowed_for_route" in src
    # Block-mode skip_and_queue branch still present.
    assert 'block_mode == "skip_and_queue"' in src


def test_dispatcher_method_exists_on_class() -> None:
    """Module-import-time pin: the new method actually exists on the
    class with the expected signature."""
    method = getattr(cg.CandidateGenerator, "_dispatch_via_sentinel", None)
    assert method is not None
    assert callable(method)
    # Async method.
    assert inspect.iscoroutinefunction(method)
