"""
Intelligence Hooks — Pre-GENERATE and Pre-GATE cognitive enhancements.

P0 intelligence gaps that make Ouroboros smarter:

1. TestGenerationHook: When creating new modules, inject instruction to
   also generate tests. Ensures no code ships without coverage.

2. TestCoverageEnforcer: Pre-GENERATE check for existing test coverage.
   If target files have zero tests, inject 'also generate tests' into prompt.

3. SemanticReviewGate: Pre-APPROVE security and logic review. Sends
   candidate to provider for focused review when modifying sensitive paths.

Boundary Principle:
  Deterministic: file existence checks, path pattern matching, test discovery.
  Agentic: test content generation and review analysis by the model.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security-sensitive path patterns (deterministic — no inference)
# ---------------------------------------------------------------------------
_SECURITY_SENSITIVE_PATTERNS: Tuple[str, ...] = (
    "auth", "login", "password", "credential", "secret", "token",
    "crypto", "encrypt", "decrypt", "hash", "session", "permission",
    "oauth", "jwt", "api_key", "unlock", "biometric", "voiceprint",
    "security", "firewall", "sanitize", "escape", "injection",
)

_REVIEW_ALWAYS_PATHS: Tuple[str, ...] = (
    "unified_supervisor.py",
    "backend/core/prime_router.py",
    "backend/core/prime_client.py",
    "backend/core/distributed_lock_manager.py",
    "backend/voice_unlock/",
    "backend/core/ouroboros/governance/orchestrator.py",
    "backend/core/ouroboros/governance/change_engine.py",
)


# ---------------------------------------------------------------------------
# Slice 239 — Adaptive Test-Sharding & Asynchronous Enforcement (layer 9).
#
# When a heavy multi-file GOAL has >2 uncovered files AND the remaining budget is
# tight, injecting "also generate N test files" into the PRIMARY op's prompt
# balloons it past its deadline. Instead, compile an ISOLATED test-coverage
# payload and emit it as a SEPARATE background signal into the existing
# UnifiedIntakeRouter WAL queue (reuse make_envelope + router.ingest + the
# intake→op pipeline) so the primary patch graduates cleanly and a later
# independent op fulfils coverage. Adaptive (route/budget/complexity-scaled, no
# hardcoded cap), env-tunable, gated, fail-soft (no router → legacy inline inject).
# ---------------------------------------------------------------------------

# Route-scaled budget floor: decouple when remaining_s drops below the route's
# floor. base × per-route fraction (tighter routes decouple earlier). The base is
# env-tunable; the fractions mirror the route cost/deadline tiers (CLAUDE.md §5).
_TEST_SHARD_ROUTE_FRACTIONS: Dict[str, float] = {
    "immediate": 0.5,     # ~45s of the 90s base — very tight reflex window
    "standard": 0.67,     # ~60s
    "complex": 1.0,       # ~90s
    "background": 1.33,   # ~120s
    "speculative": 2.0,   # ~180s
}


def test_sharding_enabled() -> bool:
    """Master switch for adaptive test-sharding (layer 9). Default TRUE — gated +
    fail-soft (no router → legacy inline inject) + only fires when budget is tight
    AND >2 files are uncovered, so light/ample ops are byte-identical. NEVER raises."""
    raw = (os.environ.get("JARVIS_TEST_SHARDING_ENABLED", "true") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _test_shard_min_uncovered() -> int:
    """Minimum uncovered-file count to warrant a SEPARATE test-coverage op (the
    ">2 files" gate). Below this, inline injection is cheap enough. Default 3.
    Env JARVIS_TEST_SHARD_MIN_UNCOVERED. Invalid/non-positive → default."""
    raw = (os.environ.get("JARVIS_TEST_SHARD_MIN_UNCOVERED", "") or "").strip()
    if not raw:
        return 3
    try:
        v = int(raw)
        return v if v > 0 else 3
    except ValueError:
        return 3


def _test_shard_budget_base_s() -> float:
    """Base budget floor (seconds) for the decouple decision, scaled per route.
    Default 90. Env JARVIS_TEST_SHARD_BUDGET_BASE_S. Invalid/non-positive → default."""
    raw = (os.environ.get("JARVIS_TEST_SHARD_BUDGET_BASE_S", "") or "").strip()
    if not raw:
        return 90.0
    try:
        v = float(raw)
        return v if v > 0 else 90.0
    except ValueError:
        return 90.0


def _test_shard_budget_floor_s(provider_route: str) -> float:
    """The route-scaled remaining-budget floor below which test-gen is decoupled."""
    frac = _TEST_SHARD_ROUTE_FRACTIONS.get(
        (provider_route or "standard").strip().lower(), 0.67,
    )
    return _test_shard_budget_base_s() * frac


def should_decouple_test_gen(
    *, provider_route: str, remaining_s, uncovered_count: int,
    target_file_count: int = 1, complexity: str = "", enabled: bool = True,
) -> bool:
    """Pure adaptive decision: emit a SEPARATE background test-coverage op instead
    of inlining the test-gen instruction into the primary op? Decouple ONLY when
    enabled AND there are >2 uncovered files AND the budget can't sustain the load
    — the budget floor scales to the route, and heavy/multi-file ops decouple with
    more headroom. Light / ample ops → False (legacy inline inject, byte-identical).
    No env / IO reads of the breaker here — all inputs injected → deterministic +
    unit-testable. Pure; NEVER raises → fail-soft to False (inline)."""
    try:
        if not enabled:
            return False
        if int(uncovered_count) < _test_shard_min_uncovered():
            return False  # ≤2 uncovered → inline is cheap enough
        rem = float(remaining_s)
        floor = _test_shard_budget_floor_s(provider_route)
        if rem < floor:
            return True
        # Heavy / multi-file ops decouple with 1.5× headroom — their per-round
        # latency makes an inlined N-file test-gen especially deadline-risky.
        heavy = (complexity or "").strip().lower() in ("heavy_code", "complex")
        multifile = int(target_file_count) >= 5
        if (heavy or multifile) and rem < floor * 1.5:
            return True
        return False
    except Exception:  # noqa: BLE001 — fail-soft to inline
        return False


def build_test_coverage_envelope(
    *, uncovered_files, parent_op_id: str, repo: str = "jarvis",
    description: str = "",
):
    """Compile the ISOLATED, dedup-stable test-coverage payload as an
    IntentEnvelope (source=test_coverage, urgency=low, routing_override=background)
    via the existing ``make_envelope``. The dedup signature is the sorted
    uncovered set, so the SAME requirement re-emitted across parent retries
    collapses to one background op. NEVER raises here is NOT promised — the caller
    wraps the emit fail-soft (a bad envelope must not break the primary op)."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope as _make_envelope,
    )
    files = tuple(sorted({str(f) for f in (uncovered_files or ()) if str(f).strip()}))
    sig = "test_coverage:" + hashlib.sha256(
        "|".join(files).encode("utf-8"),
    ).hexdigest()[:16]
    desc = description or (
        f"Generate test coverage for {len(files)} uncovered file(s) decoupled from "
        f"patch op {str(parent_op_id)[:16]}: "
        f"{', '.join(files[:3])}{'…' if len(files) > 3 else ''}"
    )
    return _make_envelope(
        source="test_coverage",
        description=desc,
        target_files=files,
        repo=repo or "jarvis",
        confidence=0.9,
        urgency="low",
        evidence={
            "signature": sig,
            "uncovered_files": list(files),
            "parent_op_id": str(parent_op_id),
            "enforcer_reason": "budget_constraint",
        },
        requires_human_ack=False,
        routing_override="background",
    )


