"""P5 Slice 1 — AdversarialReviewer primitive.

Per OUROBOROS_VENOM_PRD.md §9 Phase 5 P5 ("Adversarial reviewer
subagent"):

  > Iron Gate enforces hygiene rules. SemanticGuardian matches
  > patterns. Neither *thinks adversarially* about whether a plan is
  > correct.
  >
  > Solution: new subagent role — AdversarialReviewer. Activates
  > post-PLAN, pre-GENERATE. Given the plan, the model is prompted
  > as: "You are a senior engineer reviewing this plan for the most
  > likely way it will fail. Find at least 3 failure modes." Output
  > is structured findings injected into GENERATE prompt as
  > "Reviewer raised:" section.

This module is the **pure-data layer** for the reviewer. It ships:

  1. ``AdversarialFinding`` — frozen dataclass for one finding
     (severity / category / description / mitigation_hint /
     file_reference for grounding).
  2. ``AdversarialReview`` — frozen aggregate of findings + cost +
     model used + skip_reason (when the reviewer was bypassed).
  3. ``build_review_prompt(plan, target_files)`` — renders the
     "find at least 3 failure modes" prompt the LLM will see
     (Slice 2 wires the LLM call).
  4. ``parse_review_response(raw, target_files)`` — defensive JSON
     parser; tolerates prose-prefix / fenced blocks / partial
     responses; never raises.
  5. ``filter_findings(findings, target_files)`` — hallucination
     filter: drops findings whose ``file_reference`` is empty or
     points outside the plan's ``target_files`` set.
  6. ``format_findings_for_generate_prompt(findings)`` — converts a
     filtered finding list into the ASCII "Reviewer raised:" section
     Slice 3 will inject into GENERATE.

Slice 1 ships ZERO LLM calls. Slice 2 wraps everything in the
``AdversarialReviewerService`` that calls Claude side-stream + cost
budget enforcement + JSONL ledger. Slice 3 wires GENERATE injection.
Slice 4 adds REPL + IDE surfaces. Slice 5 graduates the master flag.

Authority invariants (PRD §12.2):
  * Pure data — no I/O, no subprocess, no env mutation.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Best-effort — every parser / filter operation is wrapped in
    ``try / except`` with structured fallback; never raises.
  * Bounded — prompt body capped at MAX_PLAN_PROMPT_CHARS; response
    parser caps the finding count at MAX_FINDINGS_PER_REVIEW so a
    runaway model can't pin downstream consumers.
  * Reviewer is **advisory only** — findings inform GENERATE but
    NEVER gate. Per PRD edge case: "Reviewer disagreement with PLAN
    — use as warning, not gate (PLAN still authoritative)." This is
    a structural property of this module: it produces text, never
    a decision.

Default-off behind ``JARVIS_ADVERSARIAL_REVIEWER_ENABLED`` until
Slice 5 graduation. Module is importable + callable; gating happens
at the Slice 2 service caller.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Per-prompt cap on the rendered plan body. Defends against feeding a
# 1 MB plan into the model side-stream (cost + latency).
MAX_PLAN_PROMPT_CHARS: int = 8 * 1024  # 8 KiB

# Per-review cap on parsed findings. PRD spec asks for ≥ 3 findings;
# 50 is a soft ceiling that bounds GENERATE-prompt injection size
# without filtering legitimate adversarial reviews.
MAX_FINDINGS_PER_REVIEW: int = 50

# Per-finding text caps — keep individual findings readable + prevent
# a model from emitting one giant blob that defeats the cap above.
MAX_DESCRIPTION_CHARS: int = 480
MAX_MITIGATION_CHARS: int = 240
MAX_CATEGORY_CHARS: int = 64
MAX_FILE_REFERENCE_CHARS: int = 256


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ADVERSARIAL_REVIEWER_ENABLED`` (default
    false until Slice 5 graduation).

    Slice 2's ``AdversarialReviewerService`` consults this; when off,
    the service short-circuits and returns an :class:`AdversarialReview`
    with ``skip_reason="master_off"``."""
    return os.environ.get(
        "JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Severity + category enums
# ---------------------------------------------------------------------------


class FindingSeverity(str, enum.Enum):
    """Per PRD spec — three buckets the model emits + we display."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# Suggested categories the prompt asks the model to use. Not enforced
# at parse time (model may emit anything); displayed verbatim.
SUGGESTED_CATEGORIES: Tuple[str, ...] = (
    "correctness",
    "edge_case",
    "race_condition",
    "performance",
    "security",
    "maintainability",
    "test_coverage",
    "rollback_safety",
)


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdversarialFinding:
    """One finding the reviewer raised against the plan.

    ``file_reference`` is the **grounding anchor** — the hallucination
    filter (``filter_findings``) drops findings whose reference is
    empty or points outside the plan's target_files set. Per PRD edge
    case: "findings must reference specific files / patterns;
    ungrounded findings filtered."
    """

    severity: FindingSeverity
    category: str
    description: str
    mitigation_hint: str
    file_reference: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "description": self.description,
            "mitigation_hint": self.mitigation_hint,
            "file_reference": self.file_reference,
        }


@dataclass(frozen=True)
class AdversarialReview:
    """Aggregate of one reviewer pass over a plan.

    ``skip_reason`` is non-empty when the reviewer was bypassed
    (master_off, safe_auto, budget_exhausted, etc.) — in that case
    ``findings`` is empty by construction. Slice 2's service produces
    these; Slice 1 ships the shape.
    """

    op_id: str
    findings: Tuple[AdversarialFinding, ...] = field(default_factory=tuple)
    raw_findings_count: int = 0    # before filter
    filtered_findings_count: int = 0  # after filter (== len(findings))
    cost_usd: float = 0.0
    model_used: str = ""
    skip_reason: str = ""
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def severity_histogram(self) -> Dict[str, int]:
        """Count findings by severity. Used by the §8 telemetry line
        (``[AdversarialReviewer] op=X raised N findings (severity
        high=A, med=B, low=C)``)."""
        out = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for f in self.findings:
            out[f.severity.value] = out.get(f.severity.value, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "findings": [f.to_dict() for f in self.findings],
            "raw_findings_count": self.raw_findings_count,
            "filtered_findings_count": self.filtered_findings_count,
            "cost_usd": self.cost_usd,
            "model_used": self.model_used,
            "skip_reason": self.skip_reason,
            "notes": list(self.notes),
            "severity_histogram": self.severity_histogram(),
        }

    @property
    def was_skipped(self) -> bool:
        return bool(self.skip_reason)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


_PROMPT_HEADER = (
    "You are a senior engineer reviewing the following plan for the "
    "most likely way it will fail. Find at least 3 failure modes."
)

_PROMPT_FORMAT_HEADER = (
    "Respond ONLY with a JSON object of this exact shape:\n"
    "  {\n"
    "    \"findings\": [\n"
    "      {\n"
    "        \"severity\": \"HIGH\" | \"MEDIUM\" | \"LOW\",\n"
    "        \"category\": \"correctness\" | \"edge_case\" | "
    "\"race_condition\" | \"performance\" | \"security\" | "
    "\"maintainability\" | \"test_coverage\" | \"rollback_safety\","
    "\n"
    "        \"description\": \"<= 480 chars; concrete failure mode\",\n"
    "        \"mitigation_hint\": \"<= 240 chars; how to prevent it\",\n"
    "        \"file_reference\": \"<one of the target files; required"
    " for grounding>\"\n"
    "      },\n"
    "      ...\n"
    "    ]\n"
    "  }\n"
    "Findings WITHOUT a file_reference matching one of the target "
    "files will be dropped. No prose outside the JSON."
)


def build_review_prompt(
    plan_text: str,
    target_files: Sequence[str] = (),
) -> str:
    """Render the reviewer prompt.

    The plan body is clipped at ``MAX_PLAN_PROMPT_CHARS`` so the
    side-stream cost stays bounded. The target file list is rendered
    explicitly so the model knows which references are valid (and so
    the hallucination filter can compare verbatim against them)."""
    safe_plan = (plan_text or "").strip()
    if len(safe_plan) > MAX_PLAN_PROMPT_CHARS:
        safe_plan = safe_plan[:MAX_PLAN_PROMPT_CHARS] + (
            "\n... <plan truncated to MAX_PLAN_PROMPT_CHARS>"
        )
    file_list = "\n".join(f"  - {p}" for p in target_files) or "  (none)"
    return "\n\n".join([
        _PROMPT_HEADER,
        f"Target files (the only valid file_reference values):\n{file_list}",
        f"Plan:\n{safe_plan}",
        _PROMPT_FORMAT_HEADER,
    ])


# ---------------------------------------------------------------------------
# Response parser (defensive)
# ---------------------------------------------------------------------------


# Loose JSON-object recovery: matches the OUTERMOST brace block. Used
# when the model wraps its JSON in fenced ```json ... ``` blocks or
# leading prose.
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_review_response(
    raw: str,
) -> Tuple[List[AdversarialFinding], List[str]]:
    """Defensive JSON parse. Returns (findings, notes).

    Tolerates:
      * Fenced code block wrappers (```json ... ```).
      * Leading / trailing prose around the JSON object.
      * Findings missing optional fields (mitigation_hint defaults
        to empty string).
      * Severity strings in any case (``"high"`` → HIGH).
      * The ``findings`` key being absent (returns empty list).

    Drops:
      * Severity values that don't match the enum (silently — note
        appended).
      * Findings with empty/missing required fields (description /
        category).
      * Per-finding cap at MAX_FINDINGS_PER_REVIEW so a runaway model
        can't blow out downstream consumers.

    NOTE: This parser does NOT do hallucination filtering. That's
    :func:`filter_findings` (separate so each layer is independently
    testable + bypassable in tests). Slice 2's service composes both."""
    notes: List[str] = []
    if not raw or not raw.strip():
        notes.append("empty_response")
        return [], notes

    # Strip fenced code blocks first.
    body = raw.strip()
    if body.startswith("```"):
        # Trim leading ```{lang}\n and trailing ```
        body = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body)

    # Direct JSON parse first.
    payload: Optional[Mapping[str, Any]] = None
    try:
        candidate = json.loads(body)
        if isinstance(candidate, dict):
            payload = candidate
    except (json.JSONDecodeError, TypeError):
        notes.append("direct_json_failed")

    # Fall back to outermost-brace recovery.
    if payload is None:
        match = _JSON_BLOCK_RE.search(body)
        if match:
            try:
                candidate = json.loads(match.group(0))
                if isinstance(candidate, dict):
                    payload = candidate
                    notes.append("json_recovered_from_brace_block")
            except (json.JSONDecodeError, TypeError):
                pass

    if payload is None:
        notes.append("unparseable")
        return [], notes

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        notes.append("findings_key_missing_or_not_list")
        return [], notes

    out: List[AdversarialFinding] = []
    for i, raw_f in enumerate(raw_findings):
        if i >= MAX_FINDINGS_PER_REVIEW:
            notes.append(
                f"findings_truncated_at_max_{MAX_FINDINGS_PER_REVIEW}",
            )
            break
        if not isinstance(raw_f, Mapping):
            notes.append(f"finding_{i}_not_object")
            continue
        finding = _coerce_finding(raw_f, notes, idx=i)
        if finding is not None:
            out.append(finding)
    return out, notes


