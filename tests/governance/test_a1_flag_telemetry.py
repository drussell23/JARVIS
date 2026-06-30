"""Dynamic 12-flag telemetry (Part D of the final A1 audit fix).

The graduation-flag audit failed because no signal evidenced the flags being
evaluated -> all UNVERIFIABLE. Instead of scattering per-flag log lines through
business logic, a centralized boot hook iterates the canonical CADENCE_POLICY
flag set, evaluates each flag's state, and emits ONE structured `[A1FlagAudit]`
block. The auditor parses that block by flag NAME (DRY -- single source of truth,
no family-marker duplication) and credits observed_evaluated. Real gate
false-rejections still dominate (REJECTED wins in the per-flag verdict).
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = str((Path(__file__).parent.parent.parent).resolve())
_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())
for _p in (_REPO_ROOT, _SCRIPTS_DIR, os.path.join(_REPO_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS_DIR, name + ".py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Emitter (flag_registry.emit_a1_graduation_telemetry)
# ---------------------------------------------------------------------------

def test_emitter_emits_structured_block_with_flag_names(monkeypatch, caplog):
    import backend.core.ouroboros.governance.flag_registry as fr
    monkeypatch.setenv("JARVIS_A1_FLAG_TELEMETRY_ENABLED", "true")
    # Pin the audited flag set so the test is deterministic + fast.
    monkeypatch.setenv(
        "JARVIS_A1_AUDIT_FLAGS",
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS,JARVIS_HYPOTHESIS_PROBE_ENABLED",
    )
    with caplog.at_level(logging.INFO):
        n = fr.emit_a1_graduation_telemetry()
    assert n == 2
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "[A1FlagAudit]" in blob
    assert "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS" in blob
    assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in blob


def test_emitter_gated_off(monkeypatch, caplog):
    import backend.core.ouroboros.governance.flag_registry as fr
    monkeypatch.setenv("JARVIS_A1_FLAG_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("JARVIS_A1_AUDIT_FLAGS", "JARVIS_X_ENABLED")
    with caplog.at_level(logging.INFO):
        n = fr.emit_a1_graduation_telemetry()
    assert n == 0
    assert "[A1FlagAudit]" not in "\n".join(r.getMessage() for r in caplog.records)


def test_emitter_never_raises(monkeypatch):
    import backend.core.ouroboros.governance.flag_registry as fr
    monkeypatch.setenv("JARVIS_A1_FLAG_TELEMETRY_ENABLED", "true")
    # No flags configured + import paths intact -> must still not raise.
    monkeypatch.setenv("JARVIS_A1_AUDIT_FLAGS", "")
    fr.emit_a1_graduation_telemetry()  # no exception


# ---------------------------------------------------------------------------
# Auditor: parse_flag_audit_line + credit observed_evaluated
# ---------------------------------------------------------------------------

def test_auditor_parses_flag_names_from_block():
    aud = _load_script("a1_graduation_auditor")
    line = "[A1FlagAudit] boot eval (2): JARVIS_FOO_ENABLED=true,JARVIS_BAR_ENABLED=false"
    names = aud.parse_flag_audit_line(line)
    assert set(names) == {"JARVIS_FOO_ENABLED", "JARVIS_BAR_ENABLED"}


def test_auditor_non_flag_line_returns_none():
    aud = _load_script("a1_graduation_auditor")
    assert aud.parse_flag_audit_line("[SemanticGuard] op=x findings=0") is None


def test_auditor_credits_observed_evaluated_from_block(monkeypatch):
    aud = _load_script("a1_graduation_auditor")
    monkeypatch.setenv("JARVIS_A1_AUDIT_FLAGS", "JARVIS_FOO_ENABLED,JARVIS_BAR_ENABLED")
    auditor = aud.A1GraduationAuditor(strict=True)
    # Pre-condition: both flags UNVERIFIABLE (no signal yet).
    auditor.ingest_log_line("[A1FlagAudit] boot eval (2): JARVIS_FOO_ENABLED=true,JARVIS_BAR_ENABLED=false")
    assert auditor.flags["JARVIS_FOO_ENABLED"].observed_evaluated is True
    assert auditor.flags["JARVIS_BAR_ENABLED"].observed_evaluated is True
