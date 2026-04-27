"""Slice 2 regression spine for the topology.2 yaml schema + dual-reader.

Pins the additive v2 surface on ``provider_topology.py``:

  * Schema-version detection (``schema_version: "topology.2"``).
  * Per-route ``dw_models:`` ordered list parsed into ``Tuple[str, ...]``.
  * Per-route ``fallback_tolerance:`` parsed with v1 ``block_mode``
    fallback derivation (``skip_and_queue`` → ``"queue"``).
  * ``monitor:`` block parsed into ``MonitorConfig``.
  * **Backward compatibility** — every v1-only test in this file proves
    the existing yaml (``brain_selection_policy.yaml`` as shipped today)
    still parses to the SAME ``ProviderTopology`` shape callers depend
    on. A v1 yaml MUST NOT spontaneously become v2 just because the
    reader was upgraded.

No consumer wiring is exercised here — Slice 2 ships the schema +
reader isolated. Slice 3 will wire ``candidate_generator`` through the
new ``dw_models_for_route`` / ``fallback_tolerance_for_route`` accessors.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

import pytest

from backend.core.ouroboros.governance import provider_topology as pt


# ---------------------------------------------------------------------------
# Helpers — synthesize yaml dicts in-memory so tests don't touch the
# production yaml file. Each test is fully self-contained.
# ---------------------------------------------------------------------------


def _v1_route(
    name: str = "standard",
    dw_allowed: bool = False,
    block_mode: str = "cascade_to_claude",
    dw_model: str = None,
    reason: str = "test reason",
) -> Mapping[str, Any]:
    body: Dict[str, Any] = {
        "dw_allowed": dw_allowed,
        "block_mode": block_mode,
        "reason": reason,
    }
    if dw_model is not None:
        body["dw_model"] = dw_model
    return {name: body}


def _doubleword_section(
    routes: Mapping[str, Any],
    *,
    schema_version: str = None,
    monitor: Mapping[str, Any] = None,
    callers: Mapping[str, Any] = None,
) -> Mapping[str, Any]:
    section: Dict[str, Any] = {
        "enabled": True,
        "routes": dict(routes),
    }
    if schema_version is not None:
        section["schema_version"] = schema_version
    if monitor is not None:
        section["monitor"] = dict(monitor)
    if callers is not None:
        section["callers"] = dict(callers)
    return {"doubleword_topology": section}


# ===========================================================================
# §1 — Schema version detection
# ===========================================================================


def test_schema_version_v1_constant() -> None:
    assert pt.SCHEMA_VERSION_V1 == "topology.1"


def test_schema_version_v2_constant() -> None:
    assert pt.SCHEMA_VERSION_V2 == "topology.2"


def test_v1_yaml_parses_as_v1() -> None:
    """No ``schema_version`` key → v1."""
    raw = _doubleword_section(_v1_route())
    topo = pt._parse_topology(raw)
    assert topo.schema_version == pt.SCHEMA_VERSION_V1


def test_v2_explicit_version_parses_as_v2() -> None:
    raw = _doubleword_section(
        _v1_route(),
        schema_version="topology.2",
    )
    topo = pt._parse_topology(raw)
    assert topo.schema_version == pt.SCHEMA_VERSION_V2


def test_unknown_schema_version_falls_back_to_v1() -> None:
    raw = _doubleword_section(
        _v1_route(),
        schema_version="topology.99",
    )
    topo = pt._parse_topology(raw)
    # Unknown → conservatively treat as v1 to avoid silent semantic shift.
    assert topo.schema_version == pt.SCHEMA_VERSION_V1


def test_disabled_section_returns_empty_topology() -> None:
    """Empty topology object surfaces v1 schema by default — disabled
    callers should never see a v2 marker that promises richer behavior."""
    raw = {"doubleword_topology": {"enabled": False}}
    topo = pt._parse_topology(raw)
    assert topo.enabled is False
    assert topo.schema_version == pt.SCHEMA_VERSION_V1


# ===========================================================================
# §2 — RouteTopology v2 fields & effective_dw_models
# ===========================================================================


def test_v1_route_has_empty_dw_models_tuple() -> None:
    """A v1-only yaml entry produces an empty ``dw_models`` tuple — the
    v2 storage is just absent. ``effective_dw_models`` derives a single-
    element tuple from ``dw_model``."""
    raw = _doubleword_section(_v1_route(
        dw_allowed=True, dw_model="moonshotai/Kimi-K2.6",
    ))
    topo = pt._parse_topology(raw)
    entry = topo.routes["standard"]
    assert entry.dw_models == ()
    assert entry.effective_dw_models == ("moonshotai/Kimi-K2.6",)


def test_v2_route_dw_models_list_parses_as_tuple() -> None:
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": True,
            "block_mode": "cascade_to_claude",
            "reason": "test",
            "dw_models": [
                "moonshotai/Kimi-K2.6",
                "zai-org/GLM-5.1-FP8",
                "Qwen/Qwen3.6-35B-A3B-FP8",
            ],
            "fallback_tolerance": "cascade_to_claude",
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    entry = topo.routes["standard"]
    assert isinstance(entry.dw_models, tuple)
    assert entry.dw_models == (
        "moonshotai/Kimi-K2.6",
        "zai-org/GLM-5.1-FP8",
        "Qwen/Qwen3.6-35B-A3B-FP8",
    )
    assert entry.effective_dw_models == entry.dw_models


def test_v2_dw_models_skips_non_string_entries() -> None:
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": True,
            "reason": "test",
            "dw_models": [
                "moonshotai/Kimi-K2.6",
                42,                # int — skipped
                None,              # None — skipped
                {"nested": "x"},   # dict — skipped
                "Qwen/Qwen3.5-9B",
            ],
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    entry = topo.routes["standard"]
    assert entry.dw_models == (
        "moonshotai/Kimi-K2.6", "Qwen/Qwen3.5-9B",
    )


def test_v2_dw_models_strips_whitespace() -> None:
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": True,
            "reason": "test",
            "dw_models": ["  moonshotai/Kimi-K2.6  ", "\tQwen/Qwen3.5-9B\t"],
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    assert topo.routes["standard"].dw_models == (
        "moonshotai/Kimi-K2.6", "Qwen/Qwen3.5-9B",
    )


def test_v2_dw_models_drops_empty_strings() -> None:
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": True,
            "reason": "test",
            "dw_models": ["valid/model", "", "   ", "another/model"],
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    assert topo.routes["standard"].dw_models == (
        "valid/model", "another/model",
    )


def test_v2_dw_models_non_list_value_yields_empty() -> None:
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": True,
            "reason": "test",
            "dw_models": "not-a-list",
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    assert topo.routes["standard"].dw_models == ()


def test_v2_disallowed_route_with_dw_models_still_parses() -> None:
    """A route with ``dw_allowed: false`` AND a ``dw_models:`` list is
    a valid intermediate state — the operator has staged the v2 ranked
    list but the topology gate is still v1-blocking. ``effective_dw_models``
    returns the v2 list (Slice 3+ consumer decides what to do; Slice 2's
    job is just to surface the data)."""
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": False,
            "block_mode": "cascade_to_claude",
            "reason": "sealed but staged",
            "dw_models": ["moonshotai/Kimi-K2.6"],
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    entry = topo.routes["standard"]
    assert entry.dw_allowed is False
    assert entry.dw_models == ("moonshotai/Kimi-K2.6",)
    assert entry.effective_dw_models == ("moonshotai/Kimi-K2.6",)


def test_route_with_no_dw_path_returns_empty_effective() -> None:
    """v1 disallowed route with no ``dw_model`` AND no ``dw_models``
    produces empty effective list."""
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": False,
            "block_mode": "cascade_to_claude",
            "reason": "sealed",
        },
    })
    topo = pt._parse_topology(raw)
    assert topo.routes["standard"].effective_dw_models == ()


# ===========================================================================
# §3 — fallback_tolerance derivation + explicit override
# ===========================================================================


def test_fallback_tolerance_v1_skip_and_queue_derives_to_queue() -> None:
    raw = _doubleword_section({
        "background": {
            "dw_allowed": False,
            "block_mode": "skip_and_queue",
            "reason": "BG sealed",
        },
    })
    topo = pt._parse_topology(raw)
    assert (
        topo.routes["background"].fallback_tolerance == "queue"
    )


def test_fallback_tolerance_v1_cascade_derives_to_cascade() -> None:
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": False,
            "block_mode": "cascade_to_claude",
            "reason": "sealed",
        },
    })
    topo = pt._parse_topology(raw)
    assert (
        topo.routes["standard"].fallback_tolerance == "cascade_to_claude"
    )


def test_fallback_tolerance_v2_explicit_overrides_block_mode() -> None:
    """When yaml v2 explicit key is present, it wins over v1 derivation."""
    raw = _doubleword_section({
        "background": {
            "dw_allowed": False,
            # v1 key would derive to "queue"
            "block_mode": "skip_and_queue",
            "reason": "BG sealed",
            # v2 explicit override:
            "fallback_tolerance": "cascade_to_claude",
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    assert (
        topo.routes["background"].fallback_tolerance == "cascade_to_claude"
    )


def test_fallback_tolerance_invalid_value_falls_back_to_derivation() -> None:
    raw = _doubleword_section({
        "background": {
            "dw_allowed": False,
            "block_mode": "skip_and_queue",
            "reason": "BG",
            "fallback_tolerance": "not-a-real-tolerance",
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    # Invalid string → falls through to block_mode derivation.
    assert topo.routes["background"].fallback_tolerance == "queue"


def test_fallback_tolerance_for_route_method_returns_value() -> None:
    raw = _doubleword_section({
        "background": {
            "dw_allowed": False,
            "block_mode": "skip_and_queue",
            "reason": "BG",
        },
    })
    topo = pt._parse_topology(raw)
    assert topo.fallback_tolerance_for_route("background") == "queue"
    assert (
        topo.fallback_tolerance_for_route("BACKGROUND") == "queue"
    )  # case-insensitive


def test_fallback_tolerance_for_unknown_route_defaults_cascade() -> None:
    raw = _doubleword_section(_v1_route())
    topo = pt._parse_topology(raw)
    assert (
        topo.fallback_tolerance_for_route("never-mapped") == "cascade_to_claude"
    )


def test_fallback_tolerance_disabled_topology_returns_cascade() -> None:
    topo = pt._EMPTY_TOPOLOGY
    assert (
        topo.fallback_tolerance_for_route("any") == "cascade_to_claude"
    )


def test_fallback_tolerance_bg_cascade_override_flips_queue_to_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing JARVIS_TOPOLOGY_BG_CASCADE_ENABLED dev-override must
    apply to the v2 method too — same semantics as block_mode_for_route."""
    raw = _doubleword_section({
        "background": {
            "dw_allowed": False,
            "block_mode": "skip_and_queue",
            "reason": "BG",
        },
    })
    topo = pt._parse_topology(raw)
    monkeypatch.setenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", "true")
    # Reset the once-warned flag so this test sees the warning path
    pt._BG_OVERRIDE_WARNED = False
    assert (
        topo.fallback_tolerance_for_route("background") == "cascade_to_claude"
    )


