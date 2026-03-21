"""StructuredCritique — replaces flat error strings with actionable feedback.

Based on Self-Refine (Madaan et al., 2023): structured critique dramatically
improves the quality of iterative refinement compared to flat error messages.

Instead of: "ShadowHarness: output diverged from expected"
Produces:   StructuredCritique(
                file_path="parser.py",
                failure_type=CritiqueType.WRONG_RETURN_TYPE,
                what_failed="Function parse() returned list instead of generator",
                where="parser.py:47",
                observed="<class 'list'>",
                expected="<class 'generator'>",
                direction="Use 'yield' instead of building a list and returning it",
                severity=CritiqueSeverity.ERROR,
            )

The StructuredCritique is consumed by:
1. EpisodicFailureMemory (records structured data, not flat strings)
2. The retry GENERATE prompt (injects the critique as context)
3. The TUI dashboard (shows structured failure details)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class CritiqueType(str, Enum):
    """Classification of what kind of failure occurred."""
    SYNTAX_ERROR = "syntax_error"          # code doesn't parse
    IMPORT_ERROR = "import_error"          # missing or wrong import
    TYPE_ERROR = "type_error"              # wrong type returned/passed
    WRONG_RETURN_TYPE = "wrong_return_type"  # function returns wrong type
    ASSERTION_FAILURE = "assertion_failure"  # test assertion failed
    RUNTIME_ERROR = "runtime_error"        # exception during execution
    TIMEOUT = "timeout"                    # execution exceeded time limit
    SECURITY_VIOLATION = "security_violation"  # sandbox/permission violation
    BUILD_ERROR = "build_error"            # compilation/build failure
    LOGIC_ERROR = "logic_error"            # code runs but produces wrong result
    MISSING_IMPLEMENTATION = "missing_impl"  # function/class not implemented


class CritiqueSeverity(str, Enum):
    """How severe is this critique?"""
    ERROR = "error"        # must fix to proceed
    WARNING = "warning"    # should fix but not blocking
    INFO = "info"          # observation, not a failure


@dataclass(frozen=True)
class StructuredCritique:
    """A single structured critique of generated code."""
    file_path: str
    failure_type: CritiqueType
    what_failed: str                  # human-readable description of the failure
    where: str                        # "file.py:47" or "file.py:47-52"
    observed: str                     # what actually happened
    expected: str                     # what should have happened
    direction: str                    # hint toward fix (not the fix itself)
    severity: CritiqueSeverity = CritiqueSeverity.ERROR
    line_number: Optional[int] = None
    test_name: Optional[str] = None   # which test caught this (if applicable)


@dataclass(frozen=True)
class CritiqueReport:
    """Collection of structured critiques from a validation run."""
    critiques: Tuple[StructuredCritique, ...]
    summary: str                      # one-line summary for logs
    total_errors: int
    total_warnings: int
    all_passed: bool

    def format_for_prompt(self) -> str:
        """Format the critique report for injection into a retry generation prompt."""
        if self.all_passed:
            return "Previous validation PASSED — no critiques."

        lines = [
            f"## Validation Critique ({self.total_errors} error(s), {self.total_warnings} warning(s))",
            "",
        ]

        for c in self.critiques:
            lines.append(f"### [{c.severity.value.upper()}] {c.failure_type.value} at {c.where}")
            lines.append(f"What failed: {c.what_failed}")
            lines.append(f"Observed: {c.observed}")
            lines.append(f"Expected: {c.expected}")
            lines.append(f"Direction: {c.direction}")
            if c.test_name:
                lines.append(f"Caught by: {c.test_name}")
            lines.append("")

        lines.append(
            "Address ALL errors above. The 'direction' field tells you which "
            "way to go — do not blindly repeat the previous approach."
        )
        return "\n".join(lines)


class CritiqueBuilder:
    """Builds StructuredCritique objects from validation output.

    Parses pytest output, ShadowHarness errors, and build failures
    into structured critiques. Uses pattern matching — no LLM calls.
    """

    @classmethod
    def from_validation_output(
        cls,
        file_path: str,
        failure_class: str,
        error_text: str,
        test_output: str = "",
    ) -> CritiqueReport:
        """Parse validation output into a CritiqueReport."""
        critiques: List[StructuredCritique] = []

        if failure_class == "build":
            critiques.extend(cls._parse_build_errors(file_path, error_text))
        elif failure_class == "test":
            critiques.extend(cls._parse_test_failures(file_path, test_output or error_text))
        elif failure_class == "security":
            critiques.append(cls._build_security_critique(file_path, error_text))
        elif failure_class == "infra":
            critiques.append(cls._build_infra_critique(file_path, error_text))
        else:
            critiques.append(cls._build_generic_critique(file_path, failure_class, error_text))

        errors = sum(1 for c in critiques if c.severity == CritiqueSeverity.ERROR)
        warnings = sum(1 for c in critiques if c.severity == CritiqueSeverity.WARNING)

        return CritiqueReport(
            critiques=tuple(critiques),
            summary=f"{len(critiques)} critique(s): {errors} error(s), {warnings} warning(s)",
            total_errors=errors,
            total_warnings=warnings,
            all_passed=errors == 0 and warnings == 0,
        )

    @classmethod
    def _parse_build_errors(cls, file_path: str, error_text: str) -> List[StructuredCritique]:
        """Parse SyntaxError, ImportError, etc. from build/compile output."""
        critiques = []

        # SyntaxError pattern: "File "xxx.py", line N"
        syntax_matches = re.finditer(
            r'File "([^"]+)", line (\d+).*?\n\s*(SyntaxError: .+)', error_text, re.DOTALL
        )
        for m in syntax_matches:
            critiques.append(StructuredCritique(
                file_path=m.group(1) or file_path,
                failure_type=CritiqueType.SYNTAX_ERROR,
                what_failed=m.group(3).strip(),
                where=f"{m.group(1)}:{m.group(2)}",
                observed=m.group(3).strip(),
                expected="Valid Python syntax",
                direction="Check for missing colons, parentheses, or indentation errors",
                line_number=int(m.group(2)),
            ))

        # ImportError pattern
        import_matches = re.finditer(
            r"(ImportError|ModuleNotFoundError): (.+)", error_text
        )
        for m in import_matches:
            critiques.append(StructuredCritique(
                file_path=file_path,
                failure_type=CritiqueType.IMPORT_ERROR,
                what_failed=m.group(0),
                where=file_path,
                observed=m.group(2),
                expected="Successful import",
                direction=f"Check that module '{m.group(2).split()[-1].strip(chr(39))}' exists or install it",
            ))

        if not critiques:
            critiques.append(cls._build_generic_critique(file_path, "build", error_text))

        return critiques

    @classmethod
    def _parse_test_failures(cls, file_path: str, test_output: str) -> List[StructuredCritique]:
        """Parse pytest failure output into structured critiques."""
        critiques = []

        # pytest FAILED pattern: "FAILED test_file.py::test_name - AssertionError: ..."
        failed_matches = re.finditer(
            r"FAILED\s+(\S+)::(\S+)\s*[-—]\s*(.+?)(?:\n|$)", test_output
        )
        for m in failed_matches:
            test_file = m.group(1)
            test_name = m.group(2)
            error_msg = m.group(3).strip()

            failure_type = CritiqueType.ASSERTION_FAILURE
            if "TypeError" in error_msg:
                failure_type = CritiqueType.TYPE_ERROR
            elif "ImportError" in error_msg or "ModuleNotFoundError" in error_msg:
                failure_type = CritiqueType.IMPORT_ERROR
            elif "RuntimeError" in error_msg:
                failure_type = CritiqueType.RUNTIME_ERROR

            critiques.append(StructuredCritique(
                file_path=test_file,
                failure_type=failure_type,
                what_failed=error_msg,
                where=f"{test_file}::{test_name}",
                observed=error_msg,
                expected="Test passes",
                direction=cls._infer_direction(failure_type, error_msg),
                test_name=test_name,
            ))

        # AssertionError with "assert X == Y" pattern
        assert_matches = re.finditer(
            r"assert\s+(.+?)\s*==\s*(.+?)(?:\n|$)", test_output
        )
        for m in assert_matches:
            if not any(c.test_name and m.group(0) in str(c.observed) for c in critiques):
                critiques.append(StructuredCritique(
                    file_path=file_path,
                    failure_type=CritiqueType.ASSERTION_FAILURE,
                    what_failed=f"Assertion failed: {m.group(0).strip()}",
                    where=file_path,
                    observed=m.group(1).strip(),
                    expected=m.group(2).strip(),
                    direction="Check the return value matches the expected output",
                ))

        if not critiques:
            critiques.append(cls._build_generic_critique(file_path, "test", test_output))

        return critiques

    @classmethod
    def _build_security_critique(cls, file_path: str, error_text: str) -> StructuredCritique:
        return StructuredCritique(
            file_path=file_path,
            failure_type=CritiqueType.SECURITY_VIOLATION,
            what_failed=error_text[:300],
            where=file_path,
            observed="Security policy violation",
            expected="Code within security boundaries",
            direction="Remove filesystem writes, subprocess calls, or network access outside allowlist",
            severity=CritiqueSeverity.ERROR,
        )

    @classmethod
    def _build_infra_critique(cls, file_path: str, error_text: str) -> StructuredCritique:
        return StructuredCritique(
            file_path=file_path,
            failure_type=CritiqueType.RUNTIME_ERROR,
            what_failed=error_text[:300],
            where=file_path,
            observed="Infrastructure failure",
            expected="Successful execution",
            direction="This may be a transient infrastructure issue — retry may help",
            severity=CritiqueSeverity.ERROR,
        )

    @classmethod
    def _build_generic_critique(cls, file_path: str, failure_class: str, error_text: str) -> StructuredCritique:
        return StructuredCritique(
            file_path=file_path,
            failure_type=CritiqueType.LOGIC_ERROR,
            what_failed=error_text[:300],
            where=file_path,
            observed=error_text[:200],
            expected="Successful validation",
            direction="Review the error message and adjust the implementation accordingly",
            severity=CritiqueSeverity.ERROR,
        )

    @staticmethod
    def _infer_direction(failure_type: CritiqueType, error_msg: str) -> str:
        """Infer a directional hint from the failure type and error message."""
        if failure_type == CritiqueType.TYPE_ERROR:
            return "Check argument types and return types match the expected signatures"
        if failure_type == CritiqueType.IMPORT_ERROR:
            module = error_msg.split("'")[-2] if "'" in error_msg else "the module"
            return f"Ensure {module} is importable — check spelling, installation, and sys.path"
        if failure_type == CritiqueType.ASSERTION_FAILURE:
            return "The function's return value doesn't match the test expectation — trace the logic"
        if failure_type == CritiqueType.RUNTIME_ERROR:
            return "An exception was raised during execution — add error handling or fix the root cause"
        return "Review the error and adjust the implementation"
