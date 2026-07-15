"""Slice 21 — execution-stall fixes for the bt-2026-07-15-063421 classes.

Three fixes, each pinned here:

  * Fix A1 — the full-prompt builder callers report preloaded-prompt
    exploration credit (`preloaded_out`) so a tool-less route can satisfy
    the Iron Gate with the file content it genuinely embedded. The RT
    full-builder omission was the 11× ``no_forward_progress`` root cause.
  * Fix A2 — capability-aware halt in the GENERATE retry handler: when the
    op structurally cannot make tool calls (the provider's own Slice-226
    suppression predicate) AND earned zero credit, the retry is
    deterministically unresolvable — terminal
    ``exploration_impossible_no_capability`` with the exact context frame,
    instead of a futile retry + an EC8 trip.
  * Fix B — evidence provenance in the OperationAdvisor: a synthetic
    conservative blast cap (Slice 12T scan-skip placeholder) may contribute
    caution but can never satisfy a hard-BLOCK predicate, and the reason
    string says the scan was skipped. The placeholder-50 hard blocks were
    the 5× ``advisor_blocked`` root cause.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import operation_advisor as OA
from backend.core.ouroboros.governance.operation_advisor import (
    AdvisoryDecision,
    OperationAdvisor,
)


# ═════════════════════════════════════════════════════════════════════
# Fix A1 — preloaded-prompt credit flows from every full-builder caller
# ═════════════════════════════════════════════════════════════════════


def _src(module) -> str:
    return inspect.getsource(module)


def test_builder_appends_preloaded_for_real_files(tmp_path):
    """Contract of the credit itself: a target file whose real content is
    embedded lands in preloaded_out; a nonexistent file does not."""
    from backend.core.ouroboros.governance.providers import (
        _build_codegen_prompt,
    )

    real = tmp_path / "mod.py"
    real.write_text("def f():\n    return 1\n", encoding="utf-8")

    class _Ctx:
        op_id = "op-test-a1"
        target_files = ["mod.py", "ghost.py"]
        description = "test"
        cross_repo = False
        is_read_only = False
        provider_route = "background"
        task_complexity = "simple"
        intake_evidence_json = ""
        telemetry = None
        signal_source = "doc_staleness"
        repo = ""

    out: list = []
    prompt = _build_codegen_prompt(
        _Ctx(), repo_root=tmp_path, provider_route="background",
        preloaded_out=out,
    )
    assert "mod.py" in prompt
    assert out == ["mod.py"], (
        f"embedded real file must earn credit; ghost must not — got {out}"
    )


def test_rt_full_builder_passes_preloaded_out():
    """THE bt-2026-07-15 root cause: _generate_realtime's full-builder
    branch must report credit like its lean branch and the batch path."""
    import backend.core.ouroboros.governance.doubleword_provider as DW
    src = inspect.getsource(DW.DoublewordProvider._generate_realtime)
    # Every _build_codegen_prompt call inside the RT path carries the sink.
    chunks = src.split("_build_codegen_prompt(")[1:]
    full_builder_calls = [c[:400] for c in chunks]
    assert full_builder_calls, "RT path must build prompts"
    for call in full_builder_calls:
        assert "preloaded_out=_preloaded_files" in call, (
            "RT full-builder call dropped the exploration-credit sink "
            "(the 11× no_forward_progress class)"
        )


def test_heavy_lane_passes_and_attaches_credit():
    import backend.core.ouroboros.governance.doubleword_provider as DW
    src = inspect.getsource(
        DW.DoublewordProvider._generate_heavy_nonstreaming
    )
    assert "preloaded_out=_preloaded_files" in src
    assert "prompt_preloaded_files=tuple(_preloaded_files)" in src


def test_claude_full_builder_kwargs_carry_credit():
    """Both providers.py full-builder kwargs dicts include the sink, and
    the offload-retry path resets it against double-count."""
    import backend.core.ouroboros.governance.providers as P
    src = _src(P)
    kwarg_blocks = [
        c[:700] for c in src.split("_prompt_kwargs = dict(")[1:]
    ]
    assert len(kwarg_blocks) >= 2, "expected both Claude full-builder sites"
    for block in kwarg_blocks:
        assert "preloaded_out=_preloaded_files" in block, (
            "a Claude full-builder kwargs dict dropped the credit sink"
        )
    assert src.count("_preloaded_files.clear()") >= 2, (
        "offload-retry paths must reset credit to avoid double-append"
    )


# ═════════════════════════════════════════════════════════════════════
# Fix A2 — capability-aware halt (no unresolvable retry)
# ═════════════════════════════════════════════════════════════════════


def test_suppression_predicate_truth_table():
    """The halt keys off the provider's OWN capability predicate — pin the
    four decisive rows so a predicate change is a visible contract change."""
    from backend.core.ouroboros.governance.exploration_engine import (
        compute_tool_loop_suppressed,
    )

    def f(route, read_only):
        return compute_tool_loop_suppressed(
            complexity="simple", route=route,
            is_bg_terminal_worker=False, has_repair_context=False,
            is_read_only=read_only,
        )

    assert f("background", True) is True      # soak class — halt CAN fire
    assert f("speculative", False) is True    # never lifts — halt CAN fire
    # Tools available → retry feedback is actionable → halt must NOT fire.
    assert f("background", False) is False    # write-intent lift
    assert f("standard", False) is False


def test_raise_sites_attach_structural_facts():
    import backend.core.ouroboros.governance.orchestrator as O
    src = _src(O)
    # Legacy counter raise + ledger raise both carry the facts.
    assert src.count("structural_credit") >= 3  # 2 attach + 1 consume
    assert src.count("rejected_model_id") >= 3


def test_catch_site_halts_before_feedback():
    """The capability check must run BEFORE the retry-feedback builder —
    a halt that fires after feedback is assembled is dead code."""
    import backend.core.ouroboros.governance.orchestrator as O
    src = _src(O)
    halt = src.find("[Slice21CapabilityHalt]")
    feedback = src.find("CRITICAL_SYSTEM_OVERRIDE>")
    assert halt != -1, "capability-halt block missing"
    assert feedback != -1
    assert halt < feedback, "halt must precede the retry-feedback builder"
    # The halt is a clean terminal, mirroring EC8's mechanics.
    tail = src[halt:halt + 4000]
    assert "exploration_impossible_no_capability" in tail
    assert "_l2_escape_terminal" in tail
    assert "_record_ledger" in tail
    # Mandate 4 — the context frame fields.
    for field in ("route=", "complexity=", "model=", "attempt=", "targets="):
        assert field in tail, f"context frame missing {field}"


def test_halt_uses_provider_own_predicate():
    """DRY mandate: the halt consults exploration_engine's predicate +
    dw_terminal_worker_policy — no redundant capability checker."""
    import backend.core.ouroboros.governance.orchestrator as O
    src = _src(O)
    halt_region = src[src.find("[Slice21CapabilityHalt]") - 4000:
                      src.find("[Slice21CapabilityHalt]") + 1000]
    assert "compute_tool_loop_suppressed" in halt_region
    assert "background_is_terminal_worker" in halt_region


# ═════════════════════════════════════════════════════════════════════
# Fix B — synthetic blast evidence can never hard-block
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture()
def advisor(tmp_path, monkeypatch):
    """Advisor reproducing the soak signature deterministically: coverage
    pinned to 0 (the scanner fail-opens to 100% on a bare tmp repo, which
    would mask the hard-block predicate under test), memory axis quiesced,
    module force-enabled."""
    monkeypatch.setattr(OA, "_ENABLED", True)
    # Pin the memory axis OFF so host state can't flip decisions in CI.
    monkeypatch.setattr(
        OA, "memory_headroom_factor", lambda: (0.0, "ok"), raising=False,
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8",
    )
    adv = OA.OperationAdvisor(project_root=tmp_path)
    # The soak signature: coverage MEASURED at 0% (the targets genuinely
    # had no tests). Deterministic pin for the decision math under test.
    monkeypatch.setattr(
        adv, "_compute_test_coverage", lambda *a, **k: 0.0,
    )
    return adv


def _advise(advisor, tmp_path=None, *, synthetic: bool):
    return advisor.advise(
        ("pkg/core.py",),
        "update core helper",
        op_id="op-test-b",
        is_read_only=False,
        _precomputed_blast_radius=50,
        _blast_is_synthetic=synthetic,
    )


def test_synthetic_blast_never_hard_blocks(advisor):
    """The 5× advisor_blocked reproduction: placeholder 50 + coverage 0
    must NOT satisfy the zero-coverage+extreme-blast hard block."""
    adv = _advise(advisor, synthetic=True)
    assert adv.decision != AdvisoryDecision.BLOCK, adv.reasons
    # It still escalates caution — the conservative signal is preserved.
    assert adv.decision in (
        AdvisoryDecision.CAUTION, AdvisoryDecision.ADVISE_AGAINST,
    )


def test_measured_blast_still_hard_blocks(advisor):
    """Byte-identical legacy: a MEASURED 50 with zero coverage keeps the
    hard block — Fix B narrows nothing about real evidence."""
    adv = _advise(advisor, synthetic=False)
    assert adv.decision == AdvisoryDecision.BLOCK
    assert any("BLOCKED" in r for r in adv.reasons)


def test_synthetic_reason_is_honest(advisor):
    """The reason string must say the scan was skipped — never narrate a
    placeholder as a measured import count."""
    adv = _advise(advisor, synthetic=True)
    joined = " | ".join(adv.reasons)
    assert "unmeasured" in joined.lower() or "scan skipped" in joined.lower()
    assert "files import these targets" not in joined


def test_measured_reason_unchanged(advisor):
    adv = _advise(advisor, synthetic=False)
    joined = " | ".join(adv.reasons)
    assert "50 files import these targets" in joined


def test_adaptive_armor_synthetic_caps_at_caution(advisor, monkeypatch):
    """Under real host memory pressure, synthetic blast may raise CAUTION
    but the armor's BLOCK arm requires measured evidence."""
    monkeypatch.setattr(
        OA, "memory_headroom_factor", lambda: (0.8, "high"), raising=False,
    )
    adv = _advise(advisor, synthetic=True)
    assert adv.decision != AdvisoryDecision.BLOCK, adv.reasons
    adv_measured = _advise(advisor, synthetic=False)
    assert adv_measured.decision == AdvisoryDecision.BLOCK


def test_classify_runner_threads_synthetic_flag():
    """All four Slice-12T placeholder call sites declare the provenance."""
    import backend.core.ouroboros.governance.phase_runners.classify_runner as CR
    src = _src(CR)
    caps = src.count("_precomputed_blast_radius=_cap,")
    flags = src.count("_blast_is_synthetic=True,")
    assert caps == 4 and flags == 4, (caps, flags)


def test_default_is_measured_semantics():
    """Callers that don't pass the flag get pre-Slice-21 behavior —
    the kwarg defaults False (measured)."""
    sig = inspect.signature(OperationAdvisor.advise)
    assert sig.parameters["_blast_is_synthetic"].default is False
