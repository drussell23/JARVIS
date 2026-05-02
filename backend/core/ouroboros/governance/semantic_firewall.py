"""
Semantic Firewall — Manifesto §5 Tier -1 boundary (Phase B).

Pure, side-effect-free sanitization + boundary-validation for GENERAL
subagent dispatch. Every string that crosses into a GENERAL subagent
passes through ``sanitize_for_firewall()``; every invocation's 5
mandatory boundary conditions pass through
``validate_boundary_conditions()``. Both functions return structured
results — they never raise; the caller raises
``SubagentSemanticFirewallRejection`` from the aggregated errors.

§5 Semantic Firewall — what this module defends against:

    1. **Prompt injection** in goal / invocation_reason / any other
       free-text field. Patterns like "ignore previous instructions",
       role-override attempts ("<|system|>"), or XML-injection of
       new instructions get rejected BEFORE the GENERAL subagent sees
       them. The pattern set is conservative — false positives are
       better than letting an attack through.

    2. **Credential shape leakage** in any input field. Reuses the
       same secret-shape redaction already wired into sanitize_for_log
       (sk-*, ghp_*, AKIA*, xox[bp]-*, PEM blocks). A `goal` that
       contains what looks like an API key is redacted before logging
       AND rejected as a signal that the invocation is malformed.

    3. **Scope escape** via operation_scope. Concrete file paths/globs
       only — "the whole repo", "**", "/etc/*", or empty are rejected.

    4. **Tool escalation** via allowed_tools. Caller must pick from
       an explicit whitelist. Mutating tools (edit/write/delete/bash)
       require the caller's parent op to be at or above NOTIFY_APPLY.

    5. **Risk-tier floor violation**. GENERAL dispatch is refused from
       a SAFE_AUTO parent op — too broad a blast radius for auto-applied
       changes. The caller must have explicit human-in-the-loop risk
       tier.

Design notes:

    * Pure functions. No I/O, no network, no filesystem. Fully testable
      without fixtures.
    * Conservative. When in doubt, reject. Operators can widen the
      whitelists via env vars; the defaults lean safe.
    * Observability. Every rejection returns a specific reason string
      suitable for direct inclusion in a POSTMORTEM or error log.
    * Python 3.9 compatible. No match statements, no PEP 604 unions
      at runtime (annotations only).

Manifesto alignment:
    §1 — Boundary Principle: trust is asserted at the boundary, not
         assumed by module. The firewall runs on every call site, not
         just "untrusted" ones.
    §5 — Tier -1: sanitization happens BEFORE the data enters the
         decision plane. GENERAL never sees raw, unvalidated input.
    §8 — Absolute Observability: rejection reasons are specific and
         auditable.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration — env-tunable whitelists (defaults conservative)
# ============================================================================


# Read-only tool whitelist. Default allowed_tools for any GENERAL
# invocation that doesn't explicitly override. Operators can widen
# via env var (comma-separated), but the default lane stays tight.
_DEFAULT_READONLY_TOOLS: FrozenSet[str] = frozenset({
    "read_file",
    "search_code",
    "list_symbols",
    "get_callers",
    "glob_files",
    "list_dir",
    "git_log",
    "git_blame",
    "web_search",
    "web_fetch",
})

# Mutating tools — must be explicitly opted into by the caller via
# allowed_tools, AND the caller's parent op must be at ≥ NOTIFY_APPLY.
# SAFE_AUTO parents cannot grant mutating tool access to GENERAL.
_MUTATING_TOOLS: FrozenSet[str] = frozenset({
    "edit_file",
    "write_file",
    "delete_file",
    "bash",
    "apply_patch",
})

# Every tool the firewall knows about. Anything outside this set is
# rejected — future tools need an explicit classification.
_KNOWN_TOOLS: FrozenSet[str] = _DEFAULT_READONLY_TOOLS | _MUTATING_TOOLS | frozenset({
    "run_tests",
    "ask_human",
})

# Risk-tier floor. GENERAL dispatch requires parent_op_risk_tier at
# or above this. SAFE_AUTO is explicitly BELOW the floor.
_RISK_TIER_ORDER: Dict[str, int] = {
    "SAFE_AUTO": 0,
    "NOTIFY_APPLY": 1,
    "APPROVAL_REQUIRED": 2,
    "BLOCKED": 3,
}
_RISK_TIER_FLOOR_NAME = os.environ.get(
    "JARVIS_GENERAL_MIN_RISK_TIER", "NOTIFY_APPLY",
)


# ============================================================================
# Prompt-injection pattern set
# ============================================================================
#
# Conservative by design. False positives cause dispatch rejection, which
# the caller can diagnose and fix. False negatives let attack payloads
# reach the subagent — much worse. Patterns stay as compiled regexes so
# scanning is fast (GENERAL invocation happens synchronously inside
# dispatch_general() hot path).

# §24.8.5 — Credential / secret shapes. Extracted as a named module
# constant so the verification subsystem (Priority A — mandatory claim
# density) can reuse the same regex set for `no_new_credential_shapes`
# without duplicating the patterns. The single source of truth lives
# here; both the firewall and the oracle import it.
_CREDENTIAL_SHAPE_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}", re.UNICODE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b", re.UNICODE),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b", re.UNICODE),
    re.compile(r"\bxox[bp]-[A-Za-z0-9\-]{10,}\b", re.UNICODE),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----", re.UNICODE),
)

_INJECTION_PATTERNS: Tuple[re.Pattern, ...] = (
    # Role-override attempts
    re.compile(r"(?i)\b(ignore|disregard|forget)\s+(previous|prior|all|above)\s+(instruction|prompt|message|directive)", re.UNICODE),
    re.compile(r"(?i)\b(you\s+are\s+now|new\s+role|act\s+as(?:\s+if)?)\b", re.UNICODE),
    re.compile(r"<\|(system|user|assistant|role)\|?>", re.UNICODE),
    re.compile(r"(?i)\[\s*system\s*\]", re.UNICODE),
    # XML/HTML injection of new instructions
    re.compile(r"(?i)<\s*system\s*>.*?</\s*system\s*>", re.UNICODE | re.DOTALL),
    re.compile(r"(?i)<\s*(critical|mandatory)_(system|admin|override)_directive\b", re.UNICODE),
    # §24.8.5 — Gate-bypass instruction sanitizer (Slice AV.3)
    # Detects attempts to instruct the model to bypass validation
    # gates, skip safety checks, or ignore governance constraints.
    # These patterns are conservative — false positives cause
    # dispatch rejection, which is safer than letting a gate-bypass
    # instruction reach the subagent.
    re.compile(r"(?i)\b(skip|bypass|disable|circumvent|ignore|override)\s+(the\s+)?(validation|gate|safety|security|governance|guard|firewall|iron.?gate|risk.?tier|approval|review)", re.UNICODE),
    re.compile(r"(?i)\b(do\s+not|don'?t|never)\s+(validate|check|verify|gate|review|audit|scan|sanitize|enforce)\b", re.UNICODE),
    re.compile(r"(?i)\bset\s+(risk.?tier|approval|gate|validation)\s+(to\s+)?(safe.?auto|none|disabled|off|skip)\b", re.UNICODE),
    re.compile(r"(?i)\b(force|always)\s+(approve|accept|pass|allow|merge|commit|apply)\b", re.UNICODE),
    re.compile(r"(?i)\bwithout\s+(human|manual|operator|approval|review|validation|verification|gate|check)\b", re.UNICODE),
    # Credential / secret shapes — sourced from the canonical
    # _CREDENTIAL_SHAPE_PATTERNS tuple above so both firewall and
    # verification share one definition (no duplication).
) + _CREDENTIAL_SHAPE_PATTERNS


# ============================================================================
# Sanitization result (immutable, observable)
# ============================================================================


@dataclass(frozen=True)
class FirewallResult:
    """Structured outcome of firewall scanning.

    ``rejected`` + ``reasons`` is the authoritative pass/fail signal.
    ``sanitized`` is the value with secret-shape redactions applied —
    safe to log at INFO. Both are always populated so the caller can
    log even rejected invocations for audit.
    """
    rejected: bool
    reasons: Tuple[str, ...] = ()
    sanitized: str = ""


# ============================================================================
# Public API
# ============================================================================


def sanitize_for_firewall(
    value: Any, *, max_chars: int = 4096, field_name: str = "value",
) -> FirewallResult:
    """Pass ``value`` through the firewall.

    Returns a FirewallResult. ``rejected=True`` means the value contains
    a prompt-injection signature OR a credential shape OR is malformed
    (wrong type, too large). The caller aggregates reasons across
    multiple fields and raises
    ``SubagentSemanticFirewallRejection(reasons)`` if any field rejected.

    Parameters
    ----------
    value:
        The input to scan. Non-string values are coerced via str().
    max_chars:
        Hard length cap — larger inputs are rejected outright. Default
        4096 is generous for a goal/reason field; operators can tighten
        via env.
    field_name:
        For diagnostic reasons only — included in the rejection reason
        so multi-field scans produce readable output.
    """
    # Coerce to string (non-string inputs are suspicious but not fatal —
    # convert then scan).
    try:
        s = str(value) if value is not None else ""
    except Exception:
        return FirewallResult(
            rejected=True,
            reasons=(f"{field_name}: value not coercible to string",),
            sanitized="",
        )

    # Length cap.
    if len(s) > max_chars:
        return FirewallResult(
            rejected=True,
            reasons=(
                f"{field_name}: length {len(s)} exceeds max {max_chars}",
            ),
            sanitized=s[:max_chars] + "…[truncated]",
        )

    # Pattern scan.
    hit_patterns: List[str] = []
    for pat in _INJECTION_PATTERNS:
        if pat.search(s):
            hit_patterns.append(pat.pattern[:60])

    # Sanitize via shared secret-shape redactor when available, THEN
    # always apply our own credential-shape redactions on top. This is
    # belt-and-suspenders: sanitize_for_log's secret patterns may not
    # cover every shape the firewall knows about, and the firewall's
    # own pattern set is the authoritative credential-shape redactor
    # for GENERAL output.
    sanitized = s
    try:
        from backend.core.secure_logging import sanitize_for_log
        sanitized = sanitize_for_log(s)
    except Exception:
        pass  # fall through — the firewall's own redaction below still runs

    # The last 5 patterns in _INJECTION_PATTERNS are credential shapes
    # (sk-*, AKIA*, ghp_*, xox[bp]-*, PEM). Apply unconditionally so
    # rejected-input sanitized output is never a credential leak.
    for pat in _INJECTION_PATTERNS[-5:]:
        sanitized = pat.sub("[REDACTED]", sanitized)

    if hit_patterns:
        return FirewallResult(
            rejected=True,
            reasons=tuple(
                f"{field_name}: injection pattern hit: {p}"
                for p in hit_patterns
            ),
            sanitized=sanitized,
        )

    return FirewallResult(
        rejected=False,
        reasons=(),
        sanitized=sanitized,
    )


def validate_boundary_conditions(
    invocation: Mapping[str, Any],
) -> Tuple[bool, Tuple[str, ...]]:
    """Enforce the 5 mandatory boundary conditions of a GENERAL invocation.

    Returns ``(valid, reasons)``. ``valid=False`` with non-empty reasons
    means the invocation must be rejected. The caller typically raises
    ``SubagentSemanticFirewallRejection(reasons)``.

    The 5 conditions (Manifesto §5):
      1. operation_scope — concrete (non-empty, not "**", not "/")
      2. max_mutations — non-negative int; if > 0, parent must have
         ≥ NOTIFY_APPLY risk tier AND allowed_tools must include a
         mutating tool (otherwise max_mutations > 0 is meaningless).
      3. allowed_tools — non-empty, every entry in _KNOWN_TOOLS
      4. invocation_reason — non-empty, ≤ 200 chars after trim
      5. parent_op_risk_tier — at or above _RISK_TIER_FLOOR_NAME
    """
    reasons: List[str] = []

    # ---- 1. operation_scope ---------------------------------------------
    scope = invocation.get("operation_scope", None)
    if scope is None:
        reasons.append(
            "operation_scope missing — GENERAL requires a concrete "
            "path/glob scope (never 'whole repo')"
        )
    else:
        scope_paths: List[str] = []
        if isinstance(scope, (list, tuple)):
            scope_paths = [str(p) for p in scope]
        elif isinstance(scope, str):
            scope_paths = [scope]
        else:
            reasons.append(
                "operation_scope must be str or Tuple[str, ...]"
            )
        scope_clean = [p.strip() for p in scope_paths if p and p.strip()]
        if not scope_clean:
            reasons.append(
                "operation_scope is empty — concrete paths/globs required"
            )
        for p in scope_clean:
            if p in ("**", "*", "/", ".", "./", "*/"):
                reasons.append(
                    f"operation_scope {p!r} is too broad — "
                    "refuse 'whole repo' scopes"
                )
            if p.startswith("/") and not p.startswith("/tmp") and p != "/":
                # Absolute paths outside tmp are suspicious — likely
                # escape attempts or accidental system-path references.
                reasons.append(
                    f"operation_scope {p!r} is an absolute path outside "
                    "/tmp — GENERAL scopes must be repo-relative"
                )

    # ---- 2. max_mutations -----------------------------------------------
    max_mut_raw = invocation.get("max_mutations", None)
    if max_mut_raw is None:
        reasons.append(
            "max_mutations missing — set to 0 for read-only, N > 0 "
            "for bounded mutating"
        )
        max_mut: int = 0  # for downstream checks
    else:
        try:
            max_mut = int(max_mut_raw)
        except (TypeError, ValueError):
            reasons.append(
                f"max_mutations must be int, got {type(max_mut_raw).__name__}"
            )
            max_mut = 0
        if max_mut < 0:
            reasons.append(f"max_mutations={max_mut} must be ≥ 0")
        if max_mut > int(os.environ.get("JARVIS_GENERAL_MAX_MUTATIONS_CEIL", "10")):
            reasons.append(
                f"max_mutations={max_mut} exceeds the operator-configured "
                "ceiling (env JARVIS_GENERAL_MAX_MUTATIONS_CEIL)"
            )

    # ---- 3. allowed_tools -----------------------------------------------
    tools_raw = invocation.get("allowed_tools", None)
    if tools_raw is None:
        reasons.append(
            "allowed_tools missing — must be an explicit subset (default "
            "is read-only-only; caller must opt into mutating tools)"
        )
        tools_set: FrozenSet[str] = frozenset()
    else:
        if not isinstance(tools_raw, (list, tuple, frozenset, set)):
            reasons.append(
                "allowed_tools must be a list/tuple/set of tool names"
            )
            tools_set = frozenset()
        else:
            tools_set = frozenset(str(t) for t in tools_raw)
        if not tools_set:
            reasons.append(
                "allowed_tools is empty — GENERAL with no tools is useless; "
                "the caller must grant at least read-only access"
            )
        unknown = tools_set - _KNOWN_TOOLS
        if unknown:
            reasons.append(
                f"allowed_tools contains unknown tool(s) "
                f"{sorted(unknown)!r} — only classified tools accepted"
            )

    # ---- 4. invocation_reason -------------------------------------------
    reason_raw = invocation.get("invocation_reason", None)
    if reason_raw is None:
        reasons.append(
            "invocation_reason missing — caller must supply a one-sentence "
            "rationale for the dispatch (≤ 200 chars)"
        )
    else:
        reason_str = str(reason_raw).strip()
        if not reason_str:
            reasons.append("invocation_reason must be non-empty")
        if len(reason_str) > 200:
            reasons.append(
                f"invocation_reason length {len(reason_str)} exceeds 200 chars"
            )

    # ---- 5. parent_op_risk_tier -----------------------------------------
    parent_tier = str(
        invocation.get("parent_op_risk_tier", "") or ""
    ).strip().upper()
    if not parent_tier:
        reasons.append(
            "parent_op_risk_tier missing — GENERAL dispatch requires the "
            "parent op's explicit risk tier"
        )
    elif parent_tier not in _RISK_TIER_ORDER:
        reasons.append(
            f"parent_op_risk_tier={parent_tier!r} not recognized "
            f"(expected one of {sorted(_RISK_TIER_ORDER)})"
        )
    else:
        parent_rank = _RISK_TIER_ORDER[parent_tier]
        floor_rank = _RISK_TIER_ORDER.get(_RISK_TIER_FLOOR_NAME, 1)
        if parent_rank < floor_rank:
            reasons.append(
                f"parent_op_risk_tier={parent_tier} is below the floor "
                f"{_RISK_TIER_FLOOR_NAME} — SAFE_AUTO ops cannot dispatch "
                "GENERAL (blast radius too broad for auto-applied changes)"
            )

    # ---- Cross-field consistency ----------------------------------------
    if max_mut > 0 and tools_set.isdisjoint(_MUTATING_TOOLS):
        reasons.append(
            f"max_mutations={max_mut} > 0 but allowed_tools contains no "
            f"mutating tool — contradiction"
        )
    if max_mut > 0 and parent_tier == "SAFE_AUTO":
        # Extra guard — double-pins the floor check since this is the
        # most dangerous combination.
        reasons.append(
            "max_mutations > 0 forbidden under SAFE_AUTO parent"
        )

    return (not reasons, tuple(reasons))


def is_within_general_subagent(parent_ctx: Any) -> bool:
    """Detect whether ``parent_ctx`` is itself already running inside a
    GENERAL subagent — signal that a recursive GENERAL dispatch would
    be happening.

    The detection looks at a conventional attribute
    ``_within_general_subagent`` on the ctx. Frozen dataclasses can
    carry this via ``object.__setattr__`` when the orchestrator builds
    a sub-context (matching the pattern used elsewhere in the code
    base, e.g. ``task_complexity`` stamping).

    Strict identity check (``is True``) — mocks and auto-attributing
    objects (like unittest.mock.MagicMock) return truthy Mocks for any
    attribute access, which would false-positive recursion rejection.
    Only an explicit ``True`` marker counts.

    Returns False for plain OperationContexts or any object without
    the explicit ``True`` marker — the default assumption is "not
    inside GENERAL".
    """
    return getattr(parent_ctx, "_within_general_subagent", False) is True


def readonly_tool_whitelist() -> FrozenSet[str]:
    """Expose the default read-only tool set for callers that need the
    authoritative list (e.g. AgenticGeneralSubagent passing it to a
    tool loop).
    """
    return _DEFAULT_READONLY_TOOLS


def known_tool_whitelist() -> FrozenSet[str]:
    """All tool names the firewall recognizes (read-only + mutating +
    ask_human + run_tests). Used by callers that need to validate
    against the complete classification surface.
    """
    return _KNOWN_TOOLS


def mutating_tool_set() -> FrozenSet[str]:
    """Tools classified as mutating — require explicit opt-in AND a
    NOTIFY_APPLY+ parent risk tier."""
    return _MUTATING_TOOLS


# ============================================================================
# Antivenom Vector 2: Tool-output prompt injection scanner
# ============================================================================
#
# Scans tool result strings for prompt-injection patterns BEFORE they
# enter the generation prompt. Credential shapes are intentionally
# EXCLUDED — tool outputs legitimately contain secrets (e.g., a
# read_file on a config file). Only the 11 prompt-injection patterns
# run on tool output.
#
# Matched content is redacted in-place (replaced with
# [TOOL_INJECTION_REDACTED: <pattern_hint>]) rather than rejected
# outright (tool loops must continue).


@dataclass(frozen=True)
class ToolOutputScanResult:
    """Structured outcome of tool-output injection scanning.

    ``redacted`` is the output with injection patterns replaced.
    ``injection_count`` is the number of patterns matched.
    ``redacted_patterns`` is the list of pattern hints that fired."""
    redacted: str
    injection_count: int = 0
    redacted_patterns: Tuple[str, ...] = ()


def _tool_output_scan_enabled() -> bool:
    """``JARVIS_TOOL_OUTPUT_INJECTION_SCAN_ENABLED`` (default
    ``true``). Kill switch for tool-output injection scanning.
    Explicit ``false`` disables; empty/unset = default ``true``."""
    raw = os.environ.get(
        "JARVIS_TOOL_OUTPUT_INJECTION_SCAN_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


# Prompt-injection patterns only (NOT credential shapes).
# These are _INJECTION_PATTERNS[:-5] — the first 11 patterns that
# detect role-override, XML injection, and gate-bypass instructions.
# Credential shapes are excluded because tool outputs legitimately
# contain secrets (read_file on a .env file, search_code matching
# an API key constant, etc.).
_TOOL_OUTPUT_INJECTION_PATTERNS: Tuple[re.Pattern, ...] = (
    _INJECTION_PATTERNS[:-len(_CREDENTIAL_SHAPE_PATTERNS)]
)


def scan_tool_output(
    text: str,
    *,
    tool_name: str = "",
    max_chars: int = 65536,
) -> ToolOutputScanResult:
    """Scan a tool result string for prompt-injection patterns.

    Returns a ``ToolOutputScanResult`` with the redacted output.
    Matched patterns are replaced in-place with
    ``[TOOL_INJECTION_REDACTED]``. Credential shapes are NOT
    scanned (tool outputs legitimately contain secrets).

    Parameters
    ----------
    text:
        The tool output string to scan.
    tool_name:
        For diagnostic logging only.
    max_chars:
        Hard length cap. Outputs larger than this are truncated
        before scanning (performance guard). Default 65536.

    NEVER raises."""
    try:
        if not _tool_output_scan_enabled():
            return ToolOutputScanResult(
                redacted=text if isinstance(text, str) else "",
            )

        if not isinstance(text, str) or not text:
            return ToolOutputScanResult(redacted=text or "")

        # Truncate before scanning for performance.
        s = text[:max_chars] if len(text) > max_chars else text

        hit_patterns: List[str] = []
        redacted = s
        for pat in _TOOL_OUTPUT_INJECTION_PATTERNS:
            if pat.search(redacted):
                hit_patterns.append(pat.pattern[:60])
                redacted = pat.sub(
                    "[TOOL_INJECTION_REDACTED]", redacted,
                )

        if hit_patterns:
            logger.warning(
                "[SemanticFirewall] tool_output_injection_redacted "
                "tool=%s patterns=%d hits=%s",
                tool_name or "unknown",
                len(hit_patterns),
                "; ".join(hit_patterns[:5]),
            )

        # Re-attach any truncated tail (unscanned but preserving
        # the original length contract).
        if len(text) > max_chars:
            redacted = redacted + text[max_chars:]

        return ToolOutputScanResult(
            redacted=redacted,
            injection_count=len(hit_patterns),
            redacted_patterns=tuple(hit_patterns),
        )
    except Exception:  # noqa: BLE001 — defensive
        # On any failure, pass through unmodified — never break
        # the tool loop.
        return ToolOutputScanResult(
            redacted=text if isinstance(text, str) else "",
        )


def register_shipped_invariants() -> list:
    """Module-owned shipped-code invariant for the V2 (tool-output
    prompt-injection scanner) Antivenom-v2 surface.

    NEVER raises. Discovery loop catches exceptions."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_v2_tool_output_surface(tree, source) -> tuple:
        violations = []
        required = (
            ("scan_tool_output",
             "V2 tool-output scanner must remain exported"),
            ("ToolOutputScanResult",
             "V2 frozen dataclass must remain exported"),
            ("_tool_output_scan_enabled",
             "V2 master flag helper must remain"),
            ("JARVIS_TOOL_OUTPUT_INJECTION_SCAN_ENABLED",
             "V2 master flag name canonical"),
            ("TOOL_INJECTION_REDACTED",
             "V2 redaction marker canonical"),
        )
        for symbol, reason in required:
            if symbol not in source:
                violations.append(
                    f"V2 surface dropped {symbol!r} — {reason} gone"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="antivenom_v2_tool_output_surface",
            target_file=(
                "backend/core/ouroboros/governance/"
                "semantic_firewall.py"
            ),
            description=(
                "Antivenom V2 (tool-output prompt-injection scanner) "
                "surface MUST preserve scan_tool_output + "
                "ToolOutputScanResult + master-flag helper + "
                "redaction-marker canonical. Catches refactor that "
                "drops the §29 brutal-review tool-output injection "
                "closure."
            ),
            validate=_validate_v2_tool_output_surface,
        ),
    ]


