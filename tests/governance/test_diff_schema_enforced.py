"""2b.1-diff is enforced for existing files; a failed hunk is realigned, not
re-emitted whole.

The 2026-10-09 soak asked for whole files 71 times and diffs 6. Three causes:
per-attempt capability evidence (one op's retry burst demoted the model; see
test_served_model_capability), the orchestrator's Slice 235 governor turning
every failed diff's retry into a whole-file rewrite (contradicting the
provider's own realignment), and a log line that could not say which.
"""
from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest


def test_the_degrade_switch_defaults_off(monkeypatch) -> None:
    from backend.core.ouroboros.governance.orchestrator import (
        _diff_failure_degrades_to_full_content,
    )
    monkeypatch.delenv("JARVIS_DIFF_FAILURE_DEGRADES_TO_FULL_CONTENT", raising=False)
    assert _diff_failure_degrades_to_full_content() is False
    monkeypatch.setenv("JARVIS_DIFF_FAILURE_DEGRADES_TO_FULL_CONTENT", "true")
    assert _diff_failure_degrades_to_full_content() is True


def test_a_failed_diff_retry_realigns_unless_the_legacy_switch_is_on() -> None:
    """Wiring pin: the override is set ONLY on the legacy branch; the default
    branch pins the failed hunk's region for the retry and asks for a diff."""
    from backend.core.ouroboros.governance import orchestrator
    src = inspect.getsource(orchestrator)
    start = src.index("_degrade = _diff_failed and _diff_failure_degrades_to_full_content()")
    block = src[start:src.index("# Inject re-plan if available", start)]
    legacy, rest = block.split("elif _diff_failed:", 1)
    assert 'force_full_content_override"] = True' in legacy
    assert "force_full_content_override" not in rest
    assert "pin_failed_hunk(ctx.op_id" in rest and "2b.1-diff" in rest


@pytest.mark.parametrize("override,capability,why", [
    (True, "full_content_and_diff", "retry_override"),
    (False, "full_content_only", "capability"),
    (False, "full_content_and_diff", "creation"),
])
def test_the_schema_line_says_why_full_content_was_forced(caplog, override, capability, why) -> None:
    from backend.core.ouroboros.governance import providers
    op = f"op-why-{why}"
    providers._SCHEMA_DECISION_SEEN.discard(op)
    ctx = SimpleNamespace(
        op_id=op, target_files=("a.py",), force_full_content_override=override,
        telemetry=SimpleNamespace(routing_intent=SimpleNamespace(
            schema_capability=capability, served_model="qwen3-coder-ov:30b", brain_id="b")))
    with caplog.at_level(logging.WARNING):
        providers._announce_schema_decision(ctx, force_full_content=True, diff=False)
    line = next(r.getMessage() for r in caplog.records if "[Schema]" in r.getMessage())
    assert f"why={why}" in line and op in line, "the full op id, so a soak can join on it"
