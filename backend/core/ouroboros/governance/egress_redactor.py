"""Scrub secrets out of a payload before it leaves this machine.

Why this fails CLOSED
---------------------

Every other guard in this tree is fail-soft, and correctly so: a telemetry
fault must not break the FSM, a context fault must not block generation. This
one is the exception, and the asymmetry is deliberate.

Fail-soft on an egress scrubber means "when unsure, send it". The payload is
`RepairTrajectoryEmitter`'s preference pair, which carries whole candidate
source in `assistant_output`, `original_response` and `corrected_response`,
and the destination is a network endpoint outside this host. A scrubber that
degrades to pass-through converts one bug into a credential disclosure, and
there is no rollback for bytes that have already left.

So: any fault, any unparseable payload, any unexpected type -> the payload is
DROPPED, not sent. Losing a training sample costs one row in a DPO corpus.
The other failure costs a key.

What it looks for
-----------------

Three passes, all deterministic, no model:

1. **Named assignment** -- `API_KEY = "..."`, `password: str = "..."`,
   `SECRET=...` in .env shape. The NAME is the signal; the value can be
   anything, which is what catches a credential no pattern would recognise.
2. **Known token shapes** -- provider key prefixes, PEM blocks, JWTs,
   connection strings with inline credentials. These catch a secret that was
   never assigned to an obvious name.
3. **AST pass for Python** -- the same question as (1) asked of the parsed
   tree, so a multi-line or concatenated literal is caught where a
   line-oriented regex would miss it.

A redaction preserves structure: the value is replaced, not deleted, so the
scrubbed source still parses and remains usable as training data.
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("Ouroboros.EgressRedactor")

REDACTED = "[REDACTED]"

# Names whose VALUE is a secret regardless of the value's shape. This is the
# pass that catches a credential no token pattern would recognise.
_SECRET_NAME = re.compile(
    r"(?i)\b("
    r"api[_-]?key|apikey|secret|passwd|password|passphrase|"
    r"access[_-]?token|auth[_-]?token|bearer|credential|private[_-]?key|"
    r"client[_-]?secret|session[_-]?key|encryption[_-]?key|salt|"
    r"aws[_-]?secret[_-]?access[_-]?key|aws[_-]?access[_-]?key[_-]?id"
    r")\b"
)

# `NAME = "value"` / `NAME: str = "value"` / `NAME='value'`
_ASSIGN = re.compile(
    r"""(?im)^(?P<lead>\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"""
    r"""(?::\s*[A-Za-z_\[\]\.\, ]+\s*)?=\s*)"""
    r"""(?P<quote>['"])(?P<value>(?:\\.|(?!\2).)*)(?P=quote)"""
)

# `KEY=value` with no quotes -- .env shape.
_ENV_LINE = re.compile(
    r"(?im)^(?P<lead>\s*(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*)"
    r"(?P<value>[^\s#'\"][^\s#]*)"
)

# Dict/JSON entry: "api_key": "value"
_MAPPING = re.compile(
    r"""(?i)(?P<lead>['"](?P<name>[A-Za-z0-9_\-]*"""
    r"""(?:key|secret|token|password|passwd|credential)[A-Za-z0-9_\-]*)"""
    r"""['"]\s*:\s*)(?P<quote>['"])(?P<value>(?:\\.|(?!\3).)*)(?P=quote)"""
)

# Shapes that are secrets wherever they appear.
_TOKEN_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Widened from an exact {35}. Redacting a string that merely LOOKS
    # like a Google key costs one training row; missing a real one
    # costs the key, and only one of those is recoverable.
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("pem_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("url_credentials", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@")),
)


@dataclass
class RedactionReport:
    """What was removed, so a drop is never silent."""

    redactions: int = 0
    kinds: List[str] = field(default_factory=list)
    dropped: bool = False
    reason: str = ""

    @property
    def clean(self) -> bool:
        return self.redactions == 0 and not self.dropped

    def render(self) -> str:
        if self.dropped:
            return f"DROPPED ({self.reason})"
        if not self.redactions:
            return "clean"
        return f"redacted={self.redactions} kinds={sorted(set(self.kinds))}"


def _note(report: RedactionReport, kind: str) -> None:
    report.redactions += 1
    report.kinds.append(kind)


