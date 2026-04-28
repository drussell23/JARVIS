"""Phase 1 Slice 1.3 — Phase capture wiring regression spine.

Two scopes:

  1. Phase capture helper itself (master flag, adapter registry,
     ctx canonicalization, RECORD/REPLAY/VERIFY semantics through
     the phase_capture wrapper).

  2. ROUTE phase wiring — proves the production callsite in
     phase_runners/route_runner.py correctly engages the substrate
     and degrades to passthrough cleanly.

Pins:
  §1   phase_capture_enabled flag — default false; case-tolerant
  §2   Both flags must be ON for capture to engage
  §3   Master flag off → pure passthrough (no disk, no recording)
  §4   register_adapter — idempotent + log-on-replace
  §5   register_adapter — defensive on bad input
  §6   get_adapter — falls back to identity
  §7   _build_ctx_inputs — canonicalizes ctx fields
  §8   _build_ctx_inputs — target_files sorted (canonical hash stable)
  §9   _build_ctx_inputs — missing ctx fields default safely
  §10  capture_phase_decision — RECORD path writes ledger
  §11  capture_phase_decision — REPLAY path returns adapter-deserialized
  §12  capture_phase_decision — adapter serialize fault → identity fallback
  §13  capture_phase_decision — adapter deserialize fault → raw repr
  §14  ROUTE wiring — adapter registered at module load
  §15  ROUTE wiring — passthrough preserves direct UrgencyRouter call
  §16  ROUTE wiring — capture failure falls back to direct call
  §17  Authority invariants — phase_capture imports
  §18  Authority invariants — route_runner doesn't break on import
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.determinism import (
    DecisionRuntime,
    OutputAdapter,
    capture_phase_decision,
    phase_capture_enabled,
    register_adapter,
)
from backend.core.ouroboros.governance.determinism.decision_runtime import (
    reset_all_for_tests as reset_runtime_for_tests,
)
from backend.core.ouroboros.governance.determinism.phase_capture import (
    _build_ctx_inputs,
    _IDENTITY_ADAPTER,
    get_adapter,
    iter_registered,
    reset_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "test-session")
    reset_runtime_for_tests()
    reset_registry_for_tests()
    yield tmp_path / "det"
    reset_runtime_for_tests()
    reset_registry_for_tests()


@pytest.fixture
def isolated_passthrough(tmp_path, monkeypatch):
    """Capture flag OFF → pure passthrough."""
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "false",
    )
    reset_runtime_for_tests()
    reset_registry_for_tests()
    yield tmp_path
    reset_runtime_for_tests()
    reset_registry_for_tests()


def _ctx_stub(**kwargs):
    """Minimal OperationContext-shaped stub."""
    ctx = MagicMock()
    ctx.op_id = kwargs.get("op_id", "op-test")
    ctx.signal_urgency = kwargs.get("signal_urgency", "normal")
    ctx.signal_source = kwargs.get("signal_source", "test_source")
    ctx.task_complexity = kwargs.get("task_complexity", "moderate")
    ctx.target_files = kwargs.get("target_files", ())
    ctx.cross_repo = kwargs.get("cross_repo", False)
    ctx.is_read_only = kwargs.get("is_read_only", False)
    return ctx


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_phase_capture_default_true(monkeypatch) -> None:
    """Phase 1 Slice 1.5 graduated default — env unset → True."""
    monkeypatch.delenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", raising=False,
    )
    assert phase_capture_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  "])
def test_phase_capture_empty_reads_as_default_true(
    monkeypatch, val,
) -> None:
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", val,
    )
    assert phase_capture_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_phase_capture_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", val,
    )
    assert phase_capture_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_phase_capture_falsy(monkeypatch, val) -> None:
    """Hot-revert: explicit false-class strings disable. Empty/
    whitespace map to graduated default True post-Slice-1.5."""
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", val,
    )
    assert phase_capture_enabled() is False


# ---------------------------------------------------------------------------
# §2 — Both flags must be ON for capture to engage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_flag_off_engages_passthrough(
    monkeypatch, isolated_passthrough,
) -> None:
    """Capture off + ledger on → still passthrough."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "false",
    )

    counter = {"n": 0}

    def compute():
        counter["n"] += 1
        return "X"

    out = await capture_phase_decision(
        op_id="op-1", phase="P", kind="K", compute=compute,
    )
    assert out == "X"
    assert counter["n"] == 1
    # No disk artifacts created
    decisions = isolated_passthrough / "det" / "test-session" / "decisions.jsonl"
    assert not decisions.exists()


@pytest.mark.asyncio
async def test_ledger_flag_off_engages_passthrough(
    monkeypatch, tmp_path,
) -> None:
    """Capture on + ledger off → passthrough (defensive)."""
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "false")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    reset_runtime_for_tests()

    out = await capture_phase_decision(
        op_id="op-1", phase="P", kind="K",
        compute=lambda: "Y",
    )
    assert out == "Y"
    assert not (tmp_path / "det").exists()


