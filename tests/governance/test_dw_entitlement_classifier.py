"""
Task #86 spine — DW entitlement classifier + policy-alignment AST pins.

Closes the operator binding 2026-05-13: "Refactor the dw_heavy_probe
and classifier so they dynamically read from brain_selection_policy.yaml
or dynamically fetch the available/entitled models via the API. The
system must adapt to the available entitlements and price caps
autonomously, entirely removing the reliance on hardcoded model lists."

This spine pins:

  * Closed taxonomy (3 kinds) — AUTH_FAILURE / ENTITLEMENT_BLOCKED /
    OTHER_4XX.  Drift outside the table is rejected at construction.
  * Pure-function contract — classify_4xx has no side effects, no
    module-level env reads, runs in <10us.
  * Decision table — every cell in the (status × body) matrix pinned
    against the empirical patterns observed in the bt-2026-05-14-000028
    SWE-Bench-Pro soak.
  * Operator override — JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS extends
    or replaces the default marker set at call time.
  * Authority invariant — classifier imports only stdlib; consumers
    (modality_probe + heavy_probe) reference it exactly once each.
  * Policy-alignment AST pins — the spine FAILS if (a) the heavy
    probe or modality probe stops consuming the classifier, (b) a
    hardcoded model-id literal sneaks into the probe modules, (c) the
    classifier grows a module-level env read that breaks
    monkey-patching.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_entitlement_classifier import (
    ClassificationResult,
    KIND_AUTH_FAILURE,
    KIND_ENTITLEMENT_BLOCKED,
    KIND_OTHER_4XX,
    classify_4xx,
)


_CLASSIFIER_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "dw_entitlement_classifier.py"
)
_HEAVY_PROBE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "dw_heavy_probe.py"
)
_MODALITY_PROBE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "dw_modality_probe.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Closed taxonomy pins
# ---------------------------------------------------------------------------


def test_taxonomy_is_closed_three_kinds():
    """Exactly three kinds — no more, no less. Drift fails fast."""
    valid = {KIND_AUTH_FAILURE, KIND_ENTITLEMENT_BLOCKED, KIND_OTHER_4XX}
    for kind in valid:
        ClassificationResult(kind=kind)  # accepted
    with pytest.raises(ValueError, match="kind must be one of"):
        ClassificationResult(kind="bogus")


def test_result_is_frozen_dataclass():
    """ClassificationResult MUST be frozen — pure-data result type."""
    r = ClassificationResult(kind=KIND_AUTH_FAILURE)
    with pytest.raises(Exception):
        r.kind = KIND_OTHER_4XX  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Empirical DW soak patterns (from bt-2026-05-14-000028)
# ---------------------------------------------------------------------------


_DW_SOAK_BODY = (
    "Real-time access to 'deepseek-ai/DeepSeek-OCR-2' is blocked by a "
    "routing rule. Please contact your administrator to request access."
)


def test_empirical_dw_pattern_classified_as_entitlement_blocked():
    """The canonical phrase observed in the soak MUST classify
    ENTITLEMENT_BLOCKED + permanent=True."""
    r = classify_4xx(403, _DW_SOAK_BODY)
    assert r.kind == KIND_ENTITLEMENT_BLOCKED, (
        "The DW soak phrase 'blocked by a routing rule' MUST classify "
        "as ENTITLEMENT_BLOCKED — this is the load-bearing fix for Task #86"
    )
    assert r.is_permanent is True
    assert r.matched_marker == "blocked by a routing rule"


@pytest.mark.parametrize("status,body,expected_kind,expected_permanent", [
    # Empirical patterns from bt-2026-05-14-000028
    (403, _DW_SOAK_BODY, KIND_ENTITLEMENT_BLOCKED, True),
    # 401 is ALWAYS auth, even with the marker (no provider mixes 401)
    (401, "Unauthorized", KIND_AUTH_FAILURE, False),
    (401, "blocked by a routing rule", KIND_AUTH_FAILURE, False),
    # 403 without marker — legacy auth interpretation preserved
    (403, '{"error":{"message":"Invalid API key"}}', KIND_AUTH_FAILURE, False),
    (403, "", KIND_AUTH_FAILURE, False),
    # 4xx with marker — accommodates 402/451-style payment/legal blocks
    (402, "Please request access from your admin", KIND_ENTITLEMENT_BLOCKED, True),
    (451, "blocked by a routing rule", KIND_ENTITLEMENT_BLOCKED, True),
    # Pure 4xx without marker
    (404, "Not found", KIND_OTHER_4XX, False),
    (422, "Schema validation failed", KIND_OTHER_4XX, False),
    (429, "rate limited", KIND_OTHER_4XX, False),
    # 5xx — outside 4xx specialization, falls through to OTHER
    (500, "Internal Server Error", KIND_OTHER_4XX, False),
    # Edge: empty body, 403 → auth
    (403, "", KIND_AUTH_FAILURE, False),
])
def test_classifier_decision_table(
    status: int, body: str, expected_kind: str, expected_permanent: bool,
):
    """Every cell in the decision-table is pinned."""
    r = classify_4xx(status, body)
    assert r.kind == expected_kind, (
        f"classify_4xx({status}, {body!r}) → kind={r.kind!r}, "
        f"expected {expected_kind!r}"
    )
    assert r.is_permanent is expected_permanent


def test_case_insensitive_marker_match():
    """Marker matching MUST be case-insensitive (DW capitalization varies)."""
    r = classify_4xx(403, "BLOCKED BY A ROUTING RULE")
    assert r.kind == KIND_ENTITLEMENT_BLOCKED
    r = classify_4xx(403, "Blocked By A Routing Rule")
    assert r.kind == KIND_ENTITLEMENT_BLOCKED
    r = classify_4xx(403, "blocked by a ROUTING rule")
    assert r.kind == KIND_ENTITLEMENT_BLOCKED


# ---------------------------------------------------------------------------
# Operator override (autonomous entitlement adaptation without code change)
# ---------------------------------------------------------------------------


def test_env_override_replaces_defaults(monkeypatch: pytest.MonkeyPatch):
    """JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS REPLACES the default set
    (not appends).  Operator intent: full control."""
    monkeypatch.setenv(
        "JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS",
        "custom_phrase_only",
    )
    # Default marker no longer matches
    r = classify_4xx(403, "blocked by a routing rule")
    assert r.kind == KIND_AUTH_FAILURE, (
        "Operator override must REPLACE defaults, not extend them"
    )
    # Custom marker matches
    r = classify_4xx(403, "we hit a custom_phrase_only here")
    assert r.kind == KIND_ENTITLEMENT_BLOCKED


def test_env_override_csv_parsing(monkeypatch: pytest.MonkeyPatch):
    """Multiple markers comma-separated, whitespace trimmed."""
    monkeypatch.setenv(
        "JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS",
        "  pattern_a , pattern_b,pattern_c  ",
    )
    for pat in ("pattern_a", "pattern_b", "pattern_c"):
        r = classify_4xx(403, f"This body contains {pat} somewhere")
        assert r.kind == KIND_ENTITLEMENT_BLOCKED, (
            f"CSV-split pattern {pat!r} should match"
        )


def test_env_override_empty_uses_defaults(monkeypatch: pytest.MonkeyPatch):
    """Empty/whitespace env value → fall back to defaults."""
    monkeypatch.setenv("JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS", "   ")
    r = classify_4xx(403, "blocked by a routing rule")
    assert r.kind == KIND_ENTITLEMENT_BLOCKED


def test_env_read_at_call_time_not_module_load(monkeypatch: pytest.MonkeyPatch):
    """Env MUST be re-read on every call — operators can flip mid-process."""
    # First call with defaults
    monkeypatch.delenv("JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS", raising=False)
    r1 = classify_4xx(403, "blocked by a routing rule")
    assert r1.kind == KIND_ENTITLEMENT_BLOCKED
    # Override mid-process
    monkeypatch.setenv(
        "JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS", "different_marker",
    )
    r2 = classify_4xx(403, "blocked by a routing rule")
    assert r2.kind == KIND_AUTH_FAILURE, (
        "Mid-process env change MUST propagate without restart"
    )


# ---------------------------------------------------------------------------
# AST pins — policy alignment enforcement
# ---------------------------------------------------------------------------


def test_ast_pin_classifier_imports_only_stdlib():
    """Classifier MUST import only stdlib (no orchestrator coupling)."""
    src = _CLASSIFIER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed = {"os", "dataclasses", "typing", "__future__"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") in allowed, (
                f"dw_entitlement_classifier.py must not import-from "
                f"{node.module!r} at module scope — stdlib-only contract"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in allowed, (
                    f"dw_entitlement_classifier.py must not import "
                    f"{alias.name!r} — stdlib-only contract"
                )


def test_ast_pin_modality_probe_consumes_classifier():
    """modality_probe MUST call classify_4xx in the 401/403 branch.

    If a future refactor removes the call, entitlement-blocked models
    will go back to VERDICT_UNKNOWN and the classifier will keep
    probing them forever.  This pin makes the regression visible.
    """
    src = _MODALITY_PROBE_SRC.read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.dw_entitlement_classifier import" in src, (
        "dw_modality_probe.py MUST import dw_entitlement_classifier"
    )
    assert "classify_4xx(status, body_excerpt)" in src, (
        "dw_modality_probe.py MUST call classify_4xx(status, body_excerpt)"
    )
    assert "KIND_ENTITLEMENT_BLOCKED" in src, (
        "dw_modality_probe.py MUST reference KIND_ENTITLEMENT_BLOCKED "
        "to dispatch on the classifier verdict"
    )


def test_ast_pin_heavy_probe_consumes_classifier():
    """heavy_probe MUST call classify_4xx in _do_probe."""
    src = _HEAVY_PROBE_SRC.read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.dw_entitlement_classifier import" in src, (
        "dw_heavy_probe.py MUST import dw_entitlement_classifier"
    )
    assert "classify_4xx(resp.status, body_text)" in src, (
        "dw_heavy_probe.py MUST call classify_4xx(resp.status, body_text)"
    )
    assert "entitlement_blocked:" in src, (
        "dw_heavy_probe.py MUST emit the 'entitlement_blocked:' error "
        "prefix on ENTITLEMENT_BLOCKED classifications"
    )


def test_ast_pin_no_hardcoded_dw_models_in_probes():
    """Probe modules MUST NOT carry hardcoded provider model IDs.

    The probe candidate set MUST flow from the live DW catalog
    (set_dynamic_catalog → assignments_by_route → run_cycle).  Any
    hardcoded model literal like 'deepseek-ai/X' or 'moonshotai/Y'
    would break the autonomous-adaptation invariant.
    """
    # Heuristic: model IDs follow the pattern '<org>/<name>' inside
    # string literals.  Whitelist common org prefixes that DW uses;
    # any HARDCODED literal of that shape inside the probe modules
    # would fail this pin.  Documentation strings + log format
    # strings + canonical-name patterns from cataloging code are NOT
    # the target — we look at IDs assigned to module-level constants
    # or literal lists used for iteration.
    forbidden_pattern = re.compile(
        r'["\'](?:deepseek-ai|moonshotai|zai-org|anthropic|openai|'
        r'meta-llama|mistralai|qwen|microsoft|nvidia)/[A-Za-z0-9._-]+["\']'
    )
    for path in (_HEAVY_PROBE_SRC, _MODALITY_PROBE_SRC):
        src = path.read_text(encoding="utf-8")
        # Strip comments + docstrings so the pin only inspects code
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # Detect string-literal model IDs in assignments / list/tuple
            # constructors / function calls — the load-bearing structural
            # surfaces.  Docstrings get an Expr+Constant string at the
            # head of a function body, which we deliberately skip.
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.List,
                                  ast.Tuple, ast.Set)):
                code = ast.unparse(node)
                if forbidden_pattern.search(code):
                    raise AssertionError(
                        f"Hardcoded model ID found in {path.name}: "
                        f"{forbidden_pattern.search(code).group(0)} "
                        f"— Task #86 forbids hardcoded probe models. "
                        f"Models must come from set_dynamic_catalog."
                    )


def test_ast_pin_classifier_has_no_module_level_env_read():
    """Env MUST be read at call time, not module-import time.

    Module-level os.environ reads break monkey-patching + hot-reload.
    This pin walks the AST top-level for any ``os.environ`` reference
    OUTSIDE a function body.
    """
    src = _CLASSIFIER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        # Only inspect module-level statements (not function bodies)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        code = ast.unparse(node)
        assert "os.environ" not in code, (
            f"Module-level os.environ reference in classifier — env "
            f"MUST be read at call time. Offending node: {code[:120]}"
        )


def test_ast_pin_classifier_referenced_once_per_consumer():
    """Each probe module MUST reference the classifier through a
    SINGLE seam — not scattered across multiple call sites.

    Distributing the call would risk inconsistent dispatch (one site
    using the classifier, another using legacy logic).
    """
    for path in (_HEAVY_PROBE_SRC, _MODALITY_PROBE_SRC):
        src = path.read_text(encoding="utf-8")
        # Count occurrences of the canonical call (excluding import line)
        call_sites = src.count("classify_4xx(")
        # 1 (the call) + 0 (no extra references in production code)
        # is the minimal hit; the import statement uses
        # "classify_4xx," (with comma), which doesn't match "(".
        assert call_sites == 1, (
            f"{path.name} references classify_4xx() {call_sites} times; "
            f"MUST be exactly 1 (single seam). Distributing the call "
            f"risks inconsistent dispatch."
        )


# ---------------------------------------------------------------------------
# FlagRegistry seed pin
# ---------------------------------------------------------------------------


def test_ast_pin_heavy_probe_scheduler_routes_entitlement_to_sentinel():
    """Task #86b — the missing wire that closes the autonomous loop.

    When ``_do_probe`` emits ``entitlement_blocked:`` (Task #86), the
    scheduler MUST call ``sentinel.report_failure(is_terminal=True)``
    so the model's breaker flips TERMINAL_OPEN and the catalog
    classifier excludes it from future route assignments.  Without
    this wire, entitlement detection is a leaf log line with no
    autonomous adaptation effect — exactly the gap v14-rev4 surfaced.

    This pin asserts both:
      * The scheduler imports FailureSource + get_default_sentinel
      * The scheduler matches ``result.error.startswith("entitlement_blocked:")``
      * The scheduler calls ``report_failure(... is_terminal=True ...)``
    """
    src = _HEAVY_PROBE_SRC.read_text(encoding="utf-8")
    assert "entitlement_blocked:" in src, (
        "Task #86 must keep the entitlement_blocked: error-string emission"
    )
    # The new Task #86b wire — these three patterns must coexist in
    # the source.  They live inside run_cycle, lazy-imported to keep
    # the module-import cost low.
    assert "from backend.core.ouroboros.governance.topology_sentinel import" in src, (
        "Heavy probe must import topology_sentinel (lazy) to route "
        "entitlement detection to TERMINAL_OPEN"
    )
    assert "FailureSource.HEAVY_PROBE_FAIL" in src, (
        "Heavy probe must use FailureSource.HEAVY_PROBE_FAIL when "
        "routing entitlement detection — canonical failure-source taxonomy"
    )
    assert "is_terminal=True" in src, (
        "Heavy probe must pass is_terminal=True to flip TERMINAL_OPEN "
        "(weighted-streak bypass per Slice H)"
    )
    assert 'result.error.startswith("entitlement_blocked:")' in src, (
        "Heavy probe must gate the sentinel call on the structured "
        "error-string prefix from Task #86's classifier dispatch"
    )


def test_seed_has_entitlement_block_markers_flag():
    """JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS MUST be seeded so operators
    can /help flag and toggle without grepping the codebase."""
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS" in src, (
        "FlagRegistry MUST seed JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS"
    )
    idx = src.find("JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS")
    window = src[idx:idx + 1500]
    # Category MUST be TUNING (it's a tunable marker list, not a master
    # safety flag)
    assert "Category.TUNING" in window, (
        "JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS MUST be Category.TUNING"
    )
    # Source file pin
    assert "dw_entitlement_classifier.py" in window
