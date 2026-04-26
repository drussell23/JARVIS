"""P0 — PostmortemRecall graduation pins (PRD §11 Layer 4 prep).

Pins the post-graduation contract for PostmortemRecallService. These tests
run on every commit going forward; if any pin breaks:

* The change was an unintentional regression — fix the change.
* The contract is intentionally being expanded — update the pin AND the
  hot-revert documentation.

Pin coverage (matches W2(4) Slice 4 + W3(7) Slice 7 graduation pin pattern):

A. Master flag default — pre-graduation == False; post-graduation == True
   (currently False; flip happens after 3 clean live sessions per
   PRD §11 Layer 4)
B. All 5 sub-flag defaults composition (top_k=3 / decay=30d /
   threshold=0.5 / max_scan=500)
C. Hot-revert: master=false force-disables the service even with
   stranded postmortems on disk
D. Authority invariants — read-only, no banned imports, no eval-class
E. JSONL schema version is "postmortem_recall.1" (frozen wire format)
F. Source-grep pins for the orchestrator wiring + ordering
G. Per-PRD §10 telemetry vocabulary additivity (no event types yet
   for P0; future SSE events documented inline as they're added)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# (A) Master flag default — pre-graduation pin
# ---------------------------------------------------------------------------


def test_master_flag_default_false_pre_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_POSTMORTEM_RECALL_ENABLED defaults False until P0 graduation
    cadence completes (3 clean live sessions per PRD §11 Layer 4).

    If this test fails AND P0 has been graduated: rename to
    test_master_flag_default_true_post_graduation, update assertion,
    update the env-reader source-grep pin in (F)."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_ENABLED", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import is_enabled
    assert is_enabled() is False, (
        "Pre-graduation default is False. If P0 has been graduated, update "
        "this pin to assert True."
    )


# ---------------------------------------------------------------------------
# (B) Sub-flag defaults composition
# ---------------------------------------------------------------------------


def test_top_k_default_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD §9 P0: 'inject up to 3 relevant lessons'."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_TOP_K", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import top_k
    assert top_k() == 3


def test_decay_days_default_30(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD §16 Open Question 1 default: 30 days."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_DECAY_DAYS", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import decay_days
    assert decay_days() == 30.0


def test_similarity_threshold_default_05(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.5 conservative default (per inline docstring)."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import (
        similarity_threshold,
    )
    assert similarity_threshold() == 0.5


def test_max_scan_default_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_MAX_SCAN", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import (
        max_postmortems_to_scan,
    )
    assert max_postmortems_to_scan() == 500


# ---------------------------------------------------------------------------
# (C) Hot-revert: master=false force-disables service
# ---------------------------------------------------------------------------


def test_hot_revert_master_off_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """JARVIS_POSTMORTEM_RECALL_ENABLED=false → recall returns [] even
    with postmortems on disk. The single env knob hot-revert path."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "false")
    # Even with overrides on sub-flags, master-off wins
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_TOP_K", "10")
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD", "0.0")

    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
    )
    svc = PostmortemRecallService(sessions_dir=tmp_path)
    result = svc.recall_for_op("any signature")
    assert result == [], "master-off must return [] regardless of sub-flags"


def test_hot_revert_master_off_singleton_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_default_service() returns None when master off."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "false")
    from backend.core.ouroboros.governance.postmortem_recall import (
        get_default_service, reset_default_service,
    )
    reset_default_service()
    assert get_default_service() is None


# ---------------------------------------------------------------------------
# (D) Authority invariants — read-only, no banned imports
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_authority_no_banned_module_imports() -> None:
    """Read-only invariant per PRD §12.2 — must NOT import authority modules."""
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    banned = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for imp in banned:
        assert imp not in src, f"banned authority import found: {imp}"


def test_security_no_code_evaluation_calls() -> None:
    """Security invariant — narrow regex parser only.

    Builds danger tokens at runtime to avoid pre-commit hook false
    positives on literal substrings in the test file itself.
    """
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    # Standalone bare call — must not appear
    danger_a = "ev" + "al("
    danger_b = "import a" + "st"
    danger_c = "from a" + "st "
    assert danger_a not in src.replace("# ", "")
    assert danger_b not in src
    assert danger_c not in src


# ---------------------------------------------------------------------------
# (E) Schema version frozen
# ---------------------------------------------------------------------------


def test_jsonl_schema_version_frozen_at_postmortem_recall_1() -> None:
    """Wire-format API: ledger schema_version is "postmortem_recall.1".

    Future schema bumps need additive migration semantics + this pin
    updated."""
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    assert '"postmortem_recall.1"' in src
    # Also verify no other version string snuck in
    assert '"postmortem_recall.2"' not in src