# ===========================================================================
# §4 — dw_models_for_route accessor
# ===========================================================================


def test_dw_models_for_route_v1_yaml_returns_single_element() -> None:
    raw = _doubleword_section(_v1_route(
        dw_allowed=True, dw_model="moonshotai/Kimi-K2.6",
    ))
    topo = pt._parse_topology(raw)
    assert (
        topo.dw_models_for_route("standard") == ("moonshotai/Kimi-K2.6",)
    )


def test_dw_models_for_route_v2_yaml_returns_full_list() -> None:
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": True,
            "reason": "test",
            "dw_models": [
                "moonshotai/Kimi-K2.6",
                "zai-org/GLM-5.1-FP8",
                "Qwen/Qwen3.6-35B-A3B-FP8",
            ],
        },
    }, schema_version="topology.2")
    topo = pt._parse_topology(raw)
    assert topo.dw_models_for_route("standard") == (
        "moonshotai/Kimi-K2.6",
        "zai-org/GLM-5.1-FP8",
        "Qwen/Qwen3.6-35B-A3B-FP8",
    )


def test_dw_models_for_unknown_route_empty() -> None:
    raw = _doubleword_section(_v1_route())
    topo = pt._parse_topology(raw)
    assert topo.dw_models_for_route("never-mapped") == ()