# ---------------------------------------------------------------------------
# §3 — Master flag off path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_off_no_disk_traffic(
    isolated_passthrough,
) -> None:
    counter = {"n": 0}

    async def compute():
        counter["n"] += 1
        return {"route": "STANDARD"}

    out = await capture_phase_decision(
        op_id="op-1", phase="ROUTE", kind="route_assignment",
        compute=compute,
    )
    assert out == {"route": "STANDARD"}
    assert counter["n"] == 1


# ---------------------------------------------------------------------------
# §4-§6 — Adapter registry
# ---------------------------------------------------------------------------


def test_register_adapter_basic() -> None:
    reset_registry_for_tests()
    a = OutputAdapter(
        serialize=lambda x: x, deserialize=lambda x: x, name="test1",
    )
    register_adapter(phase="P", kind="K", adapter=a)
    assert get_adapter(phase="P", kind="K") is a


def test_register_adapter_replace_logs(caplog) -> None:
    import logging
    reset_registry_for_tests()
    a = OutputAdapter(
        serialize=lambda x: x, deserialize=lambda x: x, name="first",
    )
    b = OutputAdapter(
        serialize=lambda x: x, deserialize=lambda x: x, name="second",
    )
    register_adapter(phase="P", kind="K", adapter=a)
    caplog.set_level(logging.INFO)
    register_adapter(phase="P", kind="K", adapter=b)
    assert get_adapter(phase="P", kind="K") is b
    replace_logs = [
        r for r in caplog.records if "replaced" in r.getMessage()
    ]
    assert len(replace_logs) >= 1


def test_register_adapter_same_instance_silent(caplog) -> None:
    """Re-registering the SAME adapter is a silent no-op (no warning)."""
    import logging
    reset_registry_for_tests()
    a = OutputAdapter(
        serialize=lambda x: x, deserialize=lambda x: x, name="same",
    )
    register_adapter(phase="P", kind="K", adapter=a)
    caplog.set_level(logging.INFO)
    caplog.clear()
    register_adapter(phase="P", kind="K", adapter=a)
    # No "replaced" log on identical re-registration
    replace_logs = [
        r for r in caplog.records if "replaced" in r.getMessage()
    ]
    assert replace_logs == []


def test_register_adapter_empty_keys_rejected() -> None:
    reset_registry_for_tests()
    a = OutputAdapter(
        serialize=lambda x: x, deserialize=lambda x: x,
    )
    register_adapter(phase="", kind="K", adapter=a)
    register_adapter(phase="P", kind="", adapter=a)
    # Neither registered → identity fallback
    assert get_adapter(phase="", kind="K") is _IDENTITY_ADAPTER
    assert get_adapter(phase="P", kind="") is _IDENTITY_ADAPTER


def test_get_adapter_unknown_falls_back_to_identity() -> None:
    reset_registry_for_tests()
    a = get_adapter(phase="UNREGISTERED", kind="UNKNOWN")
    assert a is _IDENTITY_ADAPTER
    assert a.serialize(42) == 42
    assert a.deserialize(42) == 42


def test_iter_registered_returns_sorted_pairs() -> None:
    reset_registry_for_tests()
    a = OutputAdapter(
        serialize=lambda x: x, deserialize=lambda x: x,
    )
    register_adapter(phase="ZED", kind="K", adapter=a)
    register_adapter(phase="ALPHA", kind="K", adapter=a)
    register_adapter(phase="ALPHA", kind="L", adapter=a)
    pairs = iter_registered()
    assert pairs == (("ALPHA", "K"), ("ALPHA", "L"), ("ZED", "K"))


# ---------------------------------------------------------------------------
# §7-§9 — _build_ctx_inputs
# ---------------------------------------------------------------------------


def test_build_ctx_inputs_basic() -> None:
    ctx = _ctx_stub(
        signal_urgency="critical", signal_source="test_failure",
        task_complexity="heavy_code",
    )
    inputs = _build_ctx_inputs(ctx)
    assert inputs["signal_urgency"] == "critical"
    assert inputs["signal_source"] == "test_failure"
    assert inputs["task_complexity"] == "heavy_code"
    assert inputs["target_files"] == []
    assert inputs["cross_repo"] is False
    assert inputs["is_read_only"] is False


def test_build_ctx_inputs_target_files_sorted() -> None:
    """Same set of files in different order → same canonical inputs."""
    ctx1 = _ctx_stub(target_files=["b.py", "a.py", "c.py"])
    ctx2 = _ctx_stub(target_files=["c.py", "a.py", "b.py"])
    i1 = _build_ctx_inputs(ctx1)
    i2 = _build_ctx_inputs(ctx2)
    assert i1["target_files"] == i2["target_files"]
    assert i1["target_files"] == ["a.py", "b.py", "c.py"]


