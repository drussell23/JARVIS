"""
MCP Output Scanner — SemanticGuardian ↔ MCP Boundary
======================================================

Closes §40 Wave 3 #5 — the final Wave 3 arc. Per operator binding §40.5:

  "SemanticGuardian's 11 injection detectors tune for internal tools.
   External MCP tools (GitHub MCP, Drive MCP) aren't pattern-matched.
   Add MCP server whitelist + pattern-delta detection + post-tool-call
   output scanning for injection signatures."

The framing identifies a structural gap: SemanticGuardian's pattern set
is Python-AST-shape-focused (removed imports / function body collapse /
guard inversions / etc.). External MCP tool outputs are typically JSON
or free-form text payloads where guardian's AST detectors don't apply.
But the canonical credential-shape regex set (composed by Tier -1 in
conversation_bridge.py) DOES apply directly to text payloads.

This module ships the **substrate layer** — a pure-function scanner
that consumes arbitrary MCP output text, composes the canonical
redact_secrets pipeline, and produces a frozen verdict tuple suitable
for upstream MCP integration hooks (mcp_tool_client / tool_executor)
that the operator can wire as a follow-on slice.

Composition contract — thin pure-function wrapper, zero parallel state,
zero hardcoded credential regexes:

* :func:`conversation_bridge.redact_secrets` — canonical Tier -1
  credential-shape redactor. Composing this guarantees the MCP scanner
  matches the SAME 5-pattern set the rest of the cage uses (operator
  binding: no parallel pattern catalog). The 5 patterns are:
  ``sk-*`` (OpenAI) / ``xox*-`` (Slack) / ``AKIA*`` (AWS) /
  ``-----BEGIN .* PRIVATE KEY-----`` / ``gh[pousr]_*`` (GitHub).
* :func:`governance_boundary_gate._normalize_path` (lazy-import only
  when needed) — reused canonically when scanning MCP tool calls
  that include path-shaped arguments.

NEVER raises. Malformed text / missing substrate / env-lookup failure
all degrade to ``CLEAN`` or ``DISABLED`` verdict, not exception.

Closed 3-value :class:`McpScanVerdict` taxonomy:

  CLEAN              ✓  no credential shapes detected
  CREDENTIAL_FOUND   ⚠  ≥1 credential-shape match in output
  DISABLED           ◌  master flag off

Closed 5-value :class:`CredentialKind` taxonomy (mirrors the canonical
``[REDACTED:label]`` markers emitted by ``redact_secrets``):

  OPENAI_KEY     — sk-* pattern
  SLACK_TOKEN    — xox*- pattern
  AWS_KEY        — AKIA* pattern
  PRIVATE_KEY    — -----BEGIN PRIVATE KEY----- block
  GITHUB_TOKEN   — gh[pousr]_* pattern

Pattern-delta detection
-----------------------
When ``prior_conversation`` is supplied, the scanner subtracts findings
that ALREADY appeared in the prior text — only NEWLY-INTRODUCED
credential shapes are reported. This is the canonical interpretation
of the §40.5 "pattern-delta detection" framing: the MCP tool brought
in a new credential that wasn't in the conversation before.

MCP server whitelist
--------------------
Operator-tunable comma-separated allowlist via
``JARVIS_MCP_SERVER_WHITELIST``. Empty (default) means "no allowlist
enforced — scan ALL MCP outputs". When populated, the predicate
:func:`is_server_whitelisted` returns True ONLY for the listed servers.
Substrate exposes the predicate; per-server enforcement is the
follow-on integration slice.

§33.1 master flag ``JARVIS_MCP_OUTPUT_SCANNER_ENABLED`` default-**FALSE**
per the cognitive-substrate convention. Operator opts in once the
follow-on integration hook is wired in mcp_tool_client.

Authority asymmetry (AST-pinned): imports stdlib +
conversation_bridge (for canonical redact_secrets) ONLY. Does NOT
import orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
mcp_tool_client. The substrate is pure-read; the consumer-side
integration (mcp_tool_client wraps scan_mcp_output around call_tool)
is a separate slice.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


MCP_OUTPUT_SCANNER_SCHEMA_VERSION: str = "mcp_output_scanner.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_MCP_OUTPUT_SCANNER_ENABLED"
_ENV_SERVER_WHITELIST = "JARVIS_MCP_SERVER_WHITELIST"
_ENV_MAX_FINDINGS = "JARVIS_MCP_OUTPUT_SCANNER_MAX_FINDINGS"

_DEFAULT_MAX_FINDINGS = 64
_MIN_MAX_FINDINGS = 1
_MAX_MAX_FINDINGS = 10_000


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Substrate returns ``DISABLED`` verdict
    + zero cost when off. Flip the master flag once the
    per-MCP-server integration hook (in mcp_tool_client) is wired
    in a follow-on slice.
    """
    return _flag(_ENV_MASTER, default=False)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def max_findings() -> int:
    """Defensive ceiling on findings per scan. Clamped to
    [1, 10_000]; default 64."""
    return _read_clamped_int(
        _ENV_MAX_FINDINGS,
        _DEFAULT_MAX_FINDINGS,
        _MIN_MAX_FINDINGS,
        _MAX_MAX_FINDINGS,
    )