# ---------------------------------------------------------------------------
# 1. Test Coverage Enforcer (Pre-GENERATE)
# ---------------------------------------------------------------------------

class TestCoverageEnforcer:
    """Detects when target files lack test coverage and injects instructions.

    Called before GENERATE. Deterministic: checks whether test files exist
    for each target file using naming conventions. If zero tests found,
    appends instruction to the operation context so the provider generates
    tests alongside code.
    """

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    def detect_uncovered(self, target_files: Tuple[str, ...]) -> List[str]:
        """Pure detection (Slice 239): the source-of-truth list of target files
        that lack test coverage. Skips test files, non-Python files, and
        ``__init__``/``conftest``. Both the legacy inline injection
        (``check_and_inject``) and the decoupled sharding path read THIS, so the
        "which files need tests" decision can never drift. NEVER raises → []."""
        uncovered: List[str] = []
        try:
            for rel_path in target_files or ():
                if "test_" in rel_path or rel_path.endswith("_test.py"):
                    continue
                if not rel_path.endswith(".py"):
                    continue
                basename = Path(rel_path).stem
                if basename in ("__init__", "conftest"):
                    continue
                if not self._find_test_file(rel_path):
                    uncovered.append(rel_path)
        except Exception:  # noqa: BLE001 — detection must never break the pipeline
            return []
        return uncovered

    def check_and_inject(
        self,
        target_files: Tuple[str, ...],
        description: str,
    ) -> Optional[str]:
        """Check test coverage for target files.

        Returns an instruction string to inject into the generation prompt
        if any target files lack test coverage. Returns None if all files
        are covered or if target files are themselves tests.
        """
        uncovered = self.detect_uncovered(target_files)

        if not uncovered:
            return None

        files_list = ", ".join(f"`{f}`" for f in uncovered[:5])
        logger.info(
            "[TestCoverageEnforcer] %d target files lack test coverage: %s",
            len(uncovered), files_list,
        )

        return (
            f"\n\nIMPORTANT: The following target files have NO existing test coverage: "
            f"{files_list}. "
            f"In addition to the requested code changes, you MUST also generate "
            f"a corresponding test file for each uncovered module. Place tests in "
            f"the `tests/` directory following the naming convention `test_<module_name>.py`. "
            f"Include at minimum: import smoke test, key public method tests, and "
            f"edge case coverage. Use pytest conventions."
        )

    def _find_test_file(self, rel_path: str) -> bool:
        """Check if a test file exists for the given source file.

        Checks multiple conventions:
          - tests/test_<module>.py
          - tests/<package>/test_<module>.py
          - <same_dir>/test_<module>.py
        """
        p = Path(rel_path)
        module_name = p.stem
        parent = p.parent

        candidates = [
            self._project_root / "tests" / f"test_{module_name}.py",
            self._project_root / "tests" / parent / f"test_{module_name}.py",
            self._project_root / parent / f"test_{module_name}.py",
        ]

        return any(c.exists() for c in candidates)