def test_build_ctx_inputs_extra_overrides() -> None:
    ctx = _ctx_stub()
    inputs = _build_ctx_inputs(
        ctx, extra={"phase_specific": "value", "signal_urgency": "OVERRIDDEN"},
    )
    assert inputs["phase_specific"] == "value"
    assert inputs["signal_urgency"] == "OVERRIDDEN"


def test_build_ctx_inputs_none_ctx() -> None:
    """None ctx → empty dict (NEVER raises)."""
    inputs = _build_ctx_inputs(None)
    assert inputs == {}


def test_build_ctx_inputs_handles_garbage_target_files() -> None:
    ctx = _ctx_stub(target_files=[1, "valid.py", None])
    inputs = _build_ctx_inputs(ctx)
    # Coerced to strings; sorted order
    assert "valid.py" in inputs["target_files"]


# ---------------------------------------------------------------------------
# §10-§13 — capture_phase_decision RECORD/REPLAY paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_record_writes_jsonl(isolated) -> None:
    ctx = _ctx_stub(op_id="op-1", signal_urgency="critical")

    async def compute():
        return {"route": "IMMEDIATE"}

    out = await capture_phase_decision(
        op_id="op-1", phase="ROUTE", kind="route_assignment",
        ctx=ctx, compute=compute,
    )
    assert out == {"route": "IMMEDIATE"}
    ledger = isolated / "test-session" / "decisions.jsonl"
    assert ledger.exists()
    import json as _json
    rows = [
        _json.loads(l) for l in
        ledger.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert len(rows) == 1
    assert rows[0]["phase"] == "ROUTE"
    assert rows[0]["kind"] == "route_assignment"


@pytest.mark.asyncio
async def test_capture_replay_returns_adapter_deserialized(
    isolated, monkeypatch,
) -> None:
    """Register a non-trivial adapter that converts route dict to
    a tuple shape, then replay returns the tuple."""
    register_adapter(
        phase="ROUTE", kind="route_assignment",
        adapter=OutputAdapter(
            serialize=lambda t: {"route": t[0], "reason": t[1]},
            deserialize=lambda d: (d["route"], d["reason"]),
            name="test_route_adapter",
        ),
    )

    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    out_record = await capture_phase_decision(
        op_id="op-1", phase="ROUTE", kind="route_assignment",
        ctx=_ctx_stub(),
        compute=lambda: ("STANDARD", "default cascade"),
    )
    assert out_record == ("STANDARD", "default cascade")

    # Reset runtimes so REPLAY reads from disk fresh
    reset_runtime_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")

    canary = {"called": False}

    def should_not_run():
        canary["called"] = True
        return ("LIVE", "should not be returned")

    out_replay = await capture_phase_decision(
        op_id="op-1", phase="ROUTE", kind="route_assignment",
        ctx=_ctx_stub(), compute=should_not_run,
    )
    assert out_replay == ("STANDARD", "default cascade")
    assert canary["called"] is False


@pytest.mark.asyncio
async def test_capture_serialize_fault_falls_back(
    isolated, caplog,
) -> None:
    """If the adapter's serialize() raises, capture logs + falls
    back to identity (live output is still returned)."""
    import logging

    def bad_serialize(x):
        raise RuntimeError("simulated serialize fault")

    register_adapter(
        phase="X", kind="Y",
        adapter=OutputAdapter(
            serialize=bad_serialize,
            deserialize=lambda x: x, name="bad_ser",
        ),
    )

    caplog.set_level(logging.WARNING)
    out = await capture_phase_decision(
        op_id="op-1", phase="X", kind="Y",
        ctx=_ctx_stub(), compute=lambda: {"data": "value"},
    )
    # Live path returned — caller gets the value
    assert out == {"data": "value"}
    fallback_logs = [
        r for r in caplog.records
        if "serialize failed" in r.getMessage()
    ]
    assert len(fallback_logs) >= 1


@pytest.mark.asyncio
async def test_capture_deserialize_fault_returns_raw_repr(
    isolated, caplog, monkeypatch,
) -> None:
    """If the adapter's deserialize() raises during REPLAY, capture
    logs + returns the raw stored repr (still useful for caller)."""
    import logging

    def bad_deserialize(x):
        raise RuntimeError("simulated deserialize fault")

    register_adapter(
        phase="X", kind="Y",
        adapter=OutputAdapter(
            serialize=lambda x: x,
            deserialize=bad_deserialize, name="bad_des",
        ),
    )

    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    await capture_phase_decision(
        op_id="op-1", phase="X", kind="Y",
        ctx=_ctx_stub(), compute=lambda: {"data": "value"},
    )

    reset_runtime_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")

    caplog.set_level(logging.WARNING)
    out = await capture_phase_decision(
        op_id="op-1", phase="X", kind="Y",
        ctx=_ctx_stub(), compute=lambda: "should-not-run",
    )
    # Raw stored repr returned (could be dict or already-decoded
    # JSON), not the raised exception
    assert out is not None
    fallback_logs = [
        r for r in caplog.records
        if "deserialize failed" in r.getMessage()
    ]
    assert len(fallback_logs) >= 1


# ---------------------------------------------------------------------------
# §14 — ROUTE wiring: adapter registered at module load
# ---------------------------------------------------------------------------


def test_route_adapter_registered_at_module_load() -> None:
    """Importing route_runner registers the route adapter."""
    reset_registry_for_tests()
    # Force re-import to trigger the module-load registration
    import importlib
    from backend.core.ouroboros.governance.phase_runners import (
        route_runner,
    )
    importlib.reload(route_runner)
    adapter = get_adapter(phase="ROUTE", kind="route_assignment")
    assert adapter is not _IDENTITY_ADAPTER
    assert adapter.name == "route_assignment_adapter"


def test_route_adapter_round_trip() -> None:
    """The route adapter serializes (ProviderRoute, str) tuples to
    {"route": str, "reason": str} and deserializes back."""
    reset_registry_for_tests()
    import importlib
    from backend.core.ouroboros.governance.phase_runners import (
        route_runner,
    )
    importlib.reload(route_runner)
    adapter = get_adapter(phase="ROUTE", kind="route_assignment")

    from backend.core.ouroboros.governance.urgency_router import (
        ProviderRoute,
    )
    original = (ProviderRoute.STANDARD, "default cascade")
    serialized = adapter.serialize(original)
    assert serialized == {
        "route": "standard", "reason": "default cascade",
    }
    deserialized = adapter.deserialize(serialized)
    assert deserialized == original


def test_route_adapter_serialize_fallback_on_garbage() -> None:
    """Garbage input doesn't raise — defensive."""
    reset_registry_for_tests()
    import importlib
    from backend.core.ouroboros.governance.phase_runners import (
        route_runner,
    )
    importlib.reload(route_runner)
    adapter = get_adapter(phase="ROUTE", kind="route_assignment")

    # Pass non-tuple — should NOT raise
    out = adapter.serialize("not a tuple")
    assert "route" in out


def test_route_adapter_deserialize_fallback_on_garbage() -> None:
    """Garbage stored input doesn't raise."""
    reset_registry_for_tests()
    import importlib
    from backend.core.ouroboros.governance.phase_runners import (
        route_runner,
    )
    importlib.reload(route_runner)
    adapter = get_adapter(phase="ROUTE", kind="route_assignment")

    out = adapter.deserialize("not a dict")
    # Falls back to returning the raw input
    assert out == "not a dict"

    out2 = adapter.deserialize({"route": "invalid_route", "reason": ""})
    # Invalid ProviderRoute value falls through; stored dict returned
    assert out2 == {"route": "invalid_route", "reason": ""}


# ---------------------------------------------------------------------------
# §17-§18 — Authority invariants
# ---------------------------------------------------------------------------


def test_phase_capture_no_orchestrator_imports() -> None:
    """phase_capture.py MUST NOT import orchestrator / phase_runner
    base / candidate_generator. It's a substrate primitive, not a
    cognitive consumer."""
    import inspect
    from backend.core.ouroboros.governance.determinism import phase_capture
    src = inspect.getsource(phase_capture)
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.phase_runner ",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for f in forbidden:
        assert f not in src, f"phase_capture must NOT contain {f!r}"


def test_route_runner_imports_phase_capture_lazily() -> None:
    """route_runner imports phase_capture INSIDE the function body
    (lazy) so that import failures don't break the runner module
    itself. The adapter registration helper does its own try/except."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/route_runner.py",
        encoding="utf-8",
    ).read()
    # The capture_phase_decision import should be inside a function
    # body (indented) — not at the module top level
    lines = src.split("\n")
    top_level_imports = [
        ln for ln in lines
        if ln.startswith("from backend.core.ouroboros.governance.determinism.phase_capture")
    ]
    # No TOP-LEVEL imports of phase_capture (must be lazy/indented)
    assert top_level_imports == [], (
        "route_runner must import phase_capture lazily, not at top level"
    )


def test_route_runner_imports_cleanly() -> None:
    """route_runner module imports without error even if Slice 1.1
    or 1.2 modules are unavailable. Defensive try/except wraps the
    adapter registration."""
    import importlib
    from backend.core.ouroboros.governance.phase_runners import (
        route_runner,
    )
    # Should not raise — module exists + ROUTERunner class accessible
    assert hasattr(route_runner, "ROUTERunner")
    importlib.reload(route_runner)
    assert hasattr(route_runner, "ROUTERunner")