def register_flags(registry):
    """Module-owned FlagRegistry registration for the V2
    (Tool-output prompt-injection scanner) Antivenom-v2 surface.

    Discovery contract: the seed loader walks
    ``governance/`` for top-level modules exposing this name +
    invokes once at boot.

    Returns count of FlagSpecs registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_TOOL_OUTPUT_INJECTION_SCAN_ENABLED",
            type=FlagType.BOOL, default=True,
            description=(
                "Antivenom V2 — semantic-firewall scan over "
                "read-only tool outputs (read_file / search_code "
                "/ etc.) BEFORE the next prompt round consumes "
                "them. 11 prompt-injection detectors; matches "
                "replaced with [TOOL_INJECTION_REDACTED]. "
                "Credential shapes excluded (config-file reads "
                "legitimately contain secrets). Default true — "
                "closes the highest-rank §29 brutal-review "
                "Antivenom bypass vector (tool-output prompt "
                "injection from malicious docstrings / comments "
                "in vendored dependencies)."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "semantic_firewall.py"
            ),
            example="true",
            since="Antivenom v2 (Priority #6)",
        ),
    ]
    try:
        registry.bulk_register(specs, override=True)
    except Exception:  # noqa: BLE001 — defensive
        return 0
    return len(specs)


__all__ = [
    "FirewallResult",
    "ToolOutputScanResult",
    "is_within_general_subagent",
    "known_tool_whitelist",
    "mutating_tool_set",
    "readonly_tool_whitelist",
    "register_flags",
    "sanitize_for_firewall",
    "scan_tool_output",
    "validate_boundary_conditions",
]