# ---------------------------------------------------------------------------
# 2. Test Generation Hook (Post-GENERATE)
# ---------------------------------------------------------------------------

class TestGenerationHook:
    """Detects new file creation in candidates and flags for test generation.

    Called after GENERATE, before VALIDATE. If the candidate creates new
    Python modules (FileOp.CREATE), checks whether the candidate also
    includes corresponding test files. If not, returns an instruction
    for the retry/repair loop.
    """

    @staticmethod
    def check_candidate(candidate: Dict[str, Any]) -> Optional[str]:
        """Check if a candidate creates new files without tests.

        Returns a warning string if new modules lack tests, None otherwise.
        """
        # Single-file candidates
        file_path = candidate.get("file_path", "")
        if file_path and file_path.endswith(".py"):
            if "test_" not in file_path and "_test.py" not in file_path:
                # New file creation — check if a test was also generated
                # (In single-file schema, there's only one file, so no test)
                return (
                    f"New module `{file_path}` was generated without a "
                    f"corresponding test file. Consider adding test coverage."
                )

        # Multi-file candidates (schema 2c.1)
        patches = candidate.get("patches", {})
        if patches:
            new_modules = []
            test_files = []
            for repo_name, patch in patches.items():
                files = getattr(patch, "files", [])
                for pf in files:
                    path = getattr(pf, "path", "")
                    op = getattr(pf, "op", None)
                    if path.endswith(".py"):
                        if "test_" in path or "_test.py" in path:
                            test_files.append(path)
                        elif str(op) == "FileOp.CREATE" or str(op) == "CREATE":
                            new_modules.append(path)

            untested = [
                m for m in new_modules
                if not any(Path(m).stem in t for t in test_files)
            ]

            if untested:
                return (
                    f"New modules created without test coverage: "
                    f"{', '.join(f'`{m}`' for m in untested[:5])}. "
                    f"Consider adding tests in the next iteration."
                )

        return None


