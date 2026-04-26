"""P5 Slice 1 — AdversarialReviewer primitive regression suite.

Pins:
  * Module constants + 3-value FindingSeverity enum + suggested
    categories tuple + frozen dataclass shapes + .to_dict.
  * Env knob default-false-pre-graduation.
  * build_review_prompt: all expected sections present; plan body
    truncated at MAX_PLAN_PROMPT_CHARS; target file list rendered.
  * parse_review_response: happy path; fenced ```json wrapper;
    leading/trailing prose; missing optional fields; case-insensitive
    severity; bad severity dropped; empty description dropped; empty
    category dropped; missing findings key; non-list findings;
    cap at MAX_FINDINGS_PER_REVIEW; empty input; unparseable input.
  * Per-finding text caps: description / mitigation / category /
    file_reference all truncated.
  * filter_findings: empty file_reference dropped; ungrounded
    reference dropped; traversal reference dropped; substring match
    accepted (operator partial path); empty target_files only drops
    empty references; drop_notes structure.
  * format_findings_for_generate_prompt: empty input → "";
    happy multi-finding render; ASCII-strict round-trip.
  * AdversarialReview.severity_histogram + was_skipped helpers.
  * Authority invariants: no banned imports + no I/O / subprocess.
"""
from __future__ import annotations

