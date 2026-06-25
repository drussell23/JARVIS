"""Tests for the Sovereign Failover Mesh Gap 3b -- Cryo-DLQ -> Prime routing.

Two halves:

  (1) provider_quarantine.quarantine_op STAMPS provider_override="gcp-jprime"
      on the sealed envelope so the Cryo-DLQ entry carries the J-Prime pin.

  (2) candidate_generator._honor_provider_override HONORS that pin on replay:
        * routes a pinned op straight to the awakened PrimeProvider;
        * FAIL-CLOSED -- when no Prime is wired (no awakened endpoint) it raises
          a terminal sentinel so the op STAYS SEALED in the DLQ and is NEVER
          re-routed to the dead DW lane;
        * empty override -> None (legacy cascade, byte-identical).

TDD with injected fakes -- ZERO real provider calls.
"""
from __future__ import annotations

import dataclasses
import importlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest


def _fresh_quarantine():
    for key in list(sys.modules.keys()):
        if "provider_quarantine" in key:
            del sys.modules[key]
    return importlib.import_module(
        "backend.core.ouroboros.governance.provider_quarantine"
    )


# ---------------------------------------------------------------------------
# (1) quarantine_op stamps provider_override onto the sealed envelope
# ---------------------------------------------------------------------------

def test_quarantine_op_stamps_override_on_mutable_ctx(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_PROVIDER_OVERRIDE", raising=False)
    mod = _fresh_quarantine()
    ctx = SimpleNamespace(op_id="op-ov-1")
    sealed = {}

    def fake_emit(op_id, **kw):  # noqa: ANN001
        pass

    def fake_append(envelope, *, reason, path=None):
        sealed["env"] = envelope
        sealed["override"] = getattr(envelope, "provider_override", None)

    with (
        mock.patch.object(mod, "_import_emit_sovereign_yield", return_value=fake_emit),
        mock.patch.object(mod, "_import_append_dlq", return_value=fake_append),
    ):
        ok = mod.quarantine_op(ctx, route="dw", telemetry={})

    assert ok is True
    assert sealed["override"] == "gcp-jprime"


def test_quarantine_op_stamps_override_on_frozen_dataclass(monkeypatch):
    """OperationContext is a frozen dataclass -- the stamp must use replace."""
    monkeypatch.delenv("JARVIS_FAILOVER_PROVIDER_OVERRIDE", raising=False)
    mod = _fresh_quarantine()
    from backend.core.ouroboros.governance.op_context import OperationContext

    ctx = OperationContext.create(target_files=("a.py",), description="x", op_id="op-ov-2")
    assert ctx.provider_override == ""  # default
    sealed = {}

    def fake_append(envelope, *, reason, path=None):
        sealed["override"] = getattr(envelope, "provider_override", None)

    with (
        mock.patch.object(mod, "_import_emit_sovereign_yield", return_value=lambda *a, **k: None),
        mock.patch.object(mod, "_import_append_dlq", return_value=fake_append),
    ):
        ok = mod.quarantine_op(ctx, route="dw", telemetry={})

    assert ok is True
    assert sealed["override"] == "gcp-jprime"


def test_quarantine_op_override_env_disable(monkeypatch):
    """Empty JARVIS_FAILOVER_PROVIDER_OVERRIDE -> no stamp (legacy)."""
    monkeypatch.setenv("JARVIS_FAILOVER_PROVIDER_OVERRIDE", "")
    mod = _fresh_quarantine()
    ctx = SimpleNamespace(op_id="op-ov-3")
    sealed = {}

    def fake_append(envelope, *, reason, path=None):
        sealed["override"] = getattr(envelope, "provider_override", None)

    with (
        mock.patch.object(mod, "_import_emit_sovereign_yield", return_value=lambda *a, **k: None),
        mock.patch.object(mod, "_import_append_dlq", return_value=fake_append),
    ):
        mod.quarantine_op(ctx, route="dw", telemetry={})

    assert sealed["override"] is None  # not stamped


# ---------------------------------------------------------------------------
# (2) _honor_provider_override at dispatch
# ---------------------------------------------------------------------------

def _deadline():
    return datetime.now(timezone.utc) + timedelta(seconds=120)


def _make_generator(jprime):
    """Build a CandidateGenerator with a fake primary + injected jprime."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    fake_primary = SimpleNamespace(provider_name="claude")
    return CandidateGenerator(primary=fake_primary, jprime=jprime)


async def test_override_routes_to_jprime(monkeypatch):
    """A pinned op routes straight to J-Prime and returns its result."""
    sentinel_result = SimpleNamespace(candidates=("patch",), provider_name="gcp-jprime")
    jprime = SimpleNamespace(provider_name="gcp-jprime")
    gen = _make_generator(jprime)

    called = {}

    async def fake_primacy(context, deadline, *, route_label, force=False):
        called["route_label"] = route_label
        called["force"] = force
        return sentinel_result

    monkeypatch.setattr(gen, "_try_jprime_primacy", fake_primacy)

    ctx = SimpleNamespace(op_id="op-r1", provider_override="gcp-jprime")
    result = await gen._honor_provider_override(ctx, _deadline())
    assert result is sentinel_result
    assert called["route_label"] == "failover_override"
    assert called["force"] is True


async def test_no_override_returns_none(monkeypatch):
    """Empty override -> None (legacy cascade continues)."""
    gen = _make_generator(SimpleNamespace(provider_name="gcp-jprime"))
    ctx = SimpleNamespace(op_id="op-r2", provider_override="")
    result = await gen._honor_provider_override(ctx, _deadline())
    assert result is None


async def test_override_failclosed_no_prime_provider():
    """Override set but NO Prime wired -> terminal raise (op stays sealed,
    NEVER routed to dead DW)."""
    gen = _make_generator(jprime=None)  # no Prime
    ctx = SimpleNamespace(op_id="op-r3", provider_override="gcp-jprime")
    with pytest.raises(RuntimeError) as exc:
        await gen._honor_provider_override(ctx, _deadline())
    assert "provider_override_unavailable:gcp-jprime" in str(exc.value)
    assert "no_prime_provider" in str(exc.value)


async def test_override_failclosed_jprime_no_candidates(monkeypatch):
    """Override set, Prime wired, but J-Prime declines -> terminal raise (NOT
    a DW cascade). Op stays sealed."""
    jprime = SimpleNamespace(provider_name="gcp-jprime")
    gen = _make_generator(jprime)

    async def fake_primacy(context, deadline, *, route_label, force=False):
        return None  # J-Prime declined

    monkeypatch.setattr(gen, "_try_jprime_primacy", fake_primacy)
    ctx = SimpleNamespace(op_id="op-r4", provider_override="gcp-jprime")
    with pytest.raises(RuntimeError) as exc:
        await gen._honor_provider_override(ctx, _deadline())
    assert "provider_override_unavailable:gcp-jprime:no_candidates" in str(exc.value)


async def test_unrecognized_override_falls_through(monkeypatch):
    """An unknown override value -> None (forward-compatible legacy cascade)."""
    gen = _make_generator(SimpleNamespace(provider_name="gcp-jprime"))
    ctx = SimpleNamespace(op_id="op-r5", provider_override="some-future-provider")
    result = await gen._honor_provider_override(ctx, _deadline())
    assert result is None


async def test_override_field_on_operation_context_roundtrips():
    """The provider_override field is a real, replace-able field on the frozen
    OperationContext (so the seal stamp is durable)."""
    from backend.core.ouroboros.governance.op_context import OperationContext

    ctx = OperationContext.create(target_files=("a.py",), description="x", op_id="op-r6")
    assert ctx.provider_override == ""
    pinned = dataclasses.replace(ctx, provider_override="gcp-jprime")
    assert pinned.provider_override == "gcp-jprime"
    # original unchanged (frozen).
    assert ctx.provider_override == ""