def whitelisted_servers() -> FrozenSet[str]:
    """Operator-supplied allowlist of MCP server names (frozenset).

    Reads comma-separated ``JARVIS_MCP_SERVER_WHITELIST``. Empty /
    unset means "no allowlist enforced" — the predicate
    :func:`is_server_whitelisted` returns True for every server
    when the set is empty. NEVER raises.
    """
    raw = os.environ.get(_ENV_SERVER_WHITELIST, "").strip()
    if not raw:
        return frozenset()
    parts = (s.strip().lower() for s in raw.split(","))
    return frozenset(p for p in parts if p)


def is_server_whitelisted(server_name: str) -> bool:
    """Pure predicate. Returns True iff:
      * The whitelist is empty (no enforcement), OR
      * ``server_name`` appears in the whitelist (case-insensitive).
    NEVER raises.
    """
    try:
        normalized = str(server_name or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    allowed = whitelisted_servers()
    if not allowed:
        return True  # No allowlist → universal allow
    return normalized in allowed


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class McpScanVerdict(str, enum.Enum):
    """Closed 3-value scan verdict — bytes-pinned via AST."""

    CLEAN = "clean"
    CREDENTIAL_FOUND = "credential_found"
    DISABLED = "disabled"


class CredentialKind(str, enum.Enum):
    """Closed 5-value credential taxonomy — bytes-pinned via AST.

    Values mirror the canonical ``[REDACTED:label]`` markers
    emitted by :func:`conversation_bridge.redact_secrets`. Drift
    requires updating both the conversation_bridge pattern set
    AND this enum.
    """

    OPENAI_KEY = "openai_key"
    SLACK_TOKEN = "slack_token"
    AWS_KEY = "aws_key"
    PRIVATE_KEY = "private_key"
    GITHUB_TOKEN = "github_token"


# Canonical map from redact_secrets' label tokens (lowercase
# hyphenated) to the closed CredentialKind enum. Bytes-pinned via
# AST so drift is structurally caught.
_LABEL_TO_KIND: Dict[str, CredentialKind] = {
    "openai-key": CredentialKind.OPENAI_KEY,
    "slack-token": CredentialKind.SLACK_TOKEN,
    "aws-access-key": CredentialKind.AWS_KEY,
    "private-key-block": CredentialKind.PRIVATE_KEY,
    "github-token": CredentialKind.GITHUB_TOKEN,
}


def _coerce_label(label: str) -> Optional[CredentialKind]:
    """Map a redact_secrets label string to a CredentialKind, or
    None when unrecognized (defensive)."""
    if not label:
        return None
    return _LABEL_TO_KIND.get(label.strip().lower())


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class McpScanFinding:
    """One credential-shape finding — frozen audit record."""

    kind: CredentialKind
    label: str
    """The canonical redact_secrets label that fired
    (e.g. ``"openai-key"``, ``"github-token"``)."""
    occurrences_in_output: int
    is_pattern_delta: bool
    """True when prior_conversation was supplied AND this kind
    was NOT present in the prior text. False when the kind was
    pre-existing OR no prior was supplied."""
    source_label: str
    """Operator-supplied tag identifying the MCP source (e.g.
    ``"mcp_github_search"``). Bounded at 128 chars."""
    schema_version: str = MCP_OUTPUT_SCANNER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "label": self.label,
            "occurrences_in_output": int(
                self.occurrences_in_output,
            ),
            "is_pattern_delta": bool(self.is_pattern_delta),
            "source_label": self.source_label[:128],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class McpScanReport:
    """Aggregate MCP-output scan report — frozen §33.5 artifact."""

    scanned_at_unix: float
    master_enabled: bool
    verdict: McpScanVerdict
    findings: Tuple[McpScanFinding, ...]
    """Bounded at :func:`max_findings`."""
    bytes_scanned: int
    bytes_redacted: int
    """Total bytes belonging to credential shapes in the
    *scanned* text. From the canonical redact_secrets pipeline."""
    delta_mode: bool
    """True when prior_conversation was supplied — findings
    reflect NEW credentials only."""
    source_label: str
    diagnostic: str
    elapsed_s: float
    schema_version: str = MCP_OUTPUT_SCANNER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanned_at_unix": float(self.scanned_at_unix),
            "master_enabled": bool(self.master_enabled),
            "verdict": self.verdict.value,
            "findings": [f.to_dict() for f in self.findings],
            "bytes_scanned": int(self.bytes_scanned),
            "bytes_redacted": int(self.bytes_redacted),
            "delta_mode": bool(self.delta_mode),
            "source_label": self.source_label[:128],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Canonical accessor composition (no parallel state)
# ===========================================================================


def _redact_secrets(text: str) -> Tuple[str, int]:
    """Compose canonical :func:`conversation_bridge.redact_secrets`.
    Returns (redacted_text, bytes_redacted). NEVER raises —
    returns (text, 0) on any failure."""
    if not text:
        return (text or "", 0)
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            redact_secrets,
        )
        result = redact_secrets(text)
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], str)
        ):
            return (result[0], int(result[1] or 0))
    except Exception:  # noqa: BLE001
        pass
    return (text, 0)