def _coerce_finding(
    raw: Mapping[str, Any],
    notes: List[str],
    idx: int,
) -> Optional[AdversarialFinding]:
    """Build one :class:`AdversarialFinding` from a raw dict. Returns
    None when required fields are missing/blank."""
    sev_raw = raw.get("severity")
    try:
        sev = FindingSeverity(str(sev_raw).strip().upper())
    except (TypeError, ValueError):
        notes.append(f"finding_{idx}_bad_severity")
        return None

    description = str(raw.get("description") or "").strip()
    if not description:
        notes.append(f"finding_{idx}_empty_description")
        return None
    description = description[:MAX_DESCRIPTION_CHARS]

    category = str(raw.get("category") or "").strip()[:MAX_CATEGORY_CHARS]
    if not category:
        notes.append(f"finding_{idx}_empty_category")
        return None

    mitigation = str(raw.get("mitigation_hint") or "").strip()
    mitigation = mitigation[:MAX_MITIGATION_CHARS]

    file_reference = str(raw.get("file_reference") or "").strip()
    file_reference = file_reference[:MAX_FILE_REFERENCE_CHARS]

    return AdversarialFinding(
        severity=sev,
        category=category,
        description=description,
        mitigation_hint=mitigation,
        file_reference=file_reference,
    )


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------


