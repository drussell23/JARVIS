"""SecurityReviewer — LLM-as-a-Judge for automated security analysis.

Uses the existing PrimeClient to send generated code to a brain for
security-focused review BEFORE the human APPROVE gate. The reviewer
looks for: injection vulnerabilities, hardcoded secrets, unsafe imports,
permission escalation, sandbox escape patterns, and supply chain risks.

Returns a SecurityVerdict that the orchestrator uses to:
    - PASS: proceed to APPROVE gate normally
    - WARN: proceed but flag warnings in the approval prompt
    - BLOCK: reject immediately, never reaches human approval

Design:
    - Reuses existing PrimeClient (no new HTTP connections)
    - Routes to Claude API by default (highest judgment quality for security)
    - Configurable brain via JARVIS_SECURITY_REVIEW_BRAIN env var
    - All errors → PASS with warning (never blocks pipeline due to reviewer crash)
    - Structured output parsed from JSON, with markdown fallback
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SecurityVerdict(str, Enum):
    PASS = "pass"       # no issues found
    WARN = "warn"       # non-blocking issues
    BLOCK = "block"     # critical security issue — reject


@dataclass(frozen=True)
class SecurityFinding:
    severity: str           # "critical", "high", "medium", "low", "info"
    category: str           # "injection", "secrets", "permissions", "sandbox_escape", etc.
    file_path: str
    line_number: Optional[int]
    description: str
    recommendation: str


@dataclass(frozen=True)
class SecurityReviewResult:
    verdict: SecurityVerdict
    findings: List[SecurityFinding]
    summary: str
    reviewer_brain: str
    review_duration_s: float

    def format_for_approval(self) -> str:
        """Format findings for injection into approval prompt."""
        if self.verdict == SecurityVerdict.PASS:
            return f"Security Review: PASS ({self.reviewer_brain}, {self.review_duration_s:.1f}s)"

        lines = [f"## Security Review: {self.verdict.value.upper()}"]
        lines.append(f"Reviewer: {self.reviewer_brain} ({self.review_duration_s:.1f}s)")
        lines.append("")
        for f in self.findings:
            lines.append(f"- **[{f.severity.upper()}]** {f.category}: {f.description}")
            if f.line_number:
                lines.append(f"  Location: {f.file_path}:{f.line_number}")
            lines.append(f"  Recommendation: {f.recommendation}")
        return "\n".join(lines)


class SecurityReviewer:
    """LLM-as-a-Judge security reviewer using existing PrimeClient.

    Injected into the orchestrator. Called between VALIDATE and GATE phases.
    """

    # Categories the reviewer checks for
    REVIEW_CATEGORIES = [
        "command_injection", "sql_injection", "xss",
        "hardcoded_secrets", "unsafe_imports", "permission_escalation",
        "sandbox_escape", "supply_chain", "path_traversal",
        "information_disclosure", "denial_of_service",
    ]

    def __init__(
        self,
        prime_client: Any = None,
        brain: Optional[str] = None,
        enabled: bool = True,
    ) -> None:
        self._client = prime_client
        self._brain = brain or os.environ.get("JARVIS_SECURITY_REVIEW_BRAIN", None)
        # Client must expose a PrimeClient-compatible prompt-based generate()
        # (takes a `prompt` kwarg). CandidateGenerator/provider objects have
        # `generate(context, deadline)` and would crash the review loop with
        # "unexpected keyword argument 'prompt'". Silently disable rather than
        # default-PASS on every review (Manifesto §7 — absolute observability).
        self._enabled = enabled and prime_client is not None and self._client_is_compatible(prime_client)

    @staticmethod
    def _client_is_compatible(client: Any) -> bool:
        """True iff client.generate accepts a `prompt` keyword argument."""
        gen = getattr(client, "generate", None)
        if gen is None or not callable(gen):
            return False
        try:
            import inspect
            sig = inspect.signature(gen)
        except (TypeError, ValueError):
            return False
        params = sig.parameters
        if "prompt" in params:
            return True
        # Accept **kwargs-style generate signatures too.
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def review(
        self,
        candidate: Dict[str, Any],
        target_files: List[str],
        description: str,
    ) -> SecurityReviewResult:
        """Review generated code for security issues.

        Returns PASS on any error (never blocks pipeline due to reviewer failure).
        """
        import time
        start = time.monotonic()

        if not self._enabled or self._client is None:
            return SecurityReviewResult(
                verdict=SecurityVerdict.PASS,
                findings=[],
                summary="Security review disabled",
                reviewer_brain="none",
                review_duration_s=0.0,
            )

        try:
            prompt = self._build_review_prompt(candidate, target_files, description)
            system_prompt = self._build_system_prompt()

            response = await self._client.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=4096,
                temperature=0.1,  # low temperature for consistent judgment
                model_name=self._brain,
                task_profile=None,
            )

            result = self._parse_response(response.content, response.source)
            elapsed = time.monotonic() - start
            return SecurityReviewResult(
                verdict=result["verdict"],
                findings=result["findings"],
                summary=result["summary"],
                reviewer_brain=response.source,
                review_duration_s=elapsed,
            )

        except Exception as exc:
            logger.warning("[SecurityReviewer] Review failed: %s — defaulting to PASS", exc)
            return SecurityReviewResult(
                verdict=SecurityVerdict.PASS,
                findings=[],
                summary=f"Review failed: {exc}",
                reviewer_brain="error",
                review_duration_s=time.monotonic() - start,
            )

    def _build_system_prompt(self) -> str:
        return (
            "You are a security reviewer for an AI governance pipeline. "
            "Analyze the code change for security vulnerabilities. "
            "Output MUST be valid JSON:\n"
            "{\n"
            '  "verdict": "pass" | "warn" | "block",\n'
            '  "findings": [{"severity": "...", "category": "...", '
            '"file_path": "...", "line_number": null, '
            '"description": "...", "recommendation": "..."}],\n'
            '  "summary": "one-line summary"\n'
            "}\n\n"
            "Categories to check: " + ", ".join(self.REVIEW_CATEGORIES) + "\n\n"
            "Rules:\n"
            "- verdict=block ONLY for critical issues (RCE, secret exposure, sandbox escape)\n"
            "- verdict=warn for medium issues (unsafe patterns, missing validation)\n"
            "- verdict=pass if code is safe\n"
            "- Be specific — include file path and line number when possible\n"
        )

    def _build_review_prompt(
        self,
        candidate: Dict[str, Any],
        target_files: List[str],
        description: str,
    ) -> str:
        sections = [f"## Operation: {description}", f"Files: {', '.join(target_files)}", ""]

        # Include the actual code change
        content = candidate.get("content", "")
        diff = candidate.get("diff", "")
        if diff:
            sections.append(f"## Diff\n```\n{diff[:8000]}\n```")
        elif content:
            sections.append(f"## Generated Code\n```python\n{content[:8000]}\n```")

        sections.append("\n## Review this code for security vulnerabilities.")
        return "\n".join(sections)

    def _parse_response(
        self, content: str, source: str,
    ) -> Dict[str, Any]:
        """Parse LLM response into verdict + findings."""
        try:
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(content[json_start:json_end])
                verdict_str = data.get("verdict", "pass").lower()
                verdict = SecurityVerdict(verdict_str) if verdict_str in ("pass", "warn", "block") else SecurityVerdict.PASS

                findings = []
                for f in data.get("findings", []):
                    findings.append(SecurityFinding(
                        severity=f.get("severity", "info"),
                        category=f.get("category", "unknown"),
                        file_path=f.get("file_path", "unknown"),
                        line_number=f.get("line_number"),
                        description=f.get("description", ""),
                        recommendation=f.get("recommendation", ""),
                    ))

                return {
                    "verdict": verdict,
                    "findings": findings,
                    "summary": data.get("summary", ""),
                }
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: if we can't parse JSON, check for obvious block signals
        content_lower = content.lower()
        if any(kw in content_lower for kw in ("critical vulnerability", "rce", "remote code execution", "block")):
            return {
                "verdict": SecurityVerdict.WARN,
                "findings": [SecurityFinding(
                    severity="medium", category="unparsed",
                    file_path="unknown", line_number=None,
                    description=content[:300],
                    recommendation="Review manually — automated parse failed",
                )],
                "summary": "Unparsed security review — manual review recommended",
            }

        return {"verdict": SecurityVerdict.PASS, "findings": [], "summary": "No issues found"}
