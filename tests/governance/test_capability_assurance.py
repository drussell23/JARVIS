"""No op may run weaker than the envelope promised.

Every capability here degrades QUIETLY: the diff schema off means whole-file
re-emission; `ctx.telemetry is None` means `capability=?` and the 2b.1-diff
schema is structurally unreachable. In both cases the pipeline still runs and
still reports success, having produced the weaker thing — the evidence arrives
days later as a mangled docstring in a landed diff.

These tests pin the two things that matter: the assurance FIRES on real
degradation, and it stays SILENT when full content is genuinely correct. A
check that fired on legitimate multi-file ops would be turned off within a day,
which is the same as not having one.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.capability_assurance import (
    assert_generation_capability,
    enforcement_enabled,
    preflight_verdict,
)
from backend.core.ouroboros.governance.op_context import (
    HostTelemetry,
    OperationContext,
    RoutingIntentTelemetry,
    TelemetryContext,
)

FILE = "backend/core/ouroboros/governance/dw_capacity_probe.py"

ARMED = {
    "JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED": "true",
    "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED": "true",
    "JARVIS_REMOTE_PUSH_AIRGAP": "true",
}


@pytest.fixture
def armed(monkeypatch):
    for k, v in ARMED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JARVIS_CAPABILITY_ASSURANCE_ENFORCE", raising=False)


def _telemetry(capability="full_content_and_diff", served="qwen3-coder-ov:30b"):
    return TelemetryContext(
        local_node=HostTelemetry(
            schema_version="1.0", arch="x86_64", cpu_percent=1.0,
            ram_available_gb=8.0, pressure="NORMAL",
            sampled_at_utc="2026-09-08T00:00:00+00:00",
            sampled_monotonic_ns=1, collector_status="ok", sample_age_ms=0,
        ),
        routing_intent=RoutingIntentTelemetry(
            expected_provider="LOCAL_OV", policy_reason="PRIMARY",
            schema_capability=capability, served_model=served,
        ),
    )


def _ctx(files=(FILE,), telemetry=True, capability="full_content_and_diff"):
    ctx = OperationContext.create(
        target_files=tuple(files), description="d", op_id="op-cap-1",
    )
    return ctx.with_telemetry(_telemetry(capability)) if telemetry else ctx


# --------------------------------------------------------------------------
# Enforcement posture
# --------------------------------------------------------------------------

def test_enforcement_is_on_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_CAPABILITY_ASSURANCE_ENFORCE", raising=False)
    assert enforcement_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "off", "no"])
def test_enforcement_can_be_deliberately_disabled(monkeypatch, v):
    monkeypatch.setenv("JARVIS_CAPABILITY_ASSURANCE_ENFORCE", v)
    assert enforcement_enabled() is False


# --------------------------------------------------------------------------
# Preflight — refuse at the keystroke, before anything runs
# --------------------------------------------------------------------------

def test_a_fully_armed_single_file_goal_passes(armed):
    v = preflight_verdict(target_files=(FILE,), target_symbols=("_sym",))
    assert v.ok, v.render()


def test_the_diff_schema_being_off_is_caught(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "false")
    v = preflight_verdict(target_files=(FILE,), target_symbols=("_sym",))
    assert not v.ok
    assert v.checks["diff_schema"] is False
    assert "diff_schema" in v.render()
    assert "JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED" in v.reason


def test_a_goal_declaring_no_symbol_is_refused(armed):
    """With no declared symbol there is nothing the candidate must be proven
    to have CHANGED — a hallucinated no-op could not be refused."""
    v = preflight_verdict(target_files=(FILE,), target_symbols=())
    assert not v.ok
    assert v.checks["declared_symbols"] is False


def test_a_multi_file_goal_is_refused_for_the_diff_schema(armed):
    v = preflight_verdict(
        target_files=(FILE, "backend/other.py"), target_symbols=("_sym",),
    )
    assert not v.ok
    assert v.checks["single_file_scope"] is False


def test_an_open_airgap_is_a_capability_failure(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_REMOTE_PUSH_AIRGAP", "false")
    v = preflight_verdict(target_files=(FILE,), target_symbols=("_sym",))
    assert not v.ok
    assert v.checks["airgap"] is False


def test_the_verdict_always_renders_something_actionable(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "false")
    text = preflight_verdict(target_files=(FILE,), target_symbols=("s",)).render()
    assert "FAIL" in text and "diff_schema" in text


# --------------------------------------------------------------------------
# Runtime — the shape that actually bit us
# --------------------------------------------------------------------------

def test_missing_routing_admission_is_degradation_not_a_model_limit(armed):
    """`ctx.telemetry is None` is not the model saying it cannot diff — it is
    nobody having asked. That is the `capability=? brain=-` shape."""
    v = assert_generation_capability(
        _ctx(telemetry=False), force_full_content=False,
    )
    assert not v.ok
    assert "telemetry is None" in v.reason


def test_full_content_on_a_diff_capable_single_file_op_is_caught(armed):
    v = assert_generation_capability(_ctx(), force_full_content=True)
    assert not v.ok
    assert "silent capability degradation" in v.reason


def test_the_diff_path_passes(armed):
    v = assert_generation_capability(_ctx(), force_full_content=False)
    assert v.ok, v.render()


# --- and the cases where full content is CORRECT: must stay silent --------

def test_multi_file_scope_is_not_degradation(armed):
    v = assert_generation_capability(
        _ctx(files=(FILE, "backend/other.py")), force_full_content=True,
    )
    assert v.ok


def test_a_model_that_cannot_diff_is_not_degradation(armed):
    v = assert_generation_capability(
        _ctx(capability="full_content_only"), force_full_content=True,
    )
    assert v.ok


def test_the_flag_deliberately_off_is_not_degradation(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "false")
    v = assert_generation_capability(_ctx(), force_full_content=True)
    assert v.ok


@pytest.mark.parametrize("junk", [None, object(), "not-a-ctx"])
def test_runtime_check_never_raises(armed, junk):
    v = assert_generation_capability(junk, force_full_content=False)
    assert v is not None


# --------------------------------------------------------------------------
# Only a SANCTIONED op may be aborted
#
# The first version of this module aborted on ANY telemetry-less context. The
# moment another test left the diff flag armed in the environment, seven
# prompt-building tests started raising `capability_degraded` — because an
# ambient tool call, a probe and a unit test all legitimately build prompts
# with no routing admission. A check that fails on ordinary paths gets turned
# off, which is the same as not having one.
# --------------------------------------------------------------------------

def _sanctioned_ctx(**kw):
    import json

    ctx = OperationContext.create(
        target_files=(FILE,), description="d", op_id="op-cap-2",
        intake_evidence_json=json.dumps({"goal_id": "ov-prod-some-goal"}),
        **kw,
    )
    return ctx


def test_an_unsanctioned_op_is_reported_but_not_enforceable(armed):
    """A bare context with no goal pointer: still FLAGGED, never aborted."""
    v = assert_generation_capability(_ctx(telemetry=False), force_full_content=False)
    assert v.ok is False           # the operator still sees it
    assert v.enforceable is False  # but it must not abort the op


def test_a_sanctioned_op_is_enforceable(armed):
    v = assert_generation_capability(_sanctioned_ctx(), force_full_content=False)
    assert v.ok is False
    assert v.enforceable is True


def test_the_generation_seam_aborts_only_when_enforceable():
    """The guarantee this test was written for, now strictly stronger.

    It originally pinned ``enforcement_enabled() and _cap.enforceable`` so an
    ambient, telemetry-less prompt build could not raise. That still holds --
    ``is_fatal`` requires ``enforceable`` -- and a second condition was added
    on top: the degradation must also be one that prevents a usable candidate.

    The reason is live evidence. Conditioning on ``enforceable`` alone meant
    every SANCTIONED op aborted, which stayed invisible only while sanctioned
    ops never reached a worker. The moment queue precedence delivered one
    (bt-2026-09-08-202025), the Sentinel's own goal died in 64.79s with
    ``tokens=0`` for a fidelity loss it could have generated straight through.
    """
    import inspect

    from backend.core.ouroboros.governance import providers
    from backend.core.ouroboros.governance.capability_assurance import (
        FATAL, RECOVERABLE, CapabilityVerdict,
    )

    src = inspect.getsource(providers)
    assert "enforcement_enabled() and _cap.is_fatal" in src, (
        "the abort is not conditioned on the op being sanctioned AND the "
        "degradation being fatal — every telemetry-less prompt build in the "
        "process would raise, and every sanctioned op would die on a "
        "recoverable one"
    )
    # The original guarantee, asserted on behaviour rather than on spelling.
    assert CapabilityVerdict(
        False, "x", enforceable=False, severity=FATAL,
    ).is_fatal is False, "an unsanctioned op can still abort"
    assert CapabilityVerdict(
        False, "x", enforceable=True, severity=RECOVERABLE,
    ).is_fatal is False, "a sanctioned op still aborts on a recoverable loss"


# --------------------------------------------------------------------------
# Reachability — the seams must actually call this
# --------------------------------------------------------------------------

def test_the_generation_seam_consults_assurance():
    """A check nobody calls is this repo's most expensive failure shape."""
    import inspect

    from backend.core.ouroboros.governance import providers

    src = inspect.getsource(providers)
    assert "assert_generation_capability" in src
    idx = src.index("_announce_schema_decision(ctx, force_full_content")
    assert "assert_generation_capability" in src[idx:idx + 2000], (
        "assurance is imported but not called at the schema-decision seam"
    )


def test_the_goal_verb_consults_preflight():
    import inspect

    from backend.core.ouroboros.battle_test import harness

    src = inspect.getsource(harness.BattleTestHarness._inject_and_report)
    assert "preflight_verdict" in src
    assert "return" in src, "no refusal path — a failed preflight must not dispatch"