def test_dw_models_for_route_disabled_topology_empty() -> None:
    assert pt._EMPTY_TOPOLOGY.dw_models_for_route("any") == ()


def test_dw_models_for_route_blocked_route_with_dw_model_returns_empty() -> None:
    """v1 disallowed route with a ``dw_model`` set: the parser drops
    the ``dw_model`` (preserving v1 semantics — disallowed = no model);
    so the effective list is empty."""
    raw = _doubleword_section({
        "standard": {
            "dw_allowed": False,
            "block_mode": "cascade_to_claude",
            "reason": "sealed",
            "dw_model": "should-be-ignored-when-disallowed",
        },
    })
    topo = pt._parse_topology(raw)
    assert topo.dw_models_for_route("standard") == ()


# ===========================================================================
# §5 — MonitorConfig parsing
# ===========================================================================


def test_monitor_config_default_when_block_absent() -> None:
    raw = _doubleword_section(_v1_route())
    topo = pt._parse_topology(raw)
    assert topo.monitor_config() is None


def test_monitor_config_parses_all_fields() -> None:
    raw = _doubleword_section(
        _v1_route(),
        monitor={
            "probe_interval_healthy_s": 30.0,
            "probe_backoff_base_s": 10.0,
            "probe_backoff_cap_s": 300.0,
            "severed_threshold_weighted": 3.0,
            "heavy_probe_ratio": 0.2,
            "ramp_schedule_csv": "0:1.0,10:2.0,30:4.0",
        },
    )
    topo = pt._parse_topology(raw)
    cfg = topo.monitor_config()
    assert cfg is not None
    assert cfg.probe_interval_healthy_s == 30.0
    assert cfg.probe_backoff_base_s == 10.0
    assert cfg.probe_backoff_cap_s == 300.0
    assert cfg.severed_threshold_weighted == 3.0
    assert cfg.heavy_probe_ratio == 0.2
    assert cfg.ramp_schedule_csv == "0:1.0,10:2.0,30:4.0"