def filter_findings(
    findings: Iterable[AdversarialFinding],
    target_files: Sequence[str] = (),
) -> Tuple[List[AdversarialFinding], List[str]]:
    """Drop findings whose ``file_reference`` is empty or points
    outside the plan's ``target_files`` set.

    When ``target_files`` is empty (e.g., a meta-plan with no
    file targets), the filter only drops findings with empty
    ``file_reference``.

    Returns (kept, drop_notes). ``drop_notes`` is a list of strings
    describing each dropped finding so Slice 4 can surface them via
    ``/adversarial why <op-id>``.
    """
    target_set = {p for p in target_files if p}
    kept: List[AdversarialFinding] = []
    drops: List[str] = []
    for f in findings:
        if not f.file_reference:
            drops.append(
                f"dropped:no_file_reference:{f.category}:{f.severity.value}",
            )
            continue
        if target_set and not _matches_target(f.file_reference, target_set):
            drops.append(
                f"dropped:ungrounded_reference:{f.file_reference}",
            )
            continue
        kept.append(f)
    return kept, drops


def _matches_target(reference: str, target_set: set) -> bool:
    """A finding's file_reference matches the target set if it equals
    one of the targets OR is a substring (operator may have specified
    ``"foo.py"`` while target is ``"backend/foo.py"`` — accept).

    NEVER accepts traversal references (``..`` segments) — those are
    a hallucination in any plan that didn't explicitly include them."""
    if ".." in reference:
        return False
    if reference in target_set:
        return True
    for t in target_set:
        if t.endswith(reference) or reference.endswith(t):
            return True
    return False