# Regex matching the canonical [REDACTED:<label>] marker emitted
# by conversation_bridge.redact_secrets. Bytes-pinned via AST.
_REDACTED_MARKER_RE = re.compile(
    r"\[REDACTED:([a-zA-Z0-9_\-]+)\]",
)


def _enumerate_findings(
    redacted_text: str,
    source_label: str,
    prior_labels: Optional[FrozenSet[str]] = None,
) -> List[McpScanFinding]:
    """Pure-function. Walks the redacted text, counts per-label
    marker occurrences, maps to CredentialKind, returns frozen
    findings. NEVER raises.

    ``prior_labels`` (when supplied) excludes credentials that
    appeared in the prior conversation — the pattern-delta
    filter.
    """
    occurrences: Dict[str, int] = {}
    try:
        for match in _REDACTED_MARKER_RE.finditer(redacted_text or ""):
            label = match.group(1).strip().lower()
            occurrences[label] = occurrences.get(label, 0) + 1
    except Exception:  # noqa: BLE001
        return []
    out: List[McpScanFinding] = []
    cap = max_findings()
    for label, count in sorted(occurrences.items()):
        if len(out) >= cap:
            break
        kind = _coerce_label(label)
        if kind is None:
            continue
        is_delta = True
        if prior_labels is not None:
            is_delta = label not in prior_labels
        out.append(McpScanFinding(
            kind=kind,
            label=label,
            occurrences_in_output=int(count),
            is_pattern_delta=is_delta,
            source_label=str(source_label or ""),
        ))
    return out


def _extract_labels(text: str) -> FrozenSet[str]:
    """Return the set of canonical credential labels present in
    ``text``. Composes the canonical redact_secrets pipeline +
    parses [REDACTED:<label>] markers. NEVER raises."""
    if not text:
        return frozenset()
    redacted, n = _redact_secrets(text)
    if n <= 0:
        return frozenset()
    labels: set = set()
    try:
        for match in _REDACTED_MARKER_RE.finditer(redacted):
            label = match.group(1).strip().lower()
            if label:
                labels.add(label)
    except Exception:  # noqa: BLE001
        pass
    return frozenset(labels)


# ===========================================================================
# Top-level scanner — operator-callable
# ===========================================================================


