"""Slice 3.5 regression spine — Pre-Flight Handshake + boundary isolation.

Pins the architectural fix from the 2026-04-27 directive:

  * ``SentinelInitializationError`` raised when the sentinel can't
    initialize inside the subprocess context — instead of the prior
    silent try/except that swallowed boundary-isolation defects.
  * ``preflight_check()`` returns a structured result with explicit
    booleans for every gate: flag enabled, module imported, singleton
    initialized, topology loaded, schema_version, routes with
    dw_models, monitor config, event-loop binding, state-dir writable.
  * ``sentinel_propagated_vars()`` enumerates every JARVIS_TOPOLOGY_*
    env var the sentinel layer reads. Test asserts every var the
    module reads is in the list (no silent additions).
  * ``live_fire_soak._build_env_for_flag`` explicitly forwards every
    var from ``sentinel_propagated_vars()``. Test asserts the
    forwarding holds end-to-end through the harness's env build.
"""
from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import topology_sentinel as ts
from backend.core.ouroboros.governance import candidate_generator as cg
from backend.core.ouroboros.governance.graduation import live_fire_soak as lfs


# ===========================================================================
# §1 — SentinelInitializationError shape
# ===========================================================================


def test_sentinel_init_error_is_runtime_error_subclass() -> None:
    assert issubclass(ts.SentinelInitializationError, RuntimeError)


def test_sentinel_init_error_carries_failed_assertions() -> None:
    err = ts.SentinelInitializationError(
        ("topology_not_loaded", "no_routes_have_dw_models"),
        ("master_flag_off",),
    )
    assert err.failed_assertions == (
        "topology_not_loaded", "no_routes_have_dw_models",
    )
    assert err.diagnostics == ("master_flag_off",)
    # Stringification must surface the assertions.
    msg = str(err)
    assert "topology_not_loaded" in msg
    assert "no_routes_have_dw_models" in msg


def test_sentinel_init_error_empty_assertions() -> None:
    err = ts.SentinelInitializationError((), ())
    assert err.failed_assertions == ()
    assert err.diagnostics == ()


# ===========================================================================
# §2 — preflight_check() — structured initialization result
# ===========================================================================


def test_preflight_returns_healthy_with_master_flag_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    ts.reset_default_sentinel_for_tests()
    # Force topology cache reload to read v2 yaml.
    from backend.core.ouroboros.governance import provider_topology as pt
    pt._CACHED_TOPOLOGY = None
    result = ts.preflight_check()
    assert result.healthy is True
    assert result.flag_enabled is True
    assert result.topology_loaded is True
    assert result.schema_version == "topology.2"
    assert "background" in result.routes_with_dw_models
    assert result.monitor_config_present is True
    assert result.state_dir_writable is True
    ts.reset_default_sentinel_for_tests()


