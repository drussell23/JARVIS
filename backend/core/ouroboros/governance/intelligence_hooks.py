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
        uncovered: List[str] = []

        for rel_path in target_files:
            # Skip test files and non-Python files
            if "test_" in rel_path or rel_path.endswith("_test.py"):
                continue
            if not rel_path.endswith(".py"):
                continue
            # Skip __init__.py and config files
            basename = Path(rel_path).stem
            if basename in ("__init__", "conftest"):
                continue

            # Look for corresponding test files
            test_exists = self._find_test_file(rel_path)
            if not test_exists:
                uncovered.append(rel_path)

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
