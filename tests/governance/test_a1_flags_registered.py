from __future__ import annotations

from backend.core.ouroboros.governance.flag_registry import ensure_seeded


def test_iron_triad_flags_registered():
    reg = ensure_seeded()
    names = {s.name for s in reg.list_all()}
    for f in (
        "JARVIS_A1_TOKEN_ENFORCER_ENABLED",
        "JARVIS_A1_SANDBOX_LOCK_ENABLED",
        "JARVIS_A1_BLAST_RADIUS_ENABLED",
        "JARVIS_A1_PR_LINTER_ENABLED",
        "JARVIS_TOKEN_AUDIT_ENABLED",
    ):
        assert f in names, f"{f} not registered"