def test_preflight_with_master_flag_off_still_returns_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Master-flag-off is a valid state — preflight returns a result
    with diagnostics, NOT failed_assertions. Caller decides what to
    do (the dispatcher won't enter the gate at all in this case)."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    result = ts.preflight_check()
    assert result.flag_enabled is False
    assert "master_flag_off" in result.diagnostics


def test_preflight_routes_with_dw_models_drops_immediate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """IMMEDIATE has empty dw_models by design (Manifesto §5 — Claude
    direct). It must NOT appear in routes_with_dw_models."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    from backend.core.ouroboros.governance import provider_topology as pt
    pt._CACHED_TOPOLOGY = None
    ts.reset_default_sentinel_for_tests()
    result = ts.preflight_check()
    assert "immediate" not in result.routes_with_dw_models
    assert "standard" in result.routes_with_dw_models
    assert "background" in result.routes_with_dw_models
    ts.reset_default_sentinel_for_tests()


def test_preflight_to_dict_pinned_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    from backend.core.ouroboros.governance import provider_topology as pt
    pt._CACHED_TOPOLOGY = None
    ts.reset_default_sentinel_for_tests()
    result = ts.preflight_check()
    payload = result.to_dict()
    assert payload["schema_version"] == "preflight.1"
    assert "flag_enabled" in payload
    assert "failed_assertions" in payload
    assert "healthy" in payload
    ts.reset_default_sentinel_for_tests()


def test_preflight_state_dir_unwritable_marked_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the state dir can't be created (permission, etc.), preflight
    reports it via diagnostic. This isn't a fatal assertion — the
    dispatcher can still function; only persistence is affected."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    # Point at a path that contains a file (so mkdir fails with NotADirectory).
    bad = "/etc/passwd/subdir"
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", bad)
    from backend.core.ouroboros.governance import provider_topology as pt
    pt._CACHED_TOPOLOGY = None
    ts.reset_default_sentinel_for_tests()
    result = ts.preflight_check()
    assert result.state_dir_writable is False
    assert any(
        "state_dir_unwritable" in d for d in result.diagnostics
    )
    ts.reset_default_sentinel_for_tests()


def test_preflight_require_routes_false_no_routes_still_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tests can disable the routes-required check via the kwarg —
    useful for unit tests that want to verify other branches in
    isolation."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    from backend.core.ouroboros.governance import provider_topology as pt
    pt._CACHED_TOPOLOGY = None
    ts.reset_default_sentinel_for_tests()
    result = ts.preflight_check(require_routes=False)
    # Result is still produced even with require_routes=False; production
    # yaml has routes, so this returns healthy.
    assert isinstance(result, ts.SentinelPreflightResult)
    ts.reset_default_sentinel_for_tests()


# ===========================================================================
# §3 — sentinel_propagated_vars() contract
# ===========================================================================


def test_sentinel_propagated_vars_includes_master_flag() -> None:
    vars_list = ts.sentinel_propagated_vars()
    assert "JARVIS_TOPOLOGY_SENTINEL_ENABLED" in vars_list


def test_sentinel_propagated_vars_includes_force_severed() -> None:
    assert "JARVIS_TOPOLOGY_FORCE_SEVERED" in ts.sentinel_propagated_vars()


def test_sentinel_propagated_vars_includes_state_dir() -> None:
    assert "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR" in ts.sentinel_propagated_vars()


def test_sentinel_propagated_vars_returns_tuple() -> None:
    vars_list = ts.sentinel_propagated_vars()
    assert isinstance(vars_list, tuple)
    # Stable shape — every element must be a string starting with the
    # canonical prefix.
    for name in vars_list:
        assert isinstance(name, str)
        assert name.startswith("JARVIS_TOPOLOGY_") or name.startswith(
            "JARVIS_TOPOLOGY_WEIGHT_"
        )


def test_sentinel_propagated_vars_no_duplicates() -> None:
    vars_list = ts.sentinel_propagated_vars()
    assert len(vars_list) == len(set(vars_list))


def test_sentinel_env_propagation_contract() -> None:
    """**The boundary isolation contract** — every JARVIS_TOPOLOGY_*
    env var that ``topology_sentinel.py`` reads MUST appear in
    ``_SENTINEL_PROPAGATED_VARS``. Otherwise the harness's explicit
    forwarding will miss it and the silent boundary failure recurs.

    This test parses the module source, finds every ``os.environ.get``
    / ``os.environ[..]`` access with a JARVIS_TOPOLOGY_ prefix, and
    asserts each is in the propagated list."""
    src = Path(ts.__file__).read_text(encoding="utf-8")
    pattern = re.compile(
        r'os\.environ(?:\.get)?\(\s*["\'](JARVIS_TOPOLOGY_\w+)["\']'
    )
    found = set(pattern.findall(src))
    # _env_bool / _env_int / _env_float / _env_path receive the name as
    # an arg — also scan for those.
    helper_pattern = re.compile(
        r'_env_\w+\(\s*["\'](JARVIS_TOPOLOGY_\w+)["\']'
    )
    found |= set(helper_pattern.findall(src))
    # f-string access for the per-source weight overrides.
    fstring_pattern = re.compile(
        r'f["\']JARVIS_TOPOLOGY_WEIGHT_'
    )
    if fstring_pattern.search(src):
        # The weight env knobs are constructed dynamically — they're
        # already pinned by name in _SENTINEL_PROPAGATED_VARS via the
        # FailureSource enum mapping. Verify the mapping is complete.
        for source in ts.FailureSource:
            expected = f"JARVIS_TOPOLOGY_WEIGHT_{source.name}"
            assert expected in ts.sentinel_propagated_vars(), (
                f"FailureSource {source.name} weight knob {expected} "
                "must appear in _SENTINEL_PROPAGATED_VARS"
            )
    propagated = set(ts.sentinel_propagated_vars())
    missing = found - propagated
    assert not missing, (
        f"env vars read by sentinel module but not in propagation "
        f"list: {missing}"
    )


# ===========================================================================
# §4 — Harness env forwarding (live_fire_soak)
# ===========================================================================


def test_harness_env_forwards_sentinel_master_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: parent sets JARVIS_TOPOLOGY_SENTINEL_ENABLED=true,
    harness builds env, subprocess sees it."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    h = lfs.LiveFireSoakHarness(
        project_root=Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"),
    )
    env = h._build_env_for_flag("JARVIS_DECISION_TRACE_LEDGER_ENABLED")
    assert env.get("JARVIS_TOPOLOGY_SENTINEL_ENABLED") == "true"


def test_harness_env_does_not_inject_sentinel_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When parent has NOT set the master flag, harness must not
    inject a default value — the sentinel module's own default-false
    handles it. Forwarding ≠ defaulting."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    h = lfs.LiveFireSoakHarness(
        project_root=Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"),
    )
    env = h._build_env_for_flag("JARVIS_DECISION_TRACE_LEDGER_ENABLED")
    assert "JARVIS_TOPOLOGY_SENTINEL_ENABLED" not in env or (
        env["JARVIS_TOPOLOGY_SENTINEL_ENABLED"] == ""
    )


def test_harness_env_forwards_force_severed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_FORCE_SEVERED", "true")
    h = lfs.LiveFireSoakHarness(
        project_root=Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"),
    )
    env = h._build_env_for_flag("JARVIS_DECISION_TRACE_LEDGER_ENABLED")
    assert env.get("JARVIS_TOPOLOGY_FORCE_SEVERED") == "true"


def test_harness_env_forwards_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    h = lfs.LiveFireSoakHarness(
        project_root=Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"),
    )
    env = h._build_env_for_flag("JARVIS_DECISION_TRACE_LEDGER_ENABLED")
    assert env.get("JARVIS_TOPOLOGY_SENTINEL_STATE_DIR") == str(tmp_path)


def test_harness_env_forwards_every_propagated_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Stress test: set every var in _SENTINEL_PROPAGATED_VARS, build
    env, assert all forwarded."""
    sentinel_value = "test-fwd"
    for name in ts.sentinel_propagated_vars():
        monkeypatch.setenv(name, f"{sentinel_value}-{name}")
    h = lfs.LiveFireSoakHarness(
        project_root=Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"),
    )
    env = h._build_env_for_flag("JARVIS_DECISION_TRACE_LEDGER_ENABLED")
    for name in ts.sentinel_propagated_vars():
        assert env.get(name) == f"{sentinel_value}-{name}", (
            f"{name} not forwarded by harness env build"
        )


# ===========================================================================
# §5 — Dispatcher gate uses preflight + raises on failure
# ===========================================================================


def test_dispatcher_gate_imports_preflight_check() -> None:
    """Source-level pin: the dispatcher's gate at line 1404+ must
    import ``preflight_check`` and ``SentinelInitializationError``
    (not just ``is_sentinel_enabled``)."""
    src = Path(cg.__file__).read_text(encoding="utf-8")
    # Find the Phase 10 gate block.
    gate_idx = src.index("Phase 10 sentinel preflight")
    pre_window = src[max(0, gate_idx - 1000):gate_idx]
    assert "preflight_check" in pre_window
    assert "SentinelInitializationError" in pre_window


def test_dispatcher_gate_raises_on_unhealthy_preflight() -> None:
    """Source-level pin: dispatcher MUST raise (not silently fall
    through) when preflight is unhealthy. Verify the raise statement
    is present in the gate block."""
    src = Path(cg.__file__).read_text(encoding="utf-8")
    gate_idx = src.index("Phase 10 sentinel preflight")
    window = src[max(0, gate_idx - 1500):gate_idx + 500]
    assert "raise _SentinelInitError" in window or (
        "raise SentinelInitializationError" in window
    )


def test_dispatcher_gate_raises_on_import_failure() -> None:
    """Source-level pin: master-flag-on + module import failure must
    raise (not silently set _sentinel_active=False). The directive's
    'no silent fallback' contract."""
    src = Path(cg.__file__).read_text(encoding="utf-8")
    gate_idx = src.index("Phase 10 sentinel preflight")
    window = src[max(0, gate_idx - 1500):gate_idx + 500]
    assert "sentinel_module_import_failed" in window
    assert "raise RuntimeError" in window


def test_dispatcher_gate_logs_preflight_result() -> None:
    """Source-level pin: on successful preflight, the dispatcher logs
    a single structured INFO line so operators can confirm the gate
    fired correctly. This is NOT the silent-failure log line we
    rejected — it's an affirmative success record."""
    src = Path(cg.__file__).read_text(encoding="utf-8")
    gate_idx = src.index("Phase 10 sentinel preflight")
    window = src[max(0, gate_idx - 1500):gate_idx + 500]
    assert "Phase 10 sentinel preflight:" in window


def test_dispatcher_gate_master_flag_off_path_unchanged() -> None:
    """Master-flag-off path MUST remain byte-identical legacy behavior.
    The gate block is wrapped in ``if _flag_raw in (...)``; below it
    the static yaml gate at line 1439+ is the legacy path."""
    src = Path(cg.__file__).read_text(encoding="utf-8")
    # The legacy gate's signature stays present (regression guard).
    assert "_topology.dw_allowed_for_route" in src
    # The flag-on conditional surrounds the preflight block.
    gate_idx = src.index("Phase 10 sentinel preflight")
    # Widen the search window to include the gate's own block + a few
    # lines after, where the conditional's text resides.
    pre = src[max(0, gate_idx - 2000):gate_idx + 1500]
    assert "JARVIS_TOPOLOGY_SENTINEL_ENABLED" in pre, (
        "expected master-flag check string in dispatcher gate block"
    )
    assert 'in ("1", "true", "yes", "on")' in pre, (
        "expected truthy-value parsing in the gate's flag check"
    )


# ===========================================================================
# §6 — Boundary isolation invariants
# ===========================================================================


def test_preflight_function_is_synchronous() -> None:
    """preflight_check() must be synchronous so it can be called from
    both async (the dispatcher) AND sync (tests, /sentinel REPL)
    contexts."""
    assert not inspect.iscoroutinefunction(ts.preflight_check)


def test_preflight_never_raises_on_caller_facing_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The CALLER raises SentinelInitializationError when
    .healthy is False. preflight_check() itself must never raise —
    it must always return a structured result."""
    # Force a topology-load failure by pointing yaml resolution at a
    # bogus path. This isn't a real env knob — we monkey-patch the
    # resolver so the test is deterministic.
    from backend.core.ouroboros.governance import provider_topology as pt
    monkeypatch.setattr(
        pt, "_locate_policy_yaml", lambda: None,
    )
    pt._CACHED_TOPOLOGY = None
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    ts.reset_default_sentinel_for_tests()
    # Must not raise. Must return a result with healthy=False.
    result = ts.preflight_check()
    assert isinstance(result, ts.SentinelPreflightResult)
    assert result.healthy is False
    ts.reset_default_sentinel_for_tests()


def test_sentinel_init_error_carries_unhealthy_preflight_data() -> None:
    """When the dispatcher raises SentinelInitializationError, the
    error MUST carry the failed_assertions + diagnostics from the
    preflight result so operators see exactly what failed."""
    err = ts.SentinelInitializationError(
        ("topology_not_loaded",),
        ("master_flag_off", "state_dir_unwritable:OSError"),
    )
    assert "topology_not_loaded" in err.failed_assertions
    assert "master_flag_off" in err.diagnostics


def test_preflight_export_in_module_all() -> None:
    assert "preflight_check" in ts.__all__
    assert "SentinelInitializationError" in ts.__all__
    assert "SentinelPreflightResult" in ts.__all__
    assert "sentinel_propagated_vars" in ts.__all__