def test_monitor_config_partial_fields_others_none() -> None:
    """Fields can be omitted independently — sentinel falls back to env
    defaults for missing ones."""
    raw = _doubleword_section(
        _v1_route(),
        monitor={"probe_interval_healthy_s": 60.0},
    )
    topo = pt._parse_topology(raw)
    cfg = topo.monitor_config()
    assert cfg is not None
    assert cfg.probe_interval_healthy_s == 60.0
    assert cfg.probe_backoff_base_s is None
    assert cfg.severed_threshold_weighted is None


def test_monitor_config_invalid_field_value_becomes_none() -> None:
    """Type-coercion-fail (e.g. probe_interval = 'twenty seconds') must
    yield None for that field, not raise."""
    raw = _doubleword_section(
        _v1_route(),
        monitor={
            "probe_interval_healthy_s": "twenty seconds",
            "severed_threshold_weighted": 3.0,
        },
    )
    topo = pt._parse_topology(raw)
    cfg = topo.monitor_config()
    assert cfg is not None
    assert cfg.probe_interval_healthy_s is None
    assert cfg.severed_threshold_weighted == 3.0


def test_monitor_config_non_mapping_value_yields_none() -> None:
    raw = _doubleword_section(
        _v1_route(),
        monitor=None,
    )
    topo = pt._parse_topology(raw)
    assert topo.monitor_config() is None


def test_monitor_config_ramp_schedule_csv_strips_whitespace() -> None:
    raw = _doubleword_section(
        _v1_route(),
        monitor={"ramp_schedule_csv": "  0:1.0,10:2.0  "},
    )
    topo = pt._parse_topology(raw)
    cfg = topo.monitor_config()
    assert cfg is not None
    assert cfg.ramp_schedule_csv == "0:1.0,10:2.0"


# ===========================================================================
# §6 — Backward compatibility against the actual production yaml
# ===========================================================================


def test_production_yaml_still_parses_as_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual ``brain_selection_policy.yaml`` shipped today MUST
    still parse to v1. If this test fails after Slice 2, it means a
    schema_version key got accidentally added to production yaml
    before Phase 10 P10.5 (the purge) is authorized."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    assert topo.enabled is True
    assert topo.schema_version == pt.SCHEMA_VERSION_V1


def test_production_yaml_routes_have_empty_dw_models() -> None:
    """Production yaml is v1; ``dw_models`` should be empty tuple for
    every route. The ``effective_dw_models`` accessor handles the
    backward-compat single-element derivation."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    for route_name, entry in topo.routes.items():
        assert entry.dw_models == (), (
            f"production route {route_name!r} has unexpected dw_models — "
            "Slice 2 should not modify production yaml v1 keys"
        )


def test_production_yaml_v1_block_mode_methods_unchanged() -> None:
    """Regression pin: existing v1 callers' answers MUST be byte-
    identical after Slice 2. If any of these change, Slice 2 broke
    backward compat."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    # All 5 routes are sealed in production today.
    for route in (
        "immediate", "complex", "standard", "background", "speculative",
    ):
        assert topo.dw_allowed_for_route(route) is False
    # Block-mode wiring per yaml today:
    assert topo.block_mode_for_route("immediate") == "cascade_to_claude"
    assert topo.block_mode_for_route("complex") == "cascade_to_claude"
    assert topo.block_mode_for_route("standard") == "cascade_to_claude"
    assert topo.block_mode_for_route("background") == "skip_and_queue"
    assert topo.block_mode_for_route("speculative") == "skip_and_queue"


