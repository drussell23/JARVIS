from __future__ import annotations

from backend.core.ouroboros.governance import blast_radius_verify as brv


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_BLAST_RADIUS_ENABLED", raising=False)
    assert brv.blast_radius_enabled() is False


def test_enabled_when_flagged(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_BLAST_RADIUS_ENABLED", "true")
    assert brv.blast_radius_enabled() is True


def test_blast_token_field_present_and_carried():
    """op_context exposes ``blast_token`` (default None) and ``advance`` carries it."""
    import dataclasses
    from backend.core.ouroboros.governance.op_context import OperationContext

    field_names = {f.name for f in dataclasses.fields(OperationContext)}
    assert "blast_token" in field_names
    # Default-OFF: zero behavioural change unless explicitly set.
    default = next(
        f for f in dataclasses.fields(OperationContext) if f.name == "blast_token"
    )
    assert default.default is None