# ---------------------------------------------------------------------------
# GENERATE-prompt formatter
# ---------------------------------------------------------------------------


def format_findings_for_generate_prompt(
    findings: Sequence[AdversarialFinding],
    *,
    indent: str = "  ",
) -> str:
    """Render the "Reviewer raised:" section that Slice 3 will inject
    into the GENERATE prompt.

    Returns ``""`` when ``findings`` is empty so the caller can
    cleanly skip injection. ASCII-strict: passes through
    ``encode("ascii", errors="replace")`` round-trip."""
    if not findings:
        return ""
    lines = ["Reviewer raised:"]
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{indent}{i}. [{f.severity.value}] [{f.category}] "
            f"{f.description}"
        )
        if f.file_reference:
            lines.append(f"{indent}{indent}file: {f.file_reference}")
        if f.mitigation_hint:
            lines.append(f"{indent}{indent}mitigation: {f.mitigation_hint}")
    text = "\n".join(lines)
    return text.encode("ascii", errors="replace").decode("ascii")


__all__ = [
    "AdversarialFinding",
    "AdversarialReview",
    "FindingSeverity",
    "MAX_CATEGORY_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_FILE_REFERENCE_CHARS",
    "MAX_FINDINGS_PER_REVIEW",
    "MAX_MITIGATION_CHARS",
    "MAX_PLAN_PROMPT_CHARS",
    "SUGGESTED_CATEGORIES",
    "build_review_prompt",
    "filter_findings",
    "format_findings_for_generate_prompt",
    "is_enabled",
    "parse_review_response",
]