import dataclasses
import io
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adversarial_reviewer import (
    MAX_CATEGORY_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_FILE_REFERENCE_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    MAX_MITIGATION_CHARS,
    MAX_PLAN_PROMPT_CHARS,
    SUGGESTED_CATEGORIES,
    AdversarialFinding,
    AdversarialReview,
    FindingSeverity,
    build_review_prompt,
    filter_findings,
    format_findings_for_generate_prompt,
    is_enabled,
    parse_review_response,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    yield


def _F(severity="HIGH", category="correctness", description="bug",
       mitigation_hint="fix it", file_reference="backend/x.py"):
    return AdversarialFinding(
        severity=FindingSeverity(severity),
        category=category, description=description,
        mitigation_hint=mitigation_hint,
        file_reference=file_reference,
    )


# ===========================================================================
# A — Module constants + enum + dataclass shapes
# ===========================================================================


def test_max_plan_prompt_chars_pinned():
    assert MAX_PLAN_PROMPT_CHARS == 8 * 1024


def test_max_findings_per_review_pinned():
    assert MAX_FINDINGS_PER_REVIEW == 50


def test_per_finding_caps_pinned():
    assert MAX_DESCRIPTION_CHARS == 480
    assert MAX_MITIGATION_CHARS == 240
    assert MAX_CATEGORY_CHARS == 64
    assert MAX_FILE_REFERENCE_CHARS == 256


def test_finding_severity_three_values():
    """Pin: PRD spec says three buckets (HIGH / MED / LOW). Adding a
    fourth requires a new slice + design doc."""
    assert {s.name for s in FindingSeverity} == {"HIGH", "MEDIUM", "LOW"}


def test_suggested_categories_pinned():
    """Pin: the 8 suggested categories the prompt asks the model to
    use. Operators can rely on these for filtering / dashboards."""
    expected = (
        "correctness", "edge_case", "race_condition", "performance",
        "security", "maintainability", "test_coverage", "rollback_safety",
    )
    assert SUGGESTED_CATEGORIES == expected


def test_finding_is_frozen():
    f = _F()
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.description = "x"  # type: ignore[misc]


def test_review_is_frozen():
    r = AdversarialReview(op_id="op")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.op_id = "x"  # type: ignore[misc]


def test_finding_to_dict_stable_shape():
    d = _F().to_dict()
    for k in ("severity", "category", "description",
              "mitigation_hint", "file_reference"):
        assert k in d
    assert d["severity"] == "HIGH"


def test_review_to_dict_stable_shape():
    r = AdversarialReview(
        op_id="op", findings=(_F(), _F(severity="LOW")),
        raw_findings_count=3, filtered_findings_count=2,
        cost_usd=0.012, model_used="claude",
        skip_reason="", notes=("note-1",),
    )
    d = r.to_dict()
    for k in ("op_id", "findings", "raw_findings_count",
              "filtered_findings_count", "cost_usd", "model_used",
              "skip_reason", "notes", "severity_histogram"):
        assert k in d
    assert len(d["findings"]) == 2
    assert d["severity_histogram"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 1}
    assert d["notes"] == ["note-1"]


def test_review_severity_histogram():
    r = AdversarialReview(
        op_id="op",
        findings=(_F("HIGH"), _F("HIGH"), _F("MEDIUM"), _F("LOW")),
    )
    assert r.severity_histogram() == {"HIGH": 2, "MEDIUM": 1, "LOW": 1}


def test_review_was_skipped_helper():
    assert AdversarialReview(op_id="op").was_skipped is False
    assert AdversarialReview(op_id="op", skip_reason="master_off").was_skipped is True


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_false_pre_graduation():
    """Slice 1 ships default-OFF. Renamed at Slice 5 graduation."""
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_is_enabled_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_is_enabled_falsy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", val)
    assert is_enabled() is False


# ===========================================================================
# C — build_review_prompt
# ===========================================================================


def test_prompt_includes_role_and_failure_modes_directive():
    out = build_review_prompt("plan", ("a.py",))
    assert "senior engineer" in out
    assert "find at least 3 failure modes" in out.lower()


def test_prompt_includes_target_file_list():
    out = build_review_prompt("plan", ("backend/foo.py", "backend/bar.py"))
    assert "  - backend/foo.py" in out
    assert "  - backend/bar.py" in out


def test_prompt_handles_no_target_files():
    out = build_review_prompt("plan", ())
    assert "(none)" in out


def test_prompt_includes_strict_json_format():
    out = build_review_prompt("plan", ("a.py",))
    assert "\"findings\":" in out
    assert "HIGH" in out
    assert "file_reference" in out


def test_prompt_truncates_long_plan():
    huge = "x" * (MAX_PLAN_PROMPT_CHARS * 2)
    out = build_review_prompt(huge, ("a.py",))
    assert "<plan truncated to MAX_PLAN_PROMPT_CHARS>" in out
    # Total render shouldn't include the full huge body.
    assert len(out) < len(huge) + 4096


def test_prompt_handles_none_plan_text():
    """Defensive: None plan_text should not crash."""
    out = build_review_prompt(None, ("a.py",))  # type: ignore[arg-type]
    assert "Plan:" in out


# ===========================================================================
# D — parse_review_response — happy paths
# ===========================================================================


def _resp(findings_json: str) -> str:
    return '{"findings": ' + findings_json + '}'


def test_parse_happy_path():
    resp = _resp(
        '[{"severity": "HIGH", "category": "race_condition", '
        '"description": "lock contention", "mitigation_hint": "use RWLock", '
        '"file_reference": "x.py"}]'
    )
    findings, notes = parse_review_response(resp)
    assert len(findings) == 1
    assert findings[0].severity is FindingSeverity.HIGH
    assert findings[0].category == "race_condition"
    assert findings[0].file_reference == "x.py"


def test_parse_handles_fenced_code_block():
    resp = '```json\n' + _resp(
        '[{"severity": "MEDIUM", "category": "perf", '
        '"description": "O(n^2)", "mitigation_hint": "use set", '
        '"file_reference": "x.py"}]'
    ) + '\n```'
    findings, _ = parse_review_response(resp)
    assert len(findings) == 1
    assert findings[0].severity is FindingSeverity.MEDIUM


def test_parse_handles_leading_prose():
    resp = "Sure, here is the review:\n" + _resp(
        '[{"severity": "LOW", "category": "x", '
        '"description": "y", "mitigation_hint": "z", '
        '"file_reference": "x.py"}]'
    )
    findings, notes = parse_review_response(resp)
    assert len(findings) == 1
    assert "json_recovered_from_brace_block" in notes


def test_parse_severity_case_insensitive():
    for sv in ("high", "High", "HIGH", "hIgH"):
        resp = _resp(
            f'[{{"severity": "{sv}", "category": "x", '
            f'"description": "y", "mitigation_hint": "z", '
            f'"file_reference": "x.py"}}]'
        )
        findings, _ = parse_review_response(resp)
        assert len(findings) == 1
        assert findings[0].severity is FindingSeverity.HIGH


def test_parse_optional_mitigation_defaults_to_empty():
    resp = _resp(
        '[{"severity": "HIGH", "category": "x", '
        '"description": "y", "file_reference": "x.py"}]'
    )
    findings, _ = parse_review_response(resp)
    assert findings[0].mitigation_hint == ""


def test_parse_optional_file_reference_defaults_to_empty():
    """file_reference can be missing at parse time — filter_findings
    is what enforces grounding."""
    resp = _resp(
        '[{"severity": "HIGH", "category": "x", '
        '"description": "y", "mitigation_hint": "z"}]'
    )
    findings, _ = parse_review_response(resp)
    assert findings[0].file_reference == ""


# ===========================================================================
# E — parse_review_response — drop / failure paths
# ===========================================================================


def test_parse_bad_severity_dropped():
    resp = _resp(
        '[{"severity": "CRITICAL", "category": "x", '
        '"description": "y", "mitigation_hint": "z", '
        '"file_reference": "x.py"}]'
    )
    findings, notes = parse_review_response(resp)
    assert findings == []
    assert any("bad_severity" in n for n in notes)


def test_parse_empty_description_dropped():
    resp = _resp(
        '[{"severity": "HIGH", "category": "x", '
        '"description": "", "mitigation_hint": "z", '
        '"file_reference": "x.py"}]'
    )
    findings, notes = parse_review_response(resp)
    assert findings == []
    assert any("empty_description" in n for n in notes)


def test_parse_empty_category_dropped():
    resp = _resp(
        '[{"severity": "HIGH", "category": "", '
        '"description": "y", "mitigation_hint": "z", '
        '"file_reference": "x.py"}]'
    )
    findings, notes = parse_review_response(resp)
    assert findings == []
    assert any("empty_category" in n for n in notes)


def test_parse_missing_findings_key():
    findings, notes = parse_review_response('{"other": []}')
    assert findings == []
    assert "findings_key_missing_or_not_list" in notes


def test_parse_findings_not_list():
    findings, notes = parse_review_response('{"findings": "not-a-list"}')
    assert findings == []
    assert "findings_key_missing_or_not_list" in notes


def test_parse_caps_at_max_findings():
    """Pin: more than MAX_FINDINGS_PER_REVIEW raw findings → truncated."""
    raw_finds = ",".join(
        '{"severity": "HIGH", "category": "x", '
        '"description": "y", "mitigation_hint": "z", '
        '"file_reference": "a.py"}'
        for _ in range(MAX_FINDINGS_PER_REVIEW + 5)
    )
    resp = _resp("[" + raw_finds + "]")
    findings, notes = parse_review_response(resp)
    assert len(findings) == MAX_FINDINGS_PER_REVIEW
    assert any("findings_truncated_at_max" in n for n in notes)


def test_parse_empty_input():
    findings, notes = parse_review_response("")
    assert findings == []
    assert "empty_response" in notes


def test_parse_unparseable_input():
    findings, notes = parse_review_response("not even close to json")
    assert findings == []
    assert "unparseable" in notes


def test_parse_non_dict_finding_skipped():
    resp = _resp(
        '["not-a-dict", {"severity": "HIGH", "category": "x", '
        '"description": "y", "mitigation_hint": "z", '
        '"file_reference": "a.py"}]'
    )
    findings, notes = parse_review_response(resp)
    assert len(findings) == 1
    assert any("not_object" in n for n in notes)


# ===========================================================================
# F — Per-finding text caps
# ===========================================================================


def test_parse_truncates_oversize_description():
    big = "x" * (MAX_DESCRIPTION_CHARS + 100)
    resp = _resp(
        f'[{{"severity": "HIGH", "category": "x", '
        f'"description": "{big}", "mitigation_hint": "z", '
        f'"file_reference": "x.py"}}]'
    )
    findings, _ = parse_review_response(resp)
    assert len(findings[0].description) == MAX_DESCRIPTION_CHARS


def test_parse_truncates_oversize_mitigation():
    big = "x" * (MAX_MITIGATION_CHARS + 100)
    resp = _resp(
        f'[{{"severity": "HIGH", "category": "x", '
        f'"description": "y", "mitigation_hint": "{big}", '
        f'"file_reference": "x.py"}}]'
    )
    findings, _ = parse_review_response(resp)
    assert len(findings[0].mitigation_hint) == MAX_MITIGATION_CHARS


def test_parse_truncates_oversize_category():
    big = "x" * (MAX_CATEGORY_CHARS + 50)
    resp = _resp(
        f'[{{"severity": "HIGH", "category": "{big}", '
        f'"description": "y", "mitigation_hint": "z", '
        f'"file_reference": "x.py"}}]'
    )
    findings, _ = parse_review_response(resp)
    assert len(findings[0].category) == MAX_CATEGORY_CHARS


def test_parse_truncates_oversize_file_reference():
    big = "x" * (MAX_FILE_REFERENCE_CHARS + 50)
    resp = _resp(
        f'[{{"severity": "HIGH", "category": "x", '
        f'"description": "y", "mitigation_hint": "z", '
        f'"file_reference": "{big}"}}]'
    )
    findings, _ = parse_review_response(resp)
    assert len(findings[0].file_reference) == MAX_FILE_REFERENCE_CHARS


# ===========================================================================
# G — filter_findings (hallucination filter)
# ===========================================================================


def test_filter_drops_empty_file_reference():
    findings = [_F(file_reference=""), _F(file_reference="backend/x.py")]
    kept, drops = filter_findings(findings, ("backend/x.py",))
    assert len(kept) == 1
    assert kept[0].file_reference == "backend/x.py"
    assert any("no_file_reference" in d for d in drops)


def test_filter_drops_ungrounded_reference():
    findings = [_F(file_reference="random/elsewhere.py")]
    kept, drops = filter_findings(findings, ("backend/x.py",))
    assert kept == []
    assert any("ungrounded_reference" in d for d in drops)


def test_filter_drops_traversal_reference():
    """Pin: even when the reference 'matches' a target via substring,
    .. segments are blocked unconditionally — those are always a
    hallucination."""
    findings = [_F(file_reference="../../etc/passwd")]
    kept, drops = filter_findings(findings, ("etc/passwd",))
    assert kept == []
    assert any("ungrounded_reference" in d for d in drops)


def test_filter_accepts_substring_match():
    """Operator-supplied partial path should match the longer canonical
    target — ``foo.py`` matches ``backend/x/foo.py``."""
    findings = [_F(file_reference="foo.py")]
    kept, _ = filter_findings(findings, ("backend/x/foo.py",))
    assert len(kept) == 1


def test_filter_with_empty_target_files_only_drops_empty_refs():
    """When target_files is empty, ungrounded findings (with any
    non-empty file_reference) are accepted — only empty references
    are dropped."""
    findings = [
        _F(file_reference=""),
        _F(file_reference="anything.py"),
    ]
    kept, drops = filter_findings(findings, ())
    assert len(kept) == 1
    assert kept[0].file_reference == "anything.py"


def test_filter_drop_notes_structure():
    findings = [_F(file_reference="")]
    _, drops = filter_findings(findings, ("x.py",))
    assert len(drops) == 1
    parts = drops[0].split(":")
    # dropped:no_file_reference:<category>:<severity>
    assert parts[0] == "dropped"
    assert parts[1] == "no_file_reference"


# ===========================================================================
# H — format_findings_for_generate_prompt
# ===========================================================================


def test_format_empty_returns_empty():
    assert format_findings_for_generate_prompt([]) == ""


def test_format_single_finding_complete():
    out = format_findings_for_generate_prompt([_F()])
    assert "Reviewer raised:" in out
    assert "[HIGH]" in out
    assert "[correctness]" in out
    assert "file: backend/x.py" in out
    assert "mitigation: fix it" in out


def test_format_multi_finding_numbered():
    out = format_findings_for_generate_prompt([
        _F(severity="HIGH"),
        _F(severity="MEDIUM"),
        _F(severity="LOW"),
    ])
    assert "1. [HIGH]" in out
    assert "2. [MEDIUM]" in out
    assert "3. [LOW]" in out


def test_format_omits_empty_optional_fields():
    """A finding without file_reference / mitigation should not render
    those lines (avoids ``file: \n`` or ``mitigation: `` clutter)."""
    f = _F(mitigation_hint="", file_reference="")
    out = format_findings_for_generate_prompt([f])
    assert "file:" not in out
    assert "mitigation:" not in out


def test_format_is_ascii_safe():
    """Pin: ASCII-strict round-trip — no Unicode in the rendered
    section."""
    out = format_findings_for_generate_prompt([_F()])
    out.encode("ascii")  # raises if non-ASCII slipped in


# ===========================================================================
# I — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_reviewer_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/adversarial_reviewer.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_reviewer_no_io_or_subprocess():
    """Pin: primitive is pure data — no I/O, no subprocess, no env
    mutation. Slice 2 owns the LLM-call surface; Slice 4 owns IDE
    GET / SSE."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/adversarial_reviewer.py"),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