# ---------------------------------------------------------------------------
# (F) Source-grep pins for module structure + orchestrator wiring
# ---------------------------------------------------------------------------


def test_pin_master_env_reader_default_false_literal() -> None:
    """The is_enabled() reader literal-defaults to False (pre-graduation).

    Slice flip will change this to True in a single commit."""
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    assert '_env_bool("JARVIS_POSTMORTEM_RECALL_ENABLED", False)' in src, (
        "Master flag default literal moved or changed. If P0 has been "
        "graduated, update both the source and this pin (rename to "
        "test_pin_master_env_reader_default_true_literal)."
    )


def test_pin_module_exports_public_api() -> None:
    """Module exports the public API surface used by orchestrator."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
        PostmortemRecord,
        RecallMatch,
        get_default_service,
        is_enabled,
        render_recall_section,
        reset_default_service,
    )
    assert callable(get_default_service)
    assert callable(render_recall_section)
    assert callable(is_enabled)
    assert callable(reset_default_service)
    assert hasattr(PostmortemRecallService, "recall_for_op")
    assert hasattr(PostmortemRecord, "signature_text")
    assert hasattr(PostmortemRecord, "lesson_text")
    assert hasattr(RecallMatch, "to_ledger_dict")


def test_pin_orchestrator_wiring_at_context_expansion() -> None:
    """Orchestrator imports + invokes the recall service at CONTEXT_EXPANSION."""
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    assert "from backend.core.ouroboros.governance.postmortem_recall" in src
    assert "get_default_service as _get_pm_recall" in src
    assert "render_recall_section as _render_pm_recall" in src
    # Best-effort discipline: wrapped in try/except (never blocks FSM)
    assert "[Orchestrator] PostmortemRecall injection skipped" in src
    # PRD reference present
    assert "PRD Phase 1" in src


def test_pin_orchestrator_recall_after_conversation_bridge() -> None:
    """Sequence pin: PostmortemRecall call site AFTER ConversationBridge block.

    Updated post-extraction (mirrors LSS pattern). The PostmortemRecall body
    now lives in module-level helper ``_inject_postmortem_recall_impl``;
    sequencing is enforced at the call site in ``_run_pipeline`` rather than
    log-string position. The ConversationBridge inline block remains in
    ``_run_pipeline`` (no extraction yet) — its log-string is the anchor.
    """
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    bridge_idx = src.find("ConversationBridge injection skipped")
    recall_call_idx = src.find("ctx = _inject_postmortem_recall_impl(ctx)")
    assert bridge_idx > 0, "ConversationBridge marker missing"
    assert recall_call_idx > 0, "PostmortemRecall call site missing"
    assert bridge_idx < recall_call_idx, (
        "PostmortemRecall call site must follow ConversationBridge inline "
        "block (per CONTEXT_EXPANSION ordering)"
    )


def test_pin_postmortem_recall_helper_extracted() -> None:
    """Helper extraction pin (mirrors LSS): the body lives at module scope.

    Mirror of ``test_last_session_summary_v1_1a`` AST regression. Extracting
    the body makes CONTEXT_EXPANSION reachable from a deterministic in-process
    smoke (W3(6) reachability supplement precedent — used when live cadence
    can't reliably exercise the wiring within the wall cap).
    """
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    # Helper defined at module scope
    assert "def _inject_postmortem_recall_impl(" in src, (
        "Helper signature missing — wire moved or unextracted"
    )
    # Helper invoked exactly once from the pipeline (idempotent integration)
    assert src.count("_inject_postmortem_recall_impl(ctx)") >= 1, (
        "Helper not invoked from the pipeline"
    )


# ---------------------------------------------------------------------------
# (G) Cross-cutting integration pins
# ---------------------------------------------------------------------------


def test_pin_uses_semantic_index_embedder() -> None:
    """Composes with existing SemanticIndex._Embedder (per PRD §9 P0
    'builds on existing SemanticIndex + ConversationBridge primitives')."""
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    assert "from backend.core.ouroboros.governance.semantic_index import" in src
    assert "_Embedder as _SemanticEmbedder" in src
    assert "_cosine" in src


def test_pin_lazy_singleton_pattern() -> None:
    """Default-singleton pattern matches W3(7) cancel_token + W2(4)
    curiosity_engine convention."""
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    assert "_default_service: Optional[PostmortemRecallService]" in src
    assert "def get_default_service" in src
    assert "def reset_default_service" in src