def scan_mcp_output(
    text: str,
    *,
    source_label: str = "",
    prior_conversation: Optional[str] = None,
    server_name: str = "",
) -> McpScanReport:
    """Pure-function MCP output scanner. NEVER raises.

    Parameters
    ----------
    text:
        Raw MCP tool output. Typically the ``content[*].text``
        field of an MCP JSON-RPC result, or the JSON-stringified
        payload itself.
    source_label:
        Operator-supplied tag identifying the MCP source
        (e.g. ``"mcp_github_search"``). Bounded at 128 chars in
        findings + report.
    prior_conversation:
        When supplied, enables **pattern-delta detection** — only
        credentials NEW to this MCP output (not in the prior
        text) are flagged as ``is_pattern_delta=True``.
    server_name:
        Optional MCP server name (e.g. ``"github"``). When the
        :func:`is_server_whitelisted` predicate returns False
        (server not in operator allowlist), the report carries
        a ``not_whitelisted`` diagnostic — but the scan still
        runs since the goal is observability, not enforcement.

    Returns
    -------
    McpScanReport
        Frozen §33.5 artifact. When master flag is off →
        ``DISABLED`` verdict, zero I/O.
    """
    started = time.time()

    if not master_enabled():
        return McpScanReport(
            scanned_at_unix=started,
            master_enabled=False,
            verdict=McpScanVerdict.DISABLED,
            findings=(),
            bytes_scanned=0,
            bytes_redacted=0,
            delta_mode=prior_conversation is not None,
            source_label=str(source_label or ""),
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false — "
                "operator opt-in workflow"
            ),
            elapsed_s=0.0,
        )

    # Defensive coercion.
    try:
        scan_text = str(text or "")
    except Exception:  # noqa: BLE001
        scan_text = ""
    bytes_scanned = len(scan_text)

    if not scan_text.strip():
        return McpScanReport(
            scanned_at_unix=started,
            master_enabled=True,
            verdict=McpScanVerdict.CLEAN,
            findings=(),
            bytes_scanned=bytes_scanned,
            bytes_redacted=0,
            delta_mode=prior_conversation is not None,
            source_label=str(source_label or ""),
            diagnostic="empty output — no credential shapes possible",
            elapsed_s=time.time() - started,
        )

    redacted, bytes_redacted = _redact_secrets(scan_text)

    # Pattern-delta mode — compute the prior set of credential
    # labels and EXCLUDE pre-existing labels from the findings.
    prior_labels: Optional[FrozenSet[str]] = None
    delta_mode = False
    if prior_conversation is not None:
        delta_mode = True
        prior_labels = _extract_labels(prior_conversation)

    findings = _enumerate_findings(
        redacted, source_label, prior_labels=prior_labels,
    )

    # Filter out pre-existing-only findings when in delta mode —
    # report only NEW credentials.
    if delta_mode:
        findings = [f for f in findings if f.is_pattern_delta]

    if findings:
        verdict = McpScanVerdict.CREDENTIAL_FOUND
        # Build a concise diagnostic listing kinds + counts.
        per_kind: Dict[str, int] = {}
        for f in findings:
            per_kind[f.kind.value] = (
                per_kind.get(f.kind.value, 0)
                + f.occurrences_in_output
            )
        kinds_summary = ",".join(
            f"{k}={v}" for k, v in sorted(per_kind.items())
        )
        whitelist_note = ""
        if server_name:
            if not is_server_whitelisted(server_name):
                whitelist_note = (
                    f" not_whitelisted:{server_name}"
                )
        diagnostic = (
            f"{len(findings)} credential kind(s) found "
            f"({kinds_summary}){whitelist_note}"
        )
    else:
        verdict = McpScanVerdict.CLEAN
        diagnostic = (
            "clean — no credential shapes detected"
            if not delta_mode
            else "clean — no NEW credential shapes in delta mode"
        )

    return McpScanReport(
        scanned_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        findings=tuple(findings),
        bytes_scanned=bytes_scanned,
        bytes_redacted=bytes_redacted,
        delta_mode=delta_mode,
        source_label=str(source_label or ""),
        diagnostic=diagnostic,
        elapsed_s=time.time() - started,
    )


# ===========================================================================
# Renderer
# ===========================================================================


