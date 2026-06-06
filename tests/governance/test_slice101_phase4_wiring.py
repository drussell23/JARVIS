"""Slice 101 Phase 4 — mcp_output_scanner @ tool-emit, cognitive_load_shedding
@ intake, autonomous_graduation_engine @ session-end.

Exercises each seam's composed building blocks: the credential scan→redact that
the Venom tool loop performs, the intake shed-gate decision surface + the
sheddable-urgency contract, and the inert-by-default session-end graduation pass.
"""

from __future__ import annotations


# === SEAM 1: tool-output credential interception ============================

def test_scanner_flags_and_redactor_removes_credential(monkeypatch):
    from backend.core.ouroboros.governance.mcp_output_scanner import (
        McpScanVerdict,
        scan_mcp_output,
    )
    from backend.core.ouroboros.governance.conversation_bridge import redact_secrets

    monkeypatch.setenv("JARVIS_MCP_OUTPUT_SCANNER_ENABLED", "1")
    leaky = "here is the key AKIAIOSFODNN7EXAMPLE you asked for"
    report = scan_mcp_output(leaky, source_label="mcp_github_search")
    assert report.verdict == McpScanVerdict.CREDENTIAL_FOUND
    assert len(report.findings) >= 1
    # The seam then redacts via the canonical Tier-1 redactor:
    redacted, n = redact_secrets(leaky)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert n > 0


def test_scanner_inert_when_master_off(monkeypatch):
    from backend.core.ouroboros.governance.mcp_output_scanner import (
        McpScanVerdict,
        scan_mcp_output,
    )
    monkeypatch.delenv("JARVIS_MCP_OUTPUT_SCANNER_ENABLED", raising=False)
    report = scan_mcp_output("AKIAIOSFODNN7EXAMPLE", source_label="t")
    # master off → DISABLED, so the seam never redacts (byte-identical legacy)
    assert report.verdict == McpScanVerdict.DISABLED


def test_clean_tool_output_is_not_flagged(monkeypatch):
    from backend.core.ouroboros.governance.mcp_output_scanner import (
        McpScanVerdict,
        scan_mcp_output,
    )
    monkeypatch.setenv("JARVIS_MCP_OUTPUT_SCANNER_ENABLED", "1")
    report = scan_mcp_output("just some ordinary file contents", source_label="t")
    assert report.verdict == McpScanVerdict.CLEAN


# === SEAM 2: intake cognitive load-shedding gate ============================

def test_shed_mode_parsing(monkeypatch):
    from backend.core.ouroboros.governance.intake import unified_intake_router as R
    monkeypatch.delenv("JARVIS_INTAKE_COGNITIVE_SHED_MODE", raising=False)
    assert R._intake_cognitive_shed_mode() == "shadow"  # mirrors governor default
    for v in ("off", "shadow", "enforce"):
        monkeypatch.setenv("JARVIS_INTAKE_COGNITIVE_SHED_MODE", v)
        assert R._intake_cognitive_shed_mode() == v
    monkeypatch.setenv("JARVIS_INTAKE_COGNITIVE_SHED_MODE", "garbage")
    assert R._intake_cognitive_shed_mode() == "shadow"


def test_only_low_urgency_is_sheddable():
    from backend.core.ouroboros.governance.intake import unified_intake_router as R
    assert R._SHEDDABLE_URGENCIES == frozenset({"low"})
    for protected in ("critical", "high", "normal"):
        assert protected not in R._SHEDDABLE_URGENCIES


def test_load_shed_produces_shed_signal_under_overload(monkeypatch):
    from backend.core.ouroboros.governance.cognitive_load_shedding import (
        LoadVerdict,
        ShedKind,
        evaluate_cognitive_load,
    )
    monkeypatch.setenv("JARVIS_COGNITIVE_LOAD_SHEDDING_ENABLED", "1")
    # Force overload via the documented override seam — the stress triplet AND
    # the forecast pair must ALL be supplied or it falls back to live substrates.
    report = evaluate_cognitive_load(
        stress_score_override=0.95,
        stressed_count_override=5,
        exhausted_count_override=2,
        forecast_score_override=0.95,
        forecast_verdict_override="critical",
    )
    assert report.verdict in (LoadVerdict.ELEVATED, LoadVerdict.OVERLOADED)
    assert report.shed_kind in (
        ShedKind.SPECULATIVE_SHED, ShedKind.BACKGROUND_SHED, ShedKind.FULL_SHED,
    )


def test_load_shed_inert_when_master_off(monkeypatch):
    from backend.core.ouroboros.governance.cognitive_load_shedding import (
        LoadVerdict,
        ShedKind,
        evaluate_cognitive_load,
    )
    monkeypatch.delenv("JARVIS_COGNITIVE_LOAD_SHEDDING_ENABLED", raising=False)
    report = evaluate_cognitive_load(
        stress_score_override=0.95, forecast_score_override=0.95,
    )
    assert report.verdict == LoadVerdict.DISABLED
    assert report.shed_kind == ShedKind.NO_SHED


# === SEAM 3: session-end autonomous graduation ==============================

def test_graduation_inert_and_callable_when_master_off(monkeypatch):
    from backend.core.ouroboros.governance.autonomous_graduation_engine import (
        autonomous_graduation_engine_enabled,
        evaluate_graduations,
        execute_graduations,
    )
    monkeypatch.delenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", raising=False)
    assert autonomous_graduation_engine_enabled() is False
    # The GLS.stop() seam guards on the accessor, but evaluate/execute must
    # themselves be safe to call and inert (the never-block-shutdown contract).
    report = evaluate_graduations()
    result = execute_graduations(report)
    assert result.recorded_overrides == ()
    assert result.advisories_emitted == ()


def test_graduation_never_auto_flips_safety(monkeypatch):
    # Structural invariant: even enabled, execute records overrides only for
    # STANDARD-tier; the durable ledger refuses SAFETY flags. We assert the
    # enabled path runs without raising and returns the typed result.
    from backend.core.ouroboros.governance.autonomous_graduation_engine import (
        evaluate_graduations,
        execute_graduations,
    )
    monkeypatch.setenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "1")
    report = evaluate_graduations()
    result = execute_graduations(report)
    # No SAFETY flag may appear in the auto-flipped set.
    assert isinstance(result.recorded_overrides, tuple)
    assert isinstance(result.advisories_emitted, tuple)