# ---------------------------------------------------------------------------
# 3. Semantic Review Gate (Pre-APPROVE)
# ---------------------------------------------------------------------------

class SemanticReviewGate:
    """Security and logic review gate before APPROVE.

    Deterministic: pattern-matching on file paths to decide IF review
    is needed. Agentic: the review CONTENT is generated by the model.

    Returns a review prompt when sensitive paths are modified, or None
    when no review is warranted.
    """

    def __init__(
        self,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled and os.environ.get(
            "JARVIS_SEMANTIC_REVIEW_ENABLED", "true"
        ).lower() in ("true", "1", "yes")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def should_review(self, target_files: Tuple[str, ...]) -> bool:
        """Determine if semantic review is needed. Pure deterministic."""
        if not self._enabled:
            return False

        for f in target_files:
            f_lower = f.lower()
            # Check explicit always-review paths
            for pattern in _REVIEW_ALWAYS_PATHS:
                if f.startswith(pattern) or f == pattern:
                    return True
            # Check security-sensitive keyword patterns
            for keyword in _SECURITY_SENSITIVE_PATTERNS:
                if keyword in f_lower:
                    return True

        return False

    def build_review_prompt(
        self,
        target_files: Tuple[str, ...],
        candidate_content: str,
        description: str,
    ) -> str:
        """Build a focused security/logic review prompt.

        The model receives the candidate code and is asked to review
        for specific categories of issues.
        """
        files_str = ", ".join(f"`{f}`" for f in target_files[:5])

        return (
            f"SECURITY AND LOGIC REVIEW\n"
            f"========================\n\n"
            f"The following code change is about to be applied to sensitive files: "
            f"{files_str}\n\n"
            f"Task description: {description}\n\n"
            f"Review the proposed change for:\n"
            f"1. INJECTION VULNERABILITIES — SQL injection, command injection, "
            f"path traversal, XSS, template injection\n"
            f"2. AUTHENTICATION BYPASS — missing auth checks, insecure token "
            f"handling, session fixation\n"
            f"3. LOGIC ERRORS — off-by-one, race conditions, null dereference, "
            f"infinite loops, unchecked error paths\n"
            f"4. SECRETS EXPOSURE — hardcoded credentials, API keys in source, "
            f"tokens logged to output\n"
            f"5. PRIVILEGE ESCALATION — operations that should require higher "
            f"permissions but don't check\n\n"
            f"Proposed code:\n```\n{candidate_content[:8000]}\n```\n\n"
            f"Return JSON: {{\"approved\": true/false, \"issues\": ["
            f"{{\"severity\": \"critical|high|medium|low\", "
            f"\"category\": \"...\", \"description\": \"...\", "
            f"\"line_hint\": \"...\"}}], "
            f"\"summary\": \"one-line verdict\"}}"
        )

    def parse_review_response(self, raw: str) -> Dict[str, Any]:
        """Parse the review model's response. Deterministic JSON extraction."""
        import json
        try:
            stripped = raw.strip()
            if stripped.startswith("```"):
                lines = stripped.split("\n")
                stripped = "\n".join(
                    line for line in lines if not line.startswith("```")
                ).strip()
            data = json.loads(stripped)
            return {
                "approved": data.get("approved", True),
                "issues": data.get("issues", []),
                "summary": data.get("summary", ""),
            }
        except (json.JSONDecodeError, ValueError):
            # Parse failure — conservatively approve (don't block pipeline)
            logger.debug("[SemanticReview] Failed to parse review response")
            return {"approved": True, "issues": [], "summary": "parse_error"}
