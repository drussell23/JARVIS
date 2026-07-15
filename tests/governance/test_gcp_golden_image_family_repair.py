"""Repair: GCP golden-image family drift + degraded-MTTR bulletproofing.

Root cause: gcp_vm_manager hardcoded ``jarvis-prime-golden`` in THREE places
(config default, bake name, list filter) while the real images are baked into
``jarvis-brain-golden`` — so the failover VM pointed at a phantom family and
either silently cold-booted or failed at VM-create. On transient Spot nodes
that defeats agile failover.

The fix: the family is inherited from the config layer (env → the centralized
gcp_compute_rest constant), no inline literal; the resolution VERIFIES the
family resolves before committing to golden mode; and an unresolvable family
raises a LOUD degraded-MTTR alarm to the observability event bus, never silent.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.gcp_vm_manager import (
    GCPVMManager,
    _default_golden_image_family,
)


# ── the family is inherited from config, not hardcoded ───────────────

def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_GCP_GOLDEN_IMAGE_FAMILY", "my-custom-family")
    assert _default_golden_image_family() == "my-custom-family"


def test_unset_resolves_to_centralized_constant(monkeypatch):
    """No inline literal default — it falls back to the ONE canonical constant
    the REST layer + bake script already share."""
    monkeypatch.delenv("JARVIS_GCP_GOLDEN_IMAGE_FAMILY", raising=False)
    from backend.core.ouroboros.governance.gcp_compute_rest import (
        _DEFAULT_BRAIN_IMAGE_FAMILY,
    )
    val = _default_golden_image_family()
    assert val == _DEFAULT_BRAIN_IMAGE_FAMILY == "jarvis-brain-golden"
    assert "prime-golden" not in val  # the drift value is gone


def test_whitespace_env_falls_through_to_constant(monkeypatch):
    monkeypatch.setenv("JARVIS_GCP_GOLDEN_IMAGE_FAMILY", "   ")
    assert _default_golden_image_family() == "jarvis-brain-golden"


# ── the degraded-MTTR alarm reaches the event bus ────────────────────

def test_publish_golden_image_degraded_emits(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    from backend.core.ouroboros.governance import ide_observability_stream as S
    S.reset_default_broker()
    broker = S.get_default_broker()
    sub = broker.subscribe()
    eid = S.publish_golden_image_degraded({
        "family": "jarvis-prime-golden", "project": "p",
        "reason": "family_unresolved", "fallback_mode": "cold_boot_fallback",
    })
    assert eid is not None
    hist = broker.recent_history(
        limit=5, event_type=S.EVENT_TYPE_GOLDEN_IMAGE_DEGRADED,
    )
    assert len(hist) == 1
    assert hist[0].payload["family"] == "jarvis-prime-golden"
    S.reset_default_broker()


def test_event_type_registered():
    from backend.core.ouroboros.governance import ide_observability_stream as S
    assert S.EVENT_TYPE_GOLDEN_IMAGE_DEGRADED in S._VALID_EVENT_TYPES


def test_emit_degraded_is_loud_and_failsoft(monkeypatch, caplog):
    """_emit_golden_image_degraded logs CRITICAL + fires the SSE, and NEVER
    raises into the VM-create path."""
    import logging
    emitted = {}
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    from backend.core.ouroboros.governance import ide_observability_stream as S
    monkeypatch.setattr(
        S, "publish_golden_image_degraded",
        lambda d: emitted.update(d) or "eid",
    )
    with caplog.at_level(logging.CRITICAL):
        # self is unused by the method → any object works.
        GCPVMManager._emit_golden_image_degraded(
            object(), family="jarvis-prime-golden", project="p",
            reason="not_found", fallback_enabled=True,
        )
    assert emitted.get("family") == "jarvis-prime-golden"
    assert emitted.get("mttr_impact") == "cold_boot"
    assert any("MTTR-DEGRADED" in r.getMessage() for r in caplog.records)


def test_emit_degraded_never_raises_when_bus_down(monkeypatch):
    from backend.core.ouroboros.governance import ide_observability_stream as S
    def _boom(_d):
        raise RuntimeError("bus down")
    monkeypatch.setattr(S, "publish_golden_image_degraded", _boom)
    # Must not raise.
    GCPVMManager._emit_golden_image_degraded(
        object(), family="f", project="p", reason="r", fallback_enabled=False,
    )


# ── drift-prevention source guards (no re-hardcoding) ────────────────

def test_no_live_prime_golden_literal():
    """Every LIVE ``jarvis-prime-golden`` literal is gone — the only surviving
    textual reference is the docstring that documents the old drift."""
    import backend.core.gcp_vm_manager as M
    full = inspect.getsource(M)
    assert full.count("jarvis-prime-golden") == 1  # the drift-doc comment only
    # The resolver's fallback returns the CANONICAL family, never the drift one.
    resolver_src = inspect.getsource(_default_golden_image_family)
    assert 'return "jarvis-brain-golden"' in resolver_src
    assert 'return "jarvis-prime-golden"' not in resolver_src


def test_bake_and_list_inherit_the_family():
    """Both the create (bake) and list paths must derive the family from
    config, so create/consume can never drift apart again."""
    import backend.core.gcp_vm_manager as M
    full = inspect.getsource(M)
    # Bake name is config-derived.
    assert 'f"{self.config.golden_image_family}-{timestamp}"' in full
    # List filter tracks the configured family.
    assert "image.name.startswith(self.config.golden_image_family)" in full


def test_resolution_verifies_before_committing_golden_mode():
    """The resolution must call get_from_family to VERIFY, and on failure set
    use_golden_image_mode=False + emit the degraded alarm — not commit golden
    mode to a phantom source."""
    import backend.core.gcp_vm_manager as M
    full = inspect.getsource(M)
    assert "_emit_golden_image_degraded" in full
    # The proven-unresolvable path explicitly de-commits golden mode.
    assert "use_golden_image_mode = False" in full
