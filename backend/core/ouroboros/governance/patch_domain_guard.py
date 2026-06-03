"""Slice 78 Track 1 — Iron Gate source-domain purity guard.

A SWE-bench-class failure mode: the model "resolves" a bug by editing the TEST
files (the held-out fail_to_pass / pass_to_pass suite) instead of the actual
source defect. ``SCORE_REJECT_TEST_MODS`` already rejects this — but only at
SCORING time, after the op has burned its full generation + APPLY budget. This
guard catches it EARLY (post-GENERATE, pre-APPLY), mirroring the exploration /
ASCII Iron Gates: a test-only candidate is short-circuited back to
GENERATE_RETRY with corrective feedback telling the model to fix the source.

Design (verify-first): swe_bench candidates carry ``full_content`` per file, not
diffs, so the authoritative pre-apply signal is the candidate's MODIFIED FILE
PATHS. ``extract_modified_paths`` additionally parses unified-diff ``+++`` headers
for diff-shaped inputs (e.g. a produced patch). The Slice 69 ``target_paths``
(the test_patch footprint) are treated as authoritative test files even when they
don't match a naming convention.

Authority discipline: this module is a pure deterministic classifier. It imports
NO orchestrator / policy / change_engine substrates — it only inspects strings.
Every public surface NEVER raises (fail-open: a guard error must not block the
pipeline). Master flag ``JARVIS_PATCH_DOMAIN_GUARD_ENABLED`` (default TRUE).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

PATCH_DOMAIN_GUARD_ENABLED_ENV_VAR: str = "JARVIS_PATCH_DOMAIN_GUARD_ENABLED"

_TRUTHY = ("1", "true", "yes", "on")

# Test-path heuristics — conventional across Python / JS / TS / Go / Ruby / etc.
# A path is a test file if ANY of these match (case-insensitive on the basename
# segments). Deliberately conservative: only well-established test conventions,
# so a source file is never misclassified as a test.
_TEST_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"(^|/)tests?/"),          # tests/  or  test/
    re.compile(r"(^|/)__tests__/"),       # JS __tests__/
    re.compile(r"(^|/)spec/"),            # ruby/js spec/
    re.compile(r"(^|/)test_[^/]+$"),      # test_foo.py
    re.compile(r"_test\.[a-z0-9]+$"),     # foo_test.go / foo_test.py
    re.compile(r"\.test\.[a-z0-9]+$"),    # foo.test.ts / foo.test.js
    re.compile(r"-test\.[a-z0-9]+$"),     # Markdown-test.ts
    re.compile(r"_spec\.[a-z0-9]+$"),     # foo_spec.rb
    re.compile(r"\.spec\.[a-z0-9]+$"),    # foo.spec.ts
    re.compile(r"(^|/)conftest\.py$"),    # pytest conftest
)


def is_test_path(path: str) -> bool:
    """True iff *path* is a test file by established naming convention. Pure;
    NEVER raises (a non-string / empty path → False)."""
    try:
        p = str(path).strip().replace("\\", "/")
        if not p:
            return False
        low = p.lower()
        return any(pat.search(low) for pat in _TEST_PATTERNS)
    except Exception:  # noqa: BLE001
        return False


def extract_modified_paths(patch_str: str) -> List[str]:
    """Extract modified file paths from a unified diff. Reads ``+++ b/<path>``
    headers (falling back to ``--- a/<path>`` for deletions), strips the a//b/
    prefixes, and skips ``/dev/null``. Order-preserving dedup. NEVER raises."""
    out: List[str] = []
    seen = set()
    try:
        for line in str(patch_str).splitlines():
            # Read both the new-path (+++ b/…) and old-path (--- a/…) headers;
            # order-preserving dedup collapses the pair for a normal hunk and
            # /dev/null is skipped, so a new file resolves to its +++ path and a
            # deletion to its --- path. Either way we capture every touched file.
            if line.startswith("+++ ") or line.startswith("--- "):
                tgt: Optional[str] = line[4:].strip()
            else:
                continue
            # strip trailing tab-timestamp some diffs carry
            tgt = tgt.split("\t", 1)[0].strip()
            if tgt in ("/dev/null", ""):
                continue
            for prefix in ("a/", "b/"):
                if tgt.startswith(prefix):
                    tgt = tgt[len(prefix):]
                    break
            if tgt and tgt not in seen:
                seen.add(tgt)
                out.append(tgt)
    except Exception:  # noqa: BLE001
        return out
    return out


@dataclass(frozen=True)
class PurityVerdict:
    """Result of the source-domain purity check."""

    modified_files: Tuple[str, ...] = ()
    test_files: Tuple[str, ...] = ()
    source_files: Tuple[str, ...] = ()

    @property
    def test_only(self) -> bool:
        """True iff the candidate modifies at least one file AND every modified
        file is a test file (the cheat-the-suite signature)."""
        return bool(self.modified_files) and not self.source_files

    @property
    def is_pure(self) -> bool:
        """True iff the candidate touches at least one source file (or is
        empty — an empty patch is judged by other gates, not this one)."""
        return not self.test_only


def verify_patch_domain_purity(
    modified_paths: Sequence[str],
    test_target_paths: Optional[Sequence[str]] = None,
) -> PurityVerdict:
    """Classify *modified_paths* into test vs source. A path is a test file if it
    matches a test naming convention OR is in *test_target_paths* (the Slice 69
    test_patch footprint — authoritative). Pure; NEVER raises."""
    try:
        targets = {str(p).replace("\\", "/") for p in (test_target_paths or [])}
        mods = [str(p).replace("\\", "/") for p in (modified_paths or []) if str(p).strip()]
        tests: List[str] = []
        sources: List[str] = []
        for p in mods:
            if p in targets or is_test_path(p):
                tests.append(p)
            else:
                sources.append(p)
        return PurityVerdict(
            modified_files=tuple(mods),
            test_files=tuple(tests),
            source_files=tuple(sources),
        )
    except Exception:  # noqa: BLE001 — fail-open to an empty (pure) verdict
        return PurityVerdict()


def build_retry_feedback(verdict: PurityVerdict) -> str:
    """The corrective GENERATE_RETRY message for a test-only candidate."""
    files = ", ".join(verdict.test_files) or "the test suite"
    return (
        "Validation Failure: Proposed patch modifies test files exclusively "
        f"({files}). You must isolate and resolve the core logic defect inside "
        "the SOURCE domain code, not the testing suite infrastructure. Do NOT "
        "edit the held-out tests — explore the source tree and fix the bug at "
        "its origin."
    )


class PatchDomainGuard:
    """Iron-Gate wrapper mirroring ``AsciiStrictGate`` — a thin, env-gated
    façade over the pure :func:`verify_patch_domain_purity` classifier."""

    def __init__(self) -> None:
        self._enabled = (
            os.environ.get(PATCH_DOMAIN_GUARD_ENABLED_ENV_VAR, "true")
            .strip().lower() in _TRUTHY
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def check(
        self,
        modified_paths: Sequence[str],
        test_target_paths: Optional[Sequence[str]] = None,
    ) -> Tuple[bool, str, PurityVerdict]:
        """Returns ``(ok, reason, verdict)``. ``ok=False`` ONLY for an enabled
        guard on a test-only candidate. NEVER raises (fail-open → ``ok=True``)."""
        try:
            if not self._enabled:
                return True, "", PurityVerdict()
            verdict = verify_patch_domain_purity(modified_paths, test_target_paths)
            if verdict.test_only:
                return False, build_retry_feedback(verdict), verdict
            return True, "", verdict
        except Exception:  # noqa: BLE001 — never block the pipeline
            return True, "", PurityVerdict()


__all__ = [
    "PATCH_DOMAIN_GUARD_ENABLED_ENV_VAR",
    "is_test_path",
    "extract_modified_paths",
    "PurityVerdict",
    "verify_patch_domain_purity",
    "build_retry_feedback",
    "PatchDomainGuard",
]