def _redact_named_assignments(text: str, report: RedactionReport) -> str:
    def _sub_assign(m: "re.Match[str]") -> str:
        if not _SECRET_NAME.search(m.group("name")):
            return m.group(0)
        if not m.group("value"):
            return m.group(0)
        _note(report, "named_assignment")
        return f"{m.group('lead')}{m.group('quote')}{REDACTED}{m.group('quote')}"

    def _sub_env(m: "re.Match[str]") -> str:
        if not _SECRET_NAME.search(m.group("name")):
            return m.group(0)
        _note(report, "env_line")
        return f"{m.group('lead')}{REDACTED}"

    def _sub_map(m: "re.Match[str]") -> str:
        if not m.group("value"):
            return m.group(0)
        _note(report, "mapping_entry")
        return f"{m.group('lead')}{m.group('quote')}{REDACTED}{m.group('quote')}"

    text = _ASSIGN.sub(_sub_assign, text)
    text = _ENV_LINE.sub(_sub_env, text)
    return _MAPPING.sub(_sub_map, text)


def _redact_token_shapes(text: str, report: RedactionReport) -> str:
    for kind, pattern in _TOKEN_SHAPES:
        # A credentialed URL keeps its trailing "@" so the remainder still
        # reads as a URL; every other shape is replaced outright.
        replacement = REDACTED + "@" if kind == "url_credentials" else REDACTED
        text, n = pattern.subn(replacement, text)
        for _ in range(n):
            _note(report, kind)
    return text


def _redact_python_ast(text: str, report: RedactionReport) -> str:
    """Catch secret-named assignments a line regex cannot see.

    A concatenated or multi-line literal spans lines, so the line-oriented
    pass misses it; the parsed tree does not. Skipped when the text is not
    Python -- which is normal here, since a candidate may be JSON or YAML.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return text

    spans: List[Tuple[int, int, str]] = []
    for node in ast.walk(tree):
        names: List[str] = []
        value = None
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        if not names or value is None:
            continue
        if not any(_SECRET_NAME.search(n) for n in names):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if not value.value:
                continue
            spans.append((value.lineno, value.end_lineno or value.lineno, "ast_assignment"))

    if not spans:
        return text
    lines = text.splitlines(keepends=True)
    for start, end, kind in sorted(spans, reverse=True):
        if start < 1 or end > len(lines):
            continue
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        head = lines[start - 1][:indent]
        eq = lines[start - 1].split("=", 1)[0]
        lines[start - 1:end] = [f"{eq}= \"{REDACTED}\"\n" if "=" in lines[start - 1]
                                else f"{head}\"{REDACTED}\"\n"]
        _note(report, kind)
    return "".join(lines)


def redact_text(text: str) -> Tuple[str, RedactionReport]:
    """Scrub one string. Raises nothing; the caller decides on the report."""
    report = RedactionReport()
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else "", report
    out = _redact_named_assignments(text, report)
    out = _redact_token_shapes(out, report)
    out = _redact_python_ast(out, report)
    return out, report


def redact_payload(payload: Any) -> Tuple[Any, RedactionReport]:
    """Scrub every string in a payload, recursively. FAILS CLOSED.

    On any fault the payload is reported ``dropped`` and the caller must not
    send it. That inverts this tree's usual fail-soft posture on purpose:
    degrading to pass-through here would turn a scrubber bug into a
    credential disclosure, and bytes that have left cannot be recalled.
    """
    report = RedactionReport()

    def _walk(node: Any, depth: int = 0) -> Any:
        if depth > 32:
            raise ValueError("payload nested beyond 32 levels")
        if isinstance(node, str):
            scrubbed, sub = redact_text(node)
            report.redactions += sub.redactions
            report.kinds.extend(sub.kinds)
            return scrubbed
        if isinstance(node, dict):
            return {k: _walk(v, depth + 1) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return type(node)(_walk(v, depth + 1) for v in node)
        if isinstance(node, (int, float, bool)) or node is None:
            return node
        raise TypeError(f"unscrubbable payload member: {type(node).__name__}")

    try:
        return _walk(payload), report
    except Exception as exc:  # noqa: BLE001 — fail CLOSED
        report.dropped = True
        report.reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[EgressRedactor] payload DROPPED rather than sent unscrubbed: %s",
            report.reason,
        )
        return None, report


__all__ = [
    "REDACTED",
    "RedactionReport",
    "redact_payload",
    "redact_text",
]