def test_production_yaml_v2_methods_derive_correctly() -> None:
    """The new v2 accessors must give sensible answers when reading the
    production v1 yaml. Slice 3 will consume these accessors directly."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    # All disallowed routes have empty dw_models lists (v1 yaml had no
    # explicit dw_model on disallowed routes).
    for route in (
        "immediate", "complex", "standard", "background", "speculative",
    ):
        assert topo.dw_models_for_route(route) == ()
    # Fallback tolerance derives from block_mode:
    assert topo.fallback_tolerance_for_route("immediate") == "cascade_to_claude"
    assert topo.fallback_tolerance_for_route("complex") == "cascade_to_claude"
    assert topo.fallback_tolerance_for_route("standard") == "cascade_to_claude"
    assert topo.fallback_tolerance_for_route("background") == "queue"
    assert topo.fallback_tolerance_for_route("speculative") == "queue"


def test_production_yaml_callers_unchanged_after_slice2() -> None:
    """The compaction-model swap from this same PR is the only
    callers section change today. Pin that the OTHER two callers are
    still on Gemma 4 31B + that compaction is on Qwen3-14B-FP8 — that
    way a future yaml edit can't silently regress without a test
    failure."""
    pt._CACHED_TOPOLOGY = None
    topo = pt.get_topology()
    assert topo.model_for_caller("semantic_triage") == "google/gemma-4-31B-it"
    assert topo.model_for_caller("ouroboros_plan") == "google/gemma-4-31B-it"
    assert topo.model_for_caller("compaction") == "Qwen/Qwen3-14B-FP8"


# ===========================================================================
# §7 — Static-block invariant pin (Phase 10 P10.5 deletion target)
# ===========================================================================


def test_phase_10_static_blocks_still_present() -> None:
    """**Inverse pin** — until Phase 10 P10.5 (the purge) is authorized,
    the static ``dw_allowed: false`` blocks MUST still be in the
    production yaml. If this test starts failing, somebody removed the
    static blocks before the dynamic sentinel was wired (Slice 3) or
    before the operator authorized the purge — both are unsafe."""
    yaml_path = (
        Path(pt.__file__).parent / "brain_selection_policy.yaml"
    )
    text = yaml_path.read_text(encoding="utf-8")
    # All 5 routes must still have dw_allowed: false for now.
    assert text.count("dw_allowed: false") == 5, (
        "Phase 10 P10.5 (THE PURGE) deletes these lines, but only AFTER "
        "Slices 3+4 land + 3 forced-clean once-proofs prove the dynamic "
        "sentinel produces correct routing decisions. If you got here "
        "via a refactor, revert."
    )


# ===========================================================================
# §8 — _resolve_fallback_tolerance helper direct
# ===========================================================================


def test_resolve_fallback_tolerance_explicit_queue() -> None:
    assert pt._resolve_fallback_tolerance("queue", "cascade_to_claude") == "queue"


def test_resolve_fallback_tolerance_explicit_cascade() -> None:
    assert pt._resolve_fallback_tolerance(
        "cascade_to_claude", "skip_and_queue",
    ) == "cascade_to_claude"


def test_resolve_fallback_tolerance_case_insensitive() -> None:
    assert pt._resolve_fallback_tolerance("QUEUE", "x") == "queue"


def test_resolve_fallback_tolerance_invalid_explicit_falls_through() -> None:
    """Invalid explicit value → derive from block_mode."""
    assert pt._resolve_fallback_tolerance(
        "garbage", "skip_and_queue",
    ) == "queue"
    assert pt._resolve_fallback_tolerance(
        "garbage", "cascade_to_claude",
    ) == "cascade_to_claude"


def test_resolve_fallback_tolerance_none_explicit_derives() -> None:
    assert pt._resolve_fallback_tolerance(
        None, "skip_and_queue",
    ) == "queue"
    assert pt._resolve_fallback_tolerance(
        None, "cascade_to_claude",
    ) == "cascade_to_claude"
    assert pt._resolve_fallback_tolerance(None, "") == "cascade_to_claude"


# ===========================================================================
# §9 — _parse_dw_models helper direct
# ===========================================================================


def test_parse_dw_models_none() -> None:
    assert pt._parse_dw_models(None) == ()


def test_parse_dw_models_empty_list() -> None:
    assert pt._parse_dw_models([]) == ()


def test_parse_dw_models_tuple_input_works() -> None:
    """Both list and tuple shapes accepted — yaml may produce either."""
    assert pt._parse_dw_models(("a", "b")) == ("a", "b")


def test_parse_dw_models_string_input_yields_empty() -> None:
    """A bare string (not a list) is malformed — fail-open empty."""
    assert pt._parse_dw_models("moonshotai/Kimi-K2.6") == ()


def test_parse_dw_models_dict_input_yields_empty() -> None:
    assert pt._parse_dw_models({"x": 1}) == ()