def format_scan_panel(
    report: Optional[McpScanReport] = None,
    *,
    text: Optional[str] = None,
    source_label: str = "",
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"MCP output scanner: disabled "
                f"({_ENV_MASTER}=false)"
            )
        report = scan_mcp_output(
            text or "", source_label=source_label,
        )
    if not report.master_enabled:
        return (
            f"MCP output scanner: disabled "
            f"({_ENV_MASTER}=false)"
        )
    lines = [
        f"🔐 MCP Output Scan  ({report.verdict.value})",
        f"  source              : {report.source_label or '(unset)'}",
        f"  bytes_scanned       : {report.bytes_scanned}",
        f"  bytes_redacted      : {report.bytes_redacted}",
        f"  delta_mode          : {report.delta_mode}",
        f"  findings            : {len(report.findings)}",
    ]
    if report.findings:
        lines.append("  per-kind findings:")
        for f in report.findings[:8]:
            tag = " (delta)" if f.is_pattern_delta else ""
            lines.append(
                f"    - {f.kind.value:<14} "
                f"occurrences={f.occurrences_in_output}"
                f"{tag}"
            )
        if len(report.findings) > 8:
            lines.append(
                f"    ... (+{len(report.findings) - 8} more)"
            )
    lines.append(f"  diagnostic          : {report.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "mcp_output_scanner.py"
    )

    _EXPECTED_VERDICTS = {
        "clean", "credential_found", "disabled",
    }
    _EXPECTED_KINDS = {
        "openai_key", "slack_token",
        "aws_key", "private_key", "github_token",
    }
    _EXPECTED_LABELS = {
        "openai-key", "slack-token", "aws-access-key",
        "private-key-block", "github-token",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "McpScanVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"McpScanVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"McpScanVerdict drift: {sorted(extra)}",
                    )
                return ()
        return ("McpScanVerdict class not found",)

    def _validate_kind_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CredentialKind"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_KINDS - found
                extra = found - _EXPECTED_KINDS
                if missing:
                    return (
                        f"CredentialKind missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"CredentialKind drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("CredentialKind class not found",)

    def _validate_label_map_coverage(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """The _LABEL_TO_KIND dict MUST cover all 5 canonical
        redact_secrets labels verbatim. Drift = silently dropping
        a credential shape from the MCP scan."""
        for node in ast.walk(tree):
            # Plain or annotated assignment named _LABEL_TO_KIND
            value_node = None
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_LABEL_TO_KIND"
                and isinstance(node.value, ast.Dict)
            ):
                value_node = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_LABEL_TO_KIND"
                and isinstance(node.value, ast.Dict)
            ):
                value_node = node.value
            if value_node is None:
                continue
            found = set()
            for k in value_node.keys:
                if (
                    isinstance(k, ast.Constant)
                    and isinstance(k.value, str)
                ):
                    found.add(k.value)
            missing = _EXPECTED_LABELS - found
            if missing:
                return (
                    f"_LABEL_TO_KIND missing labels: "
                    f"{sorted(missing)}",
                )
            extra = found - _EXPECTED_LABELS
            if extra:
                return (
                    f"_LABEL_TO_KIND has unexpected labels: "
                    f"{sorted(extra)}",
                )
            return ()
        return ("_LABEL_TO_KIND dict not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.mcp_tool_client",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "conversation_bridge" not in source:
            violations.append(
                "must compose canonical "
                "conversation_bridge.redact_secrets (no "
                "parallel credential regex set)",
            )
        if "redact_secrets" not in source:
            violations.append(
                "must compose redact_secrets — the canonical "
                "Tier -1 pipeline. NO parallel regex match.",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "mcp_output_scanner_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "McpScanVerdict 3-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mcp_output_scanner_kind_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CredentialKind 5-value taxonomy bytes-pinned. "
                "Adding/removing a kind requires updating both "
                "this enum AND _LABEL_TO_KIND verbatim — drift "
                "silently drops a credential shape."
            ),
            validate=_validate_kind_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mcp_output_scanner_label_map_coverage"
            ),
            target_file=target,
            description=(
                "_LABEL_TO_KIND dict MUST cover all 5 "
                "canonical redact_secrets labels."
            ),
            validate=_validate_label_map_coverage,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mcp_output_scanner_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure-function scanner. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / mcp_tool_client."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mcp_output_scanner_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mcp_output_scanner_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes canonical "
                "conversation_bridge.redact_secrets — no "
                "parallel credential regex set, no parallel "
                "Tier -1 pipeline."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "mcp_output_scanner.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "MCP output scanner master switch. §33.1 "
                "cognitive substrate default-FALSE. Operator "
                "opts in once the per-MCP-server integration "
                "hook (in mcp_tool_client) is wired in a "
                "follow-on slice."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_SERVER_WHITELIST,
            type=FlagType.STR,
            default="",
            description=(
                "Operator-supplied comma-separated allowlist "
                "of MCP server names. Empty (default) means "
                "'no allowlist enforced' — predicate "
                "is_server_whitelisted returns True for every "
                "server. Populated → only listed servers pass."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_SERVER_WHITELIST}=github,drive",
        ),
        FlagSpec(
            name=_ENV_MAX_FINDINGS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_FINDINGS,
            description=(
                "Defensive ceiling on findings per scan. "
                "Clamped to [1, 10_000]; default 64."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_FINDINGS}=200",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "MCP_OUTPUT_SCANNER_SCHEMA_VERSION",
    "McpScanVerdict",
    "CredentialKind",
    "McpScanFinding",
    "McpScanReport",
    "master_enabled",
    "max_findings",
    "whitelisted_servers",
    "is_server_whitelisted",
    "scan_mcp_output",
    "format_scan_panel",
    "register_shipped_invariants",
    "register_flags",
]
