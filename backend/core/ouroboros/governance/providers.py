"""
Provider Adapters for Governed Code Generation
================================================

Wraps existing PrimeClient and Claude API into CandidateProvider protocol
implementations for use with the CandidateGenerator's failback state machine.

Components
----------
- ``_build_codegen_prompt``: builds structured prompt from OperationContext
- ``_parse_generation_response``: strict JSON schema parser for model output
- ``PrimeProvider``: wraps PrimeClient.generate()
- ``ClaudeProvider``: wraps anthropic.AsyncAnthropic (cost-gated)
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

try:
    from backend.core.prime_client import TaskProfile as _TaskProfile
except ImportError:
    _TaskProfile = None  # type: ignore[assignment,misc]

logger = logging.getLogger("Ouroboros.Providers")


# ---------------------------------------------------------------------------
# Shared: Prompt Builder
# ---------------------------------------------------------------------------

_CODEGEN_SYSTEM_PROMPT = (
    "You are a precise code modification assistant for the JARVIS multi-repo ecosystem. "
    "For single-repo requests respond with schema_version 2b.1. "
    "For cross-repo requests (where the prompt specifies schema_version 2c.1) "
    "respond with schema_version 2c.1 and a patches dict keyed by repo name. "
    "You MUST respond with valid JSON only. "
    "No markdown preamble, no explanations outside the JSON. Only the JSON object. "
    # Diff-anchoring mandate — critical for small models that default to trained memory
    "DIFF ANCHORING RULES: When generating unified_diff output, context lines MUST be "
    "verbatim copies of lines from the ## Source Snapshot provided in the prompt. "
    "Do NOT reconstruct context lines from your training data or memory of what the "
    "file 'should' contain. Copy them exactly from the provided source. "
    "If the requested change is already present in the source file, return "
    '{"schema_version": "2b.1-noop", "reason": "<why already done>"} instead of a diff.'
    + (
        " " + os.environ["JARVIS_CODEGEN_SYSTEM_PROMPT_EXTRA"]
        if os.environ.get("JARVIS_CODEGEN_SYSTEM_PROMPT_EXTRA")
        else ""
    )
)

# ── Phase 2B: size/security constants ────────────────────────────────────
_MAX_TARGET_FILE_CHARS = 12000     # full content below this (increased from 6000)
_TARGET_FILE_HEAD_CHARS = 8000     # head kept on truncation
_TARGET_FILE_TAIL_CHARS = 2000     # tail kept on truncation
_MAX_IMPORT_CONTEXT_CHARS = 1500   # total across all discovered import files
_MAX_TEST_CONTEXT_CHARS = 1500     # total across all discovered test files
_MAX_IMPORT_FILES = 5              # hard cap on discovered import sources
_MAX_TEST_FILES = 2                # hard cap on discovered test files
_SCHEMA_VERSION = "2b.1"
_SCHEMA_VERSION_MULTI = "2c.1"
_SCHEMA_VERSION_DIFF = "2b.1-diff"   # Task 4: unified-diff output for single-file tasks
_SCHEMA_TOP_LEVEL_KEYS = frozenset({"schema_version", "candidates", "provider_metadata"})
_CANDIDATE_KEYS = frozenset({"candidate_id", "file_path", "full_content", "rationale"})
_DIFF_CANDIDATE_KEYS = frozenset({"candidate_id", "file_path", "unified_diff", "rationale"})

# ── Tool-use interface ────────────────────────────────────────────────
_TOOL_SCHEMA_VERSION = "2b.2-tool"
MAX_TOOL_ITERATIONS  = 5
MAX_TOOL_LOOP_CHARS  = 32_000   # hard accumulated-prompt budget


def _safe_context_path(repo_root: Path, target: Path) -> Path:
    """Resolve target path and verify it stays within repo_root.

    Raises BlockedPathError if the resolved path is outside repo_root
    or if the path is a symlink.
    """
    from backend.core.ouroboros.governance.test_runner import BlockedPathError
    # Check for symlink before resolving (resolve() follows symlinks)
    if target.is_symlink():
        raise BlockedPathError(f"Symlink not allowed in context discovery: {target}")
    resolved = target.resolve()
    repo_resolved = repo_root.resolve()
    if not str(resolved).startswith(str(repo_resolved) + "/") and resolved != repo_resolved:
        raise BlockedPathError(f"Context file outside repo root: {target}")
    return resolved


def _read_with_truncation(path: Path, max_chars: int = _MAX_TARGET_FILE_CHARS) -> str:
    """Read file content, applying truncation with an explicit marker if needed."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(content) <= max_chars:
        return content
    head = content[:_TARGET_FILE_HEAD_CHARS]
    tail = content[-_TARGET_FILE_TAIL_CHARS:]
    omitted_bytes = len(content.encode()) - len(head.encode()) - len(tail.encode())
    omitted_lines = content.count("\n") - head.count("\n") - tail.count("\n")
    marker = f"\n[TRUNCATED: {omitted_bytes} bytes, {omitted_lines} lines omitted]\n"
    return head + marker + tail


def _file_source_hash(content: str) -> str:
    """Return hex SHA-256 of file content."""
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Disease 1 Fix: StaleDiffError + validate_diff_context + is_change_needed
# ---------------------------------------------------------------------------

class StaleDiffError(ValueError):
    """Raised when a diff's context lines don't match the actual file content.

    Attributes
    ----------
    hunk_line:
        1-based line number where the mismatch was detected.
    expected_context:
        The context lines the diff expected to find.
    actual_lines:
        What the file actually contains at that position.
    """

    def __init__(
        self,
        message: str,
        *,
        hunk_line: int,
        expected_context: List[str],
        actual_lines: List[str],
    ) -> None:
        super().__init__(message)
        self.hunk_line = hunk_line
        self.expected_context = expected_context
        self.actual_lines = actual_lines


def validate_diff_context(original: str, diff_text: str) -> None:
    """Pre-apply validation gate: verify every hunk's context lines are
    verbatim substrings of *original* BEFORE any file mutation.

    This is a pure read operation — it never writes to disk.

    Raises
    ------
    StaleDiffError
        If any hunk's context lines cannot be located in *original*
        (indicating the model generated against a stale or hallucinated
        version of the file).
    """
    _hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    orig_lines = original.splitlines(keepends=True)

    def _norm(lines: List[str]) -> List[str]:
        return [ln.rstrip("\n\r") for ln in lines]

    diff_lines = diff_text.splitlines(keepends=True)
    i = 0
    # Skip --- / +++ header
    while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
        i += 1

    while i < len(diff_lines):
        m = _hunk_re.match(diff_lines[i])
        if m is None:
            i += 1
            continue

        orig_start = int(m.group(1)) - 1  # 0-indexed
        i += 1

        # Collect context + removed lines (the "original" side of the hunk)
        hunk_orig: List[str] = []
        while i < len(diff_lines) and not _hunk_re.match(diff_lines[i]):
            line = diff_lines[i]
            if line.startswith("-") or line.startswith(" "):
                hunk_orig.append(line[1:])
            i += 1

        if not hunk_orig:
            continue

        hunk_len = len(hunk_orig)
        norm_hunk = _norm(hunk_orig)

        # Exact match first
        actual = orig_lines[orig_start:orig_start + hunk_len]
        if _norm(actual) == norm_hunk:
            continue

        # Bounded fuzzy search (±5 lines) to tolerate minor off-by-N from LLM
        window = int(os.environ.get("OUROBOROS_DIFF_FUZZY_WINDOW", "5"))
        lo = max(0, orig_start - window)
        hi = min(len(orig_lines) - hunk_len + 1, orig_start + window + 1)
        found = -1
        for candidate in range(lo, hi):
            if _norm(orig_lines[candidate:candidate + hunk_len]) == norm_hunk:
                found = candidate
                break

        if found == -1:
            raise StaleDiffError(
                f"Diff hunk at line {orig_start + 1} does not match source — "
                f"model likely generated against stale/hallucinated content. "
                f"Expected context: {hunk_orig[:2]!r}, "
                f"got: {orig_lines[orig_start:orig_start + 2]!r}. "
                f"Searched ±{window} lines with no match.",
                hunk_line=orig_start + 1,
                expected_context=hunk_orig,
                actual_lines=orig_lines[orig_start:orig_start + hunk_len],
            )


def is_change_needed(file_path: Path, sentinel: str) -> bool:
    """Return True if *sentinel* (an exact line) is NOT already present in *file_path*.

    Used as a pre-generation idempotency guard: if the change is already present
    we return a no-op GenerationResult without calling any model.

    Comparison is line-exact (stripped of trailing whitespace).  A substring
    match inside a longer line does NOT count — the sentinel must appear as a
    standalone line.

    Parameters
    ----------
    file_path:
        Absolute or relative path to the file to inspect.
    sentinel:
        The exact line to search for (without trailing newline).
    """
    if not file_path.exists():
        return True  # File doesn't exist → change definitely needed (create)
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True  # Can't read → treat as needed
    sentinel_stripped = sentinel.strip()
    for line in content.splitlines():
        if line.strip() == sentinel_stripped:
            return False  # Exact line match found → no change needed
    return True


def _apply_unified_diff(original: str, diff_text: str) -> str:
    """Apply a unified diff to *original*, returning patched content.

    Supports standard GNU unified-diff format:
      @@ -start[,count] +start[,count] @@
      ' ' context line
      '-' removed line
      '+' added line

    Hunks are applied in reverse order so earlier-hunk indices remain valid
    after later-hunk edits.

    Raises
    ------
    ValueError
        If a hunk's context lines do not match the original at the expected
        position, indicating a stale or malformed diff.
    """
    orig_lines = original.splitlines(keepends=True)
    result: List[str] = list(orig_lines)

    _hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    diff_lines = diff_text.splitlines(keepends=True)

    # Skip --- / +++ header lines
    i = 0
    while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
        i += 1

    hunks: List[Tuple[int, List[str], List[str]]] = []
    while i < len(diff_lines):
        m = _hunk_re.match(diff_lines[i])
        if m is None:
            i += 1
            continue

        orig_start = int(m.group(1)) - 1  # 0-indexed
        i += 1

        hunk_orig: List[str] = []
        hunk_new: List[str] = []
        while i < len(diff_lines) and not _hunk_re.match(diff_lines[i]):
            line = diff_lines[i]
            if line.startswith("-"):
                hunk_orig.append(line[1:])
            elif line.startswith("+"):
                hunk_new.append(line[1:])
            elif line.startswith(" "):
                hunk_orig.append(line[1:])
                hunk_new.append(line[1:])
            # Ignore "\\ No newline at end of file" and stray lines
            i += 1

        hunks.append((orig_start, hunk_orig, hunk_new))

    def _normalize(lines: List[str]) -> List[str]:
        return [ln.rstrip("\n\r") for ln in lines]

    def _find_hunk_start(result: List[str], orig_start: int, hunk_orig: List[str], window: int = 3) -> int:
        """Search for hunk_orig within a ±window line window of orig_start.

        Returns the best matching start index, or -1 if not found.
        This tolerates off-by-N line numbers that LLMs commonly generate.
        """
        norm_hunk = _normalize(hunk_orig)
        hunk_len = len(hunk_orig)
        lo = max(0, orig_start - window)
        hi = min(len(result) - hunk_len + 1, orig_start + window + 1)
        for candidate in range(lo, hi):
            if _normalize(result[candidate:candidate + hunk_len]) == norm_hunk:
                return candidate
        return -1

    # Apply hunks bottom-to-top so earlier indices stay valid
    for orig_start, hunk_orig, hunk_new in reversed(hunks):
        end = orig_start + len(hunk_orig)
        actual = result[orig_start:end]
        # Normalise line endings for comparison only
        if _normalize(actual) != _normalize(hunk_orig):
            # Exact match failed — try fuzzy search within ±3 lines (LLMs commonly
            # generate diffs with off-by-1 or off-by-2 line numbers)
            found = _find_hunk_start(result, orig_start, hunk_orig, window=3)
            if found == -1:
                raise ValueError(
                    f"Diff hunk at line {orig_start + 1} does not match source — "
                    f"expected {hunk_orig[:2]!r}, got {actual[:2]!r}"
                )
            orig_start = found
            end = orig_start + len(hunk_orig)
        result[orig_start:end] = hunk_new

    return "".join(result)


def _find_context_files(
    target_file: Path,
    repo_root: Path,
) -> Tuple[List[Path], List[Path]]:
    """Discover import sources and test files related to target_file.

    Returns (import_files, test_files) — each capped by hard limits.
    All returned paths are safe (within repo_root, no symlinks).
    """
    from backend.core.ouroboros.governance.test_runner import BlockedPathError

    import_files: List[Path] = []
    test_files: List[Path] = []

    # -- Import context: scan first 60 lines for import statements --------
    try:
        lines = target_file.read_text(encoding="utf-8", errors="replace").splitlines()[:60]
    except OSError:
        lines = []

    import_pattern = re.compile(r"^\s*(?:from|import)\s+([\w.]+)")
    for line in lines:
        if len(import_files) >= _MAX_IMPORT_FILES:
            break
        m = import_pattern.match(line)
        if not m:
            continue
        module_name = m.group(1).split(".")[0]
        # Look for module as a .py file in repo
        candidate = repo_root / f"{module_name}.py"
        if not candidate.exists():
            # Try subdirectory package
            candidate = repo_root / module_name / "__init__.py"
        if not candidate.exists():
            continue
        try:
            safe = _safe_context_path(repo_root, candidate)
            if safe not in import_files:
                import_files.append(safe)
        except BlockedPathError:
            continue

    # -- Test context: find test_*.py that mentions target module name ----
    target_stem = target_file.stem
    tests_dir = repo_root / "tests"
    if tests_dir.is_dir():
        for test_file in sorted(tests_dir.rglob("test_*.py")):
            if len(test_files) >= _MAX_TEST_FILES:
                break
            try:
                text = test_file.read_text(encoding="utf-8", errors="replace")
                if target_stem in text:
                    safe = _safe_context_path(repo_root, test_file)
                    test_files.append(safe)
            except (OSError, Exception):
                continue

    return import_files, test_files


def _build_system_context_block(ctx: "OperationContext") -> Optional[str]:
    """Build '## System Context' block from ctx.telemetry, or return None.

    Returns None (silently omitted) when telemetry is not set —
    zero behavior change for existing tests and callers.
    """
    tc = ctx.telemetry
    if tc is None:
        return None
    h = tc.local_node
    ri = tc.routing_intent
    lines = [
        "## System Context",
        (
            f"Host  : {h.arch} | CPU: {h.cpu_percent:.2f}% "
            f"| RAM: {h.ram_available_gb:.2f} GB avail | Pressure: {h.pressure}"
        ),
        f"Sample: {h.sampled_at_utc} | Age: {h.sample_age_ms}ms | Status: {h.collector_status}",
        f"Route : {ri.expected_provider} | Reason: {ri.policy_reason}",
    ]
    if tc.routing_actual is not None:
        ra = tc.routing_actual
        lines.append(
            f"Actual: {ra.provider_name} ({ra.endpoint_class}) | Degraded: {ra.was_degraded}"
        )
    return "\n".join(lines)


def _build_tool_section() -> str:
    """Return the 'Available Tools' block injected into the generation prompt."""
    return (
        "## Available Tools\n\n"
        "If you need more information before writing the patch, respond with ONLY a\n"
        "tool_call JSON (no other text):\n\n"
        "```json\n"
        "{\n"
        f'  "schema_version": "{_TOOL_SCHEMA_VERSION}",\n'
        '  "tool_call": {\n'
        '    "name": "<tool_name>",\n'
        '    "arguments": {...}\n'
        "  }\n"
        "}\n"
        "```\n\n"
        "Available tools:\n"
        '- `search_code(pattern, file_glob="*.py")` — search the codebase with a regex pattern\n'
        "- `read_file(path, lines_from=1, lines_to=200)` — read file content (repo-relative path)\n"
        "- `list_symbols(module_path)` — list functions and classes in a Python file\n"
        "- `run_tests(paths)` — run pytest for the given test paths (list of strings), returns summary\n"
        "- `get_callers(function_name, file_path=None)` — find call sites of a function\n\n"
        f"Max {MAX_TOOL_ITERATIONS} tool calls total. After gathering info, respond with the patch JSON."
    )


def _build_codegen_prompt(
    ctx: "OperationContext",
    repo_root: Optional[Path] = None,
    repo_roots: Optional[Dict[str, Path]] = None,
    tools_enabled: bool = False,
    max_prompt_tokens: Optional[int] = None,
    force_full_content: bool = False,
) -> str:
    """Build an enriched codegen prompt with file contents, context, and schema.

    Reads each target file from disk, hashes it, applies truncation, discovers
    surrounding import/test context (capped), and injects the appropriate output
    schema specification: schema_version 2b.1 for single-repo operations and
    schema_version 2c.1 for cross-repo operations.

    Parameters
    ----------
    ctx:
        The operation context describing target files, description, and repo scope.
    repo_root:
        Root path for single-repo operations. Defaults to cwd if not provided.
    repo_roots:
        Mapping of repo name -> root path for cross-repo operations. When
        provided alongside a cross-repo ctx, each file section is labelled with
        the repo it belongs to and the 2c.1 schema is emitted.
    force_full_content:
        When True, always use schema 2b.1 (full_content) even for single-file
        tasks, bypassing the 2b.1-diff (unified diff) path.  Required for
        smaller models (≤13B) that lack the precision to generate verbatim
        context lines in unified diffs — they reconstruct from parametric
        memory instead of copying from the in-context source snapshot.
    """
    from backend.core.ouroboros.governance.test_runner import BlockedPathError

    if repo_root is None:
        repo_root = Path.cwd()

    # ── 1. Build source snapshot for each target file ──────────────────
    file_sections: List[str] = []
    for raw_path in ctx.target_files:
        # Determine which repo root governs this file and resolve label
        repo_label: Optional[str] = None
        effective_root = repo_root
        if ctx.cross_repo and repo_roots:
            abs_raw = Path(raw_path)
            for rname, rroot in repo_roots.items():
                try:
                    abs_raw.relative_to(rroot)
                    repo_label = rname
                    effective_root = rroot
                    break
                except ValueError:
                    continue
            # Fall back to absolute path resolution against each root
            if repo_label is None:
                for rname, rroot in repo_roots.items():
                    candidate = (rroot / raw_path).resolve()
                    try:
                        candidate.relative_to(rroot.resolve())
                        repo_label = rname
                        effective_root = rroot
                        break
                    except ValueError:
                        continue

        abs_path = Path(raw_path) if Path(raw_path).is_absolute() else (effective_root / raw_path).resolve()
        try:
            abs_path = _safe_context_path(effective_root, abs_path)
        except BlockedPathError as exc:
            file_sections.append(f"## File: {raw_path}\n[BLOCKED: {exc}]\n")
            continue

        content = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else ""
        source_hash = _file_source_hash(content)
        size_bytes = len(content.encode())
        line_count = content.count("\n")
        truncated = _read_with_truncation(abs_path)

        # Build the section header — include [repo_name] label for cross-repo ops
        if repo_label is not None:
            header = (
                f"## File: {raw_path} [{repo_label}] [SHA-256: {source_hash[:12]}]"
                f" [{size_bytes} bytes, {line_count} lines]"
            )
        else:
            header = (
                f"## File: {raw_path} [SHA-256: {source_hash[:12]}]"
                f" [{size_bytes} bytes, {line_count} lines]"
            )

        file_sections.append(f"{header}\n```\n{truncated}\n```")

    # ── 2. Discover surrounding context (import sources + tests) ────────
    context_parts: List[str] = []
    if ctx.target_files:
        primary = (repo_root / ctx.target_files[0]).resolve()
        try:
            primary = _safe_context_path(repo_root, primary)
            import_files, test_files = _find_context_files(primary, repo_root)
        except BlockedPathError:
            import_files, test_files = [], []

        import_budget = _MAX_IMPORT_CONTEXT_CHARS
        for ifile in import_files:
            try:
                text = ifile.read_text(encoding="utf-8", errors="replace")
                snippet = "\n".join(text.splitlines()[:30])[:import_budget]
                rel = ifile.relative_to(repo_root)
                context_parts.append(f"### Import source: {rel}\n```\n{snippet}\n```")
                import_budget -= len(snippet)
                if import_budget <= 0:
                    break
            except OSError:
                continue

        test_budget = _MAX_TEST_CONTEXT_CHARS
        for tfile in test_files:
            try:
                text = tfile.read_text(encoding="utf-8", errors="replace")
                snippet = "\n".join(text.splitlines()[:50])[:test_budget]
                rel = tfile.relative_to(repo_root)
                context_parts.append(f"### Test context: {rel}\n```\n{snippet}\n```")
                test_budget -= len(snippet)
                if test_budget <= 0:
                    break
            except OSError:
                continue

    context_block = (
        "## Surrounding Context (read-only — do not modify)\n\n"
        + ("\n\n".join(context_parts) if context_parts else "_No surrounding context discovered._")
    )

    # ── 2b. Expanded context files (pre-generation context expansion result) ──
    expanded_context_parts: List[str] = []
    for raw_exp in getattr(ctx, "expanded_context_files", ()):
        abs_exp = Path(raw_exp) if Path(raw_exp).is_absolute() else (repo_root / raw_exp).resolve()
        try:
            abs_exp = _safe_context_path(repo_root, abs_exp)
        except BlockedPathError:
            continue
        exp_content = _read_with_truncation(abs_exp, max_chars=_MAX_TARGET_FILE_CHARS)
        if not exp_content:
            continue
        expanded_context_parts.append(
            f"### Expanded context: {raw_exp} [CONTEXT ONLY — DO NOT MODIFY]\n```\n{exp_content}\n```"
        )
    expanded_context_block = ""
    if expanded_context_parts:
        expanded_context_block = (
            "## Expanded Context Files (CONTEXT ONLY — DO NOT MODIFY)\n\n"
            + "\n\n".join(expanded_context_parts)
        )

    # ── 3. Output schema instruction ────────────────────────────────────
    # force_full_content disables the diff schema — smaller models (≤13B) can't
    # generate verbatim context lines; they hallucinate from training data.
    _single_file_task = (
        len(ctx.target_files) == 1
        and not getattr(ctx, "cross_repo", False)
        and not force_full_content
    )
    if getattr(ctx, "cross_repo", False) and repo_roots:
        repos_listed = "\n".join(
            f'        "{r}": [{{"file_path": "...", "full_content": "...", "op": "modify"}}]'
            for r in ctx.repo_scope
        )
        schema_instruction = f"""## Output Schema

Return a JSON object matching **exactly** this structure (schema_version: "{_SCHEMA_VERSION_MULTI}"):

```json
{{
  "schema_version": "{_SCHEMA_VERSION_MULTI}",
  "candidates": [
    {{
      "candidate_id": "c1",
      "patches": {{
{repos_listed}
      }},
      "rationale": "<one sentence, max 200 chars>"
    }}
  ],
  "provider_metadata": {{
    "model_id": "<your model identifier>",
    "reasoning_summary": "<max 200 chars>"
  }}
}}
```

Each repo entry in `patches` is a list of file patch objects:
- `file_path`: path relative to that repo's root
- `full_content`: complete modified file content (not a diff)
- `op`: one of "modify", "create", "delete"

Rules:
- Return 1–3 candidates. c1 = primary, c2 = alternative.
- `full_content` must be the **complete** file (not a diff or patch).
- Python files must be syntactically valid.
- Only include repos that actually require changes. Omit unchanged repos.
- No extra keys at any level. Return ONLY the JSON object."""
    elif _single_file_task:
        # Task 4: ask for a unified diff — more token-efficient for focused edits.
        # Capture the source hash for the primary target file so we can embed it.
        _primary_sha = ""
        if ctx.target_files:
            _ppath = Path(ctx.target_files[0])
            _pabs = _ppath if _ppath.is_absolute() else ((repo_root or Path.cwd()) / _ppath)
            try:
                _primary_sha = _file_source_hash(_pabs.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass

        schema_instruction = f"""## Output Schema

⚠️  CRITICAL ANCHORING REQUIREMENT ⚠️
Your unified_diff MUST use verbatim context lines copied EXACTLY from the
"## Source Snapshot" section above. Do NOT use your trained memory of this file.
Count actual line numbers from the source provided — not from any cached knowledge.

Idempotency check: If the required change is ALREADY PRESENT in the source shown
above, return {{"no_op": true, "reason": "<why no change needed>"}} instead of a diff.

Return a JSON object matching **exactly** this structure (schema_version: "{_SCHEMA_VERSION_DIFF}"):

```json
{{
  "schema_version": "{_SCHEMA_VERSION_DIFF}",
  "source_sha256": "{_primary_sha[:12] if _primary_sha else '<first-12-chars-of-sha256-from-header>'}",
  "candidates": [
    {{
      "candidate_id": "c1",
      "file_path": "<repo-relative path matching the target file>",
      "unified_diff": "@@ -5,3 +5,4 @@\\n context\\n-old line\\n+new line\\n context",
      "rationale": "<one sentence, max 200 chars>"
    }}
  ],
  "provider_metadata": {{
    "model_id": "<your model identifier>",
    "reasoning_summary": "<max 200 chars>"
  }}
}}
```

Rules:
- Return 1–3 candidates. c1 = primary approach, c2 = alternative.
- `source_sha256`: echo back the first 12 chars of the SHA-256 from the Source Snapshot header.
- `unified_diff` must be a valid GNU unified diff (no --- / +++ header needed).
  - @@ hunk headers: `@@ -start[,count] +start[,count] @@`
  - Prefix context lines with a space, removed lines with `-`, added lines with `+`.
  - Context lines MUST be verbatim copies from the Source Snapshot shown above.
  - Include 3 lines of unchanged context around each change.
- The diff must apply cleanly to the source file shown above.
- Python changes must result in syntactically valid code.
- No extra keys at any level. Return ONLY the JSON object (or the no_op object)."""
    else:
        schema_instruction = f"""## Output Schema

Return a JSON object matching **exactly** this structure (schema_version: "{_SCHEMA_VERSION}"):

```json
{{
  "schema_version": "{_SCHEMA_VERSION}",
  "candidates": [
    {{
      "candidate_id": "c1",
      "file_path": "<repo-relative path matching the target file>",
      "full_content": "<complete modified file content — not a diff>",
      "rationale": "<one sentence, max 200 chars>"
    }}
  ],
  "provider_metadata": {{
    "model_id": "<your model identifier>",
    "reasoning_summary": "<max 200 chars>"
  }}
}}
```

Rules:
- Return 1–3 candidates. c1 = primary approach, c2 = alternative, c3 = minimal-change fallback.
- `full_content` must be the **complete** file (not a diff or patch).
- Python files must be syntactically valid (`ast.parse()`-clean).
- No extra keys at any level. Return ONLY the JSON object."""

    # ── 4. Assemble final prompt ─────────────────────────────────────────
    file_block = "\n\n".join(file_sections) if file_sections else "_No target files._"
    parts = [
        f"## Task\nOp-ID: {ctx.op_id}\nGoal: {ctx.description}",
    ]
    sys_ctx_block = _build_system_context_block(ctx)
    if sys_ctx_block is not None:
        parts.append(sys_ctx_block)
    parts += [
        f"## Source Snapshot\n\n{file_block}",
        context_block,
    ]
    if expanded_context_block:
        parts.append(expanded_context_block)
    if tools_enabled:
        parts.append(_build_tool_section())
    parts.append(schema_instruction)
    prompt = "\n\n".join(parts)

    # N7: Prompt-size gate — prevent silent context-window truncation.
    # Estimate: 4 chars ≈ 1 token (conservative for code/text mix).
    _limit = max_prompt_tokens
    if _limit is None:
        _limit = int(os.environ.get("JPRIME_MAX_PROMPT_TOKENS", "0")) or None
    if _limit is not None:
        _estimated_tokens = len(prompt) // 4
        if _estimated_tokens > _limit:
            raise RuntimeError(
                f"prompt_too_large:{_estimated_tokens}_tokens_estimated"
                f"_limit_{_limit}"
            )

    return prompt


# ---------------------------------------------------------------------------
# Shared: Response Parser helpers
# ---------------------------------------------------------------------------


def _try_reconstruct_from_ellipsis(
    full_content: str,
    source_path: str,
    max_change_chars: int = 500,
    repo_root: Optional[Path] = None,
) -> Optional[str]:
    """Reconstruct full file content when a small model outputs '...\\n[change]\\n...'

    Small models (e.g. Mistral 7B) commonly abbreviate unchanged file sections
    with '...' rather than emitting the full content verbatim.  When the content
    is short AND starts with '...', we attempt to recover by:

      1. Extracting the meaningful *change* that sits between the ellipsis tokens.
      2. Reading the original source file from disk.
      3. Appending the extracted change to the original (append-to-end only).

    Safety guard: reconstruction is skipped when the extracted change already
    appears verbatim in the first 90 % of the original file — that would indicate
    a mid-file edit whose position cannot be determined from the placeholder alone.

    Returns the reconstructed content string, or None when reconstruction is
    unsafe or impossible.
    """
    stripped = full_content.strip()

    # Must start with '...' and be short relative to a real file
    if not stripped.startswith("...") or len(stripped) > max_change_chars:
        return None

    # Strip leading '...' and surrounding whitespace / newlines
    remainder = stripped[3:].lstrip("\n")

    # Strip optional trailing '...' and any preceding whitespace
    if remainder.endswith("..."):
        remainder = remainder[:-3].rstrip()

    remainder = remainder.strip("\n").strip()
    if not remainder:
        return None

    # Read the original source file
    if not source_path:
        return None
    try:
        _sp = Path(source_path)
        abs_path = (
            _sp
            if _sp.is_absolute()
            else (repo_root or Path.cwd()) / source_path
        )
        if not abs_path.exists():
            return None
        original = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Safety: only append when the change is genuinely new (not already in the
    # first 90 % of the file — that would indicate a mid-file edit we can't
    # safely reconstruct without knowing the insert position).
    head_90pct = original[: int(len(original) * 0.9)]
    if remainder.strip() in head_90pct:
        return None

    # Reconstruct: append change to original
    if not original.endswith("\n"):
        original += "\n"
    return original + remainder + "\n"


# ---------------------------------------------------------------------------
# Reactor Core feedback — fire-and-forget content failure telemetry
# ---------------------------------------------------------------------------


async def _reactor_http_post(url: str, payload: dict, timeout_s: float = 3.0) -> None:
    """Low-level HTTP POST to Reactor Core telemetry endpoint.

    Separated from the main emit function so tests can patch it directly.
    Raises on network errors — callers must swallow exceptions.
    """
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status >= 500:
                    logger.debug("[ReactorFeedback] Server error %d", resp.status)
    except ImportError:
        import urllib.request
        import json as _json
        data = _json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=timeout_s)
        except Exception:
            pass


async def _emit_content_failure_to_reactor(payload: dict) -> None:
    """Fire-and-forget telemetry emission to Reactor Core on content failures.

    Never raises — all exceptions are swallowed.  The signal is best-effort:
    if Reactor Core is offline the failure is logged at DEBUG level only.

    Controlled by OUROBOROS_REACTOR_FEEDBACK_ENABLED env var (default: true).
    Target URL read from JARVIS_REACTOR_URL (default: http://localhost:8090).
    Endpoint: OUROBOROS_REACTOR_FEEDBACK_ENDPOINT (overrides default URL+path).
    """
    if os.environ.get("OUROBOROS_REACTOR_FEEDBACK_ENABLED", "true").lower() != "true":
        return
    reactor_url = os.environ.get("JARVIS_REACTOR_URL", "http://localhost:8090")
    endpoint = os.environ.get(
        "OUROBOROS_REACTOR_FEEDBACK_ENDPOINT",
        f"{reactor_url}/v1/telemetry/events",
    )
    timeout_s = float(os.environ.get("OUROBOROS_REACTOR_FEEDBACK_TIMEOUT_S", "3.0"))
    try:
        await _reactor_http_post(endpoint, payload, timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("[ReactorFeedback] Emission failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Shared: Response Parser
# ---------------------------------------------------------------------------


def _extract_json_block(raw: str) -> str:
    """Extract JSON from raw text, handling markdown fences.

    Tries direct parse first, then looks for ```json ... ``` blocks.
    """
    # Try direct parse first
    stripped = raw.strip()
    if stripped.startswith("{"):
        return stripped

    # Look for markdown JSON fences
    match = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", raw, re.DOTALL)
    if match:
        return match.group(1)

    return stripped


def _parse_tool_call_response(raw: str) -> Optional["ToolCall"]:
    """Parse a 2b.2-tool response into a ToolCall, or return None.

    Returns None for any parse/validation failure (including patch responses),
    so callers can treat None as "not a tool call".
    """
    try:
        data = json.loads(_extract_json_block(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != _TOOL_SCHEMA_VERSION:
        return None
    tc = data.get("tool_call")
    if not isinstance(tc, dict):
        return None
    name = tc.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = tc.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    from backend.core.ouroboros.governance.tool_executor import ToolCall
    return ToolCall(name=name, arguments=arguments)


def _parse_multi_repo_response(
    data: dict,
    provider_name: str,
    duration_s: float,
    repo_roots: Dict[str, Path],
) -> "GenerationResult":
    """Parse schema 2c.1 multi-repo response into GenerationResult with RepoPatch candidates."""
    from backend.core.ouroboros.governance.saga.saga_types import (
        FileOp,
        PatchedFile,
        RepoPatch,
    )

    pfx = provider_name
    raw_candidates = data.get("candidates", [])
    if not raw_candidates or not isinstance(raw_candidates, list):
        raise RuntimeError(f"{pfx}_schema_invalid:no_candidates:2c.1")

    validated: List[Dict[str, Any]] = []
    for raw_cand in raw_candidates[:3]:
        patches_raw = raw_cand.get("patches")
        if not isinstance(patches_raw, dict):
            raise RuntimeError(f"{pfx}_schema_invalid:missing_patches:2c.1")

        repo_patches: Dict[str, Any] = {}
        for repo_name, file_list in patches_raw.items():
            if not isinstance(file_list, list):
                raise RuntimeError(
                    f"{pfx}_schema_invalid:patches_not_list:{repo_name}"
                )

            patched_files: List[PatchedFile] = []
            new_content: List[Tuple[str, bytes]] = []

            for file_entry in file_list:
                file_path = file_entry.get("file_path")
                full_content = file_entry.get("full_content")
                op_str = file_entry.get("op", "modify")

                if not file_path or full_content is None:
                    raise RuntimeError(
                        f"{pfx}_schema_invalid:missing_file_fields:{repo_name}:{file_path}"
                    )

                # AST check for Python files
                if str(file_path).endswith(".py"):
                    try:
                        ast.parse(full_content)
                    except SyntaxError as e:
                        raise RuntimeError(
                            f"{pfx}_schema_invalid:syntax_error:{repo_name}:{file_path}:{e}"
                        ) from e

                # Validate op — unknown values are a model error, not a safe fallback
                try:
                    op = FileOp(op_str)
                except ValueError:
                    raise RuntimeError(
                        f"{pfx}_schema_invalid:unknown_op:{repo_name}:{file_path}:{op_str!r}"
                    )

                # Read preimage for MODIFY/DELETE ops
                preimage: Optional[bytes] = None
                if op in (FileOp.MODIFY, FileOp.DELETE):
                    repo_root = repo_roots.get(repo_name)
                    if repo_root is None:
                        raise RuntimeError(
                            f"{pfx}_schema_invalid:unknown_repo_in_patches:{repo_name}"
                        )
                    full_disk_path = Path(repo_root) / file_path
                    try:
                        preimage = full_disk_path.read_bytes()
                    except OSError:
                        preimage = b""
                        op = FileOp.CREATE

                patched_files.append(PatchedFile(path=file_path, op=op, preimage=preimage))
                # DELETE ops carry no new bytes — omit from new_content
                if op != FileOp.DELETE:
                    new_content.append((file_path, full_content.encode()))

            repo_patches[repo_name] = RepoPatch(
                repo=repo_name,
                files=tuple(patched_files),
                new_content=tuple(new_content),
            )

        validated.append({
            "candidate_id": raw_cand.get("candidate_id", "c1"),
            "patches": repo_patches,
            "rationale": raw_cand.get("rationale", ""),
        })

    if not validated:
        raise RuntimeError(f"{pfx}_schema_invalid:all_candidates_failed:2c.1")

    model_id = data.get("provider_metadata", {}).get("model_id", provider_name)
    return GenerationResult(
        candidates=tuple(validated),
        provider_name=provider_name,
        generation_duration_s=duration_s,
        model_id=model_id,
    )


def _parse_generation_response(
    raw: str,
    provider_name: str,
    duration_s: float,
    ctx: "OperationContext",
    source_hash: str,
    source_path: str,
    repo_roots: Optional[Dict[str, Path]] = None,
    repo_root: Optional[Path] = None,
) -> "GenerationResult":
    """Parse and strictly validate a generation response.

    Handles schema_version 2b.1, 2b.1-diff (Task 4), 2c.1, and no_op.

    Validation sequence (fail-fast):
      0. no_op shortcut: {"no_op": true} → GenerationResult(is_noop=True)
      1. JSON parse
      2. Top-level type = dict
      3. schema_version routing:
         2c.1       → _parse_multi_repo_response
         2b.1-diff  → pre-apply validation → apply unified diffs → rewrite as 2b.1
         other      → fail-fast
      4. No extra top-level keys (2b.1 only)
      5. candidates: non-empty list, len 1-3 (>3 → normalize + continue)
      6. Per-candidate: required fields, no extras, AST check for .py files
         SyntaxError → skip candidate; all fail → RuntimeError
      7. Compute per-candidate candidate_hash; attach source_hash, source_path

    Parameters
    ----------
    repo_root:
        Root path for resolving relative source_path in the 2b.1-diff branch.
        Uses repo_root if provided, falls back to cwd only as last resort.

    Returns GenerationResult with validated candidates as a tuple of dicts.
    """
    pfx = provider_name

    # Step 0: no_op shortcut — model signals change already present
    try:
        _quick = json.loads(_extract_json_block(raw))
    except (json.JSONDecodeError, ValueError):
        _quick = {}
    if isinstance(_quick, dict) and _quick.get("no_op") is True:
        logger.info("[%s] Model returned no_op: %s", pfx, _quick.get("reason", ""))
        return GenerationResult(
            candidates=(),
            provider_name=pfx,
            generation_duration_s=duration_s,
            is_noop=True,
        )

    # Step 1: JSON parse
    try:
        data = json.loads(_extract_json_block(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{pfx}_schema_invalid:json_parse_error") from exc

    # Step 2: top-level type
    if not isinstance(data, dict):
        raise RuntimeError(f"{pfx}_schema_invalid:expected_object")

    # Step 3: schema_version — route to dedicated parsers
    actual_version = data.get("schema_version", "__missing__")
    if actual_version == _SCHEMA_VERSION_MULTI:
        if not repo_roots:
            raise RuntimeError(f"{pfx}_schema_invalid:2c1_requires_repo_roots")
        return _parse_multi_repo_response(data, provider_name, duration_s, repo_roots)

    # Task 4: reconstruct full_content from unified diff before normal validation
    if actual_version == _SCHEMA_VERSION_DIFF:
        # Resolve source path: repo_root takes precedence over cwd (Disease 7 fix)
        orig_content = ""
        if source_path:
            _sp = Path(source_path)
            if _sp.is_absolute():
                _resolved = _sp
            elif repo_root is not None:
                _resolved = (repo_root / source_path).resolve()
            else:
                _resolved = (Path.cwd() / source_path).resolve()
            try:
                orig_content = _resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        if not orig_content and source_path:
            # Can't apply diff against empty/missing source — guard against silent corruption
            raise RuntimeError(
                f"{pfx}_schema_invalid:diff_source_unreadable:{source_path}"
            )

        raw_cands = data.get("candidates", [])
        if not isinstance(raw_cands, list) or not raw_cands:
            raise RuntimeError(f"{pfx}_schema_invalid:candidates_empty")
        rewritten: List[Dict[str, Any]] = []
        for cand in raw_cands:
            if not isinstance(cand, dict):
                continue
            unified_diff = cand.get("unified_diff", "")
            if not unified_diff or not orig_content:
                logger.warning("[%s] Skipping diff candidate %s: no diff/source", pfx, cand.get("candidate_id"))
                continue
            try:
                # Pre-apply validation gate (Disease 1 fix): check context lines
                # against the ACTUAL file before attempting to mutate anything.
                validate_diff_context(orig_content, unified_diff)
                patched = _apply_unified_diff(orig_content, unified_diff)
            except StaleDiffError as exc:
                logger.warning(
                    "[%s] Stale diff rejected for %s at hunk line %d: %s",
                    pfx, cand.get("candidate_id"), exc.hunk_line, exc,
                )
                # D8: fire-and-forget feedback to Reactor Core for model quality tracking
                try:
                    import asyncio as _asyncio
                    _loop = _asyncio.get_event_loop()
                    if _loop.is_running():
                        _loop.create_task(_emit_content_failure_to_reactor({
                            "event_type": "CUSTOM",
                            "source": "ouroboros.providers",
                            "data": {
                                "failure_type": "content_quality",
                                "failure_subtype": "stale_diff",
                                "provider": pfx,
                                "op_id": getattr(ctx, "op_id", ""),
                                "source_sha256": source_hash,
                                "candidate_id": cand.get("candidate_id", ""),
                                "error": str(exc),
                                "target_file": source_path,
                                "hunk_line": exc.hunk_line,
                            },
                            "labels": {
                                "provider": pfx,
                                "failure_class": "content",
                            },
                        }))
                except Exception:
                    pass  # never block on feedback emission
                continue
            except ValueError as exc:
                logger.warning("[%s] Diff application failed for %s: %s", pfx, cand.get("candidate_id"), exc)
                continue
            rewritten.append({
                "candidate_id": cand.get("candidate_id", "c1"),
                "file_path": cand.get("file_path", source_path),
                "full_content": patched,
                "rationale": cand.get("rationale", ""),
            })
        if not rewritten:
            raise RuntimeError(f"{pfx}_schema_invalid:diff_apply_failed_all_candidates")
        # Overwrite data so the rest of the function validates normally as 2b.1
        data = {
            "schema_version": _SCHEMA_VERSION,
            "candidates": rewritten,
            "provider_metadata": data.get("provider_metadata", {}),
        }
        actual_version = _SCHEMA_VERSION

    # schema_version "2b.1-noop" — model signals the change is already present
    if actual_version == "2b.1-noop":
        logger.info("[%s] Model returned 2b.1-noop: %s", pfx, data.get("reason", ""))
        return GenerationResult(
            candidates=(),
            provider_name=pfx,
            generation_duration_s=duration_s,
            is_noop=True,
        )

    if actual_version != _SCHEMA_VERSION:
        raise RuntimeError(
            f"{pfx}_schema_invalid:wrong_schema_version:{actual_version}"
        )

    # Step 4: extra top-level keys
    extra_top = set(data.keys()) - _SCHEMA_TOP_LEVEL_KEYS
    if extra_top:
        raise RuntimeError(
            f"{pfx}_schema_invalid:unexpected_keys:{','.join(sorted(extra_top))}"
        )

    # Step 5: candidates
    if "candidates" not in data:
        raise RuntimeError(f"{pfx}_schema_invalid:missing_candidates")
    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list) or len(raw_candidates) == 0:
        raise RuntimeError(f"{pfx}_schema_invalid:candidates_empty")

    # Normalize >3 candidates
    if len(raw_candidates) > 3:
        dropped_ids = [
            c.get("candidate_id", f"idx{i}") if isinstance(c, dict) else f"idx{i}"
            for i, c in enumerate(raw_candidates[3:], 3)
        ]
        logger.warning(
            "candidates_normalized: truncating %d candidates to 3; dropped=%s",
            len(raw_candidates),
            dropped_ids,
        )
        raw_candidates = raw_candidates[:3]

    # Step 6: per-candidate validation
    validated: List[Dict[str, Any]] = []
    for i, cand in enumerate(raw_candidates):
        if not isinstance(cand, dict):
            raise RuntimeError(f"{pfx}_schema_invalid:candidate_{i}_not_object")

        # Required fields
        for field in ("candidate_id", "file_path", "full_content", "rationale"):
            if field not in cand:
                raise RuntimeError(
                    f"{pfx}_schema_invalid:candidate_{i}_missing_{field}"
                )

        # Extra fields
        extra_cand = set(cand.keys()) - _CANDIDATE_KEYS
        if extra_cand:
            raise RuntimeError(
                f"{pfx}_schema_invalid:candidate_{i}_unexpected_keys:{','.join(sorted(extra_cand))}"
            )

        # AST check for Python files
        file_path: str = cand["file_path"]
        full_content: str = cand["full_content"]
        if file_path.endswith(".py"):
            try:
                ast.parse(full_content)
            except SyntaxError:
                logger.warning(
                    "Skipping candidate %s: SyntaxError in %s",
                    cand["candidate_id"],
                    file_path,
                )
                continue  # skip this candidate; try next

        # Placeholder / truncation guard — reject content that looks like the
        # model summarised the file rather than producing it.
        _PLACEHOLDER_PATTERNS = (
            "...<the entire",
            "<the entire file",
            "...<complete file",
            "<complete file content",
            "...<rest of",
            "# ... rest of file",
            "# (rest of file unchanged)",
            "<the complete modified file",
            "<the complete file",
            "<insert the",
            "<full file content",
        )
        _content_lower = full_content.lower()
        if any(p.lower() in _content_lower for p in _PLACEHOLDER_PATTERNS):
            logger.warning(
                "Skipping candidate %s: full_content contains placeholder text",
                cand["candidate_id"],
            )
            continue

        # Length sanity: if we know the original file, the candidate must be at
        # least 50% of the original byte-length (catches silent truncation).
        # When short content starts with '...' (small-model ellipsis), attempt
        # to reconstruct the full file before rejecting.
        if source_path:
            try:
                _sp2 = Path(source_path)
                _orig_path = (
                    _sp2 if _sp2.is_absolute()
                    else (repo_root or Path.cwd()) / source_path
                )
                if _orig_path.exists():
                    _orig_len = _orig_path.stat().st_size
                    _cand_len = len(full_content.encode())
                    if _orig_len > 200 and _cand_len < _orig_len * 0.5:
                        # Attempt ellipsis reconstruction before discarding
                        _reconstructed = _try_reconstruct_from_ellipsis(
                            full_content, source_path, repo_root=repo_root
                        )
                        if _reconstructed:
                            logger.info(
                                "[Parser] Reconstructed full_content from ellipsis "
                                "placeholder for %s (%d → %d bytes)",
                                cand["candidate_id"],
                                _cand_len,
                                len(_reconstructed.encode()),
                            )
                            full_content = _reconstructed
                            cand = dict(cand)
                            cand["full_content"] = full_content
                        else:
                            logger.warning(
                                "Skipping candidate %s: full_content too short "
                                "(%d bytes vs original %d bytes)",
                                cand["candidate_id"],
                                _cand_len,
                                _orig_len,
                            )
                            continue
            except OSError:
                pass  # can't stat — skip length check

        # Step 7: compute hashes and attach provenance
        candidate_hash = hashlib.sha256(full_content.encode()).hexdigest()
        enriched = dict(cand)
        enriched["candidate_hash"] = candidate_hash
        enriched["source_hash"] = source_hash
        enriched["source_path"] = source_path
        validated.append(enriched)

    if not validated:
        raise RuntimeError(f"{pfx}_schema_invalid:all_candidates_syntax_error")

    # Extract model_id from provider_metadata (optional)
    provider_metadata = data.get("provider_metadata", {})
    model_id = (
        provider_metadata.get("model_id", "")
        if isinstance(provider_metadata, dict)
        else ""
    )

    return GenerationResult(
        candidates=tuple(validated),
        provider_name=provider_name,
        generation_duration_s=duration_s,
        model_id=model_id,
    )


# ---------------------------------------------------------------------------
# PrimeProvider
# ---------------------------------------------------------------------------


class PrimeProvider:
    """CandidateProvider adapter wrapping PrimeClient.generate().

    Uses the existing PrimeClient for code generation with strict JSON
    schema enforcement. Temperature is fixed at 0.2 for deterministic
    code generation.

    Parameters
    ----------
    prime_client:
        An initialized PrimeClient instance.
    max_tokens:
        Maximum tokens for generation requests.
    """

    def __init__(
        self,
        prime_client: Any,
        max_tokens: int = 8192,
        repo_root: Optional[Path] = None,
        repo_roots: Optional[Dict[str, Path]] = None,
        tools_enabled: bool = False,
    ) -> None:
        self._client = prime_client
        self._max_tokens = max_tokens
        self._repo_root = repo_root
        self._repo_roots = repo_roots
        self._tools_enabled = tools_enabled

    @property
    def provider_name(self) -> str:
        return "gcp-jprime"

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate code candidates via PrimeClient with optional tool-call loop.

        When ``tools_enabled=True``, the model may respond with a 2b.2-tool
        schema response to request tool execution. The loop re-sends the prompt
        with tool results appended until the model returns a patch response or
        the iteration/budget limits are reached.

        Raises
        ------
        RuntimeError
            ``gcp-jprime_tool_loop_max_iterations`` if the model exceeds
            ``MAX_TOOL_ITERATIONS`` consecutive tool calls.
            ``gcp-jprime_tool_loop_budget_exceeded`` if the accumulated prompt
            exceeds ``MAX_TOOL_LOOP_CHARS``.
            ``gcp-jprime_schema_invalid:...`` on patch schema validation failure.
        """
        repo_root = self._repo_root or Path.cwd()
        executor = None  # created lazily on first tool call

        # Determine force_full_content from brain's schema_capability in routing telemetry.
        # "full_content_only" → True (models ≤14B can't produce verbatim diffs)
        # "full_content_and_diff" → False (32B+ can produce unified diffs)
        # Default True (conservative) if telemetry unavailable.
        _schema_cap = "full_content_only"
        if context.telemetry and context.telemetry.routing_intent:
            _schema_cap = getattr(
                context.telemetry.routing_intent, "schema_capability", "full_content_only"
            )
        _force_full = _schema_cap != "full_content_and_diff"

        prompt = _build_codegen_prompt(
            context,
            repo_root=self._repo_root,
            repo_roots=self._repo_roots,
            tools_enabled=self._tools_enabled,
            force_full_content=_force_full,
        )
        accumulated_chars = len(prompt)
        tool_rounds = 0
        start = time.monotonic()

        # Task 3: build TaskProfile from routing telemetry for J-Prime dispatch
        _brain_model: Optional[str] = None
        _task_profile: Optional[Any] = None
        if context.telemetry and context.telemetry.routing_intent:
            ri = context.telemetry.routing_intent
            _brain_model = ri.brain_model or None
            if _TaskProfile is not None and ri.brain_id and ri.brain_model:
                raw_reason = ri.routing_reason or "unknown"
                intent = (
                    raw_reason.removeprefix("cai_intent_")
                    if raw_reason.startswith("cai_intent_")
                    else raw_reason
                )
                _task_profile = _TaskProfile(
                    intent=intent,
                    complexity=ri.task_complexity or "unknown",
                    brain_id=ri.brain_id,
                    model=ri.brain_model,
                )

        while True:
            response = await self._client.generate(
                prompt=prompt,
                system_prompt=_CODEGEN_SYSTEM_PROMPT,
                max_tokens=self._max_tokens,
                temperature=0.2,
                model_name=_brain_model,
                task_profile=_task_profile,
            )
            raw = response.content

            # Log raw response for diagnosis (truncated at 2000 chars)
            logger.warning(
                "[PrimeProvider] J-Prime raw response (len=%d bytes, first 2000): %r",
                len(raw.encode()) if raw else 0,
                raw[:2000] if raw else "",
            )

            # Attempt to parse as tool call
            if self._tools_enabled:
                tool_call = _parse_tool_call_response(raw)
                if tool_call is not None:
                    if tool_rounds >= MAX_TOOL_ITERATIONS:
                        raise RuntimeError(
                            f"gcp-jprime_tool_loop_max_iterations:{MAX_TOOL_ITERATIONS}"
                        )
                    # Lazily create executor on first tool call
                    if executor is None:
                        from backend.core.ouroboros.governance.tool_executor import ToolExecutor
                        executor = ToolExecutor(repo_root=repo_root)
                    # Execute the tool
                    tool_result = executor.execute(tool_call)
                    result_text = (
                        f"--- Tool Result: {tool_call.name} ---\n"
                        f"{tool_result.output if not tool_result.error else 'ERROR: ' + tool_result.error}\n"
                        "--- End Tool Result ---\n"
                        "Now continue. Either call another tool or return the patch JSON."
                    )
                    # Append tool exchange to prompt (single-turn Prime)
                    old_prompt_len = len(prompt)
                    prompt = (
                        f"{prompt}\n\n"
                        f"[You called: {tool_call.name}({json.dumps(tool_call.arguments)})]\n"
                        f"{result_text}"
                    )
                    accumulated_chars += len(prompt) - old_prompt_len
                    if accumulated_chars > MAX_TOOL_LOOP_CHARS:
                        raise RuntimeError(
                            f"gcp-jprime_tool_loop_budget_exceeded:{accumulated_chars}"
                        )
                    tool_rounds += 1
                    continue  # re-send to model

            # Not a tool call (or tools disabled) — parse as patch
            duration = time.monotonic() - start

            source_hash = ""
            source_path = ""
            if context.target_files:
                source_path = context.target_files[0]
                abs_path = (repo_root / source_path) if repo_root else Path(source_path)
                try:
                    content_bytes = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else ""
                    source_hash = _file_source_hash(content_bytes)
                except OSError:
                    pass

            result = _parse_generation_response(
                raw,
                self.provider_name,
                duration,
                context,
                source_hash,
                source_path,
                repo_roots=self._repo_roots,
                repo_root=self._repo_root,
            )

            logger.info(
                "[PrimeProvider] Generated %d candidates in %.1fs (tool_rounds=%d), "
                "model=%s, tokens=%d",
                len(result.candidates),
                duration,
                tool_rounds,
                getattr(response, "model", "unknown"),
                getattr(response, "tokens_used", 0),
            )
            return result

    async def health_probe(self) -> bool:
        """Check PrimeClient health. Returns True only if AVAILABLE."""
        try:
            status = await self._client._check_health()
            return status.name == "AVAILABLE"
        except Exception:
            logger.debug("[PrimeProvider] Health probe failed", exc_info=True)
            return False

    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a lightweight planning prompt; return raw string response.

        Used by ContextExpander for expansion rounds. Caller parses expansion.1 JSON.
        Low token budget (512) and temperature=0.0 for deterministic planning.
        """
        response = await self._client.generate(
            prompt=prompt,
            system_prompt=(
                "You are a code context analyst for the JARVIS self-programming pipeline. "
                "Identify additional files needed for context. "
                "Respond with valid JSON only matching schema_version expansion.1. "
                "No markdown, no preamble."
            ),
            max_tokens=512,
            temperature=0.0,
        )
        return response.content


# ---------------------------------------------------------------------------
# ClaudeProvider
# ---------------------------------------------------------------------------

# Cost estimation constants (per 1M tokens, approximate)
_CLAUDE_INPUT_COST_PER_M = 3.00   # Sonnet pricing
_CLAUDE_OUTPUT_COST_PER_M = 15.00


class ClaudeProvider:
    """CandidateProvider adapter wrapping the Anthropic Claude API.

    Cost-gated: each call checks accumulated daily spend against
    ``daily_budget`` before proceeding. Budget resets at midnight UTC.

    Parameters
    ----------
    api_key:
        Anthropic API key.
    model:
        Model identifier (default: claude-sonnet-4-20250514).
    max_tokens:
        Maximum output tokens per generation.
    max_cost_per_op:
        Maximum estimated cost per single operation.
    daily_budget:
        Maximum daily spend in USD.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        max_cost_per_op: float = 0.50,
        daily_budget: float = 10.00,
        repo_root: Optional[Path] = None,
        repo_roots: Optional[Dict[str, Path]] = None,
        tools_enabled: bool = False,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._max_cost_per_op = max_cost_per_op
        self._daily_budget = daily_budget
        self._daily_spend: float = 0.0
        self._budget_reset_date = datetime.now(tz=timezone.utc).date()
        self._client: Any = None  # Lazy init
        self._repo_root = repo_root
        self._repo_roots = repo_roots
        self._tools_enabled = tools_enabled

    @property
    def provider_name(self) -> str:
        return "claude-api"

    def _ensure_client(self) -> Any:
        """Lazily initialize the Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
            except ImportError:
                raise RuntimeError(
                    "claude_api_unavailable:anthropic_not_installed"
                )
        return self._client

    def _maybe_reset_daily_budget(self) -> None:
        """Reset daily spend if the day has changed."""
        today = datetime.now(tz=timezone.utc).date()
        if today > self._budget_reset_date:
            self._daily_spend = 0.0
            self._budget_reset_date = today

    def _record_cost(self, cost: float) -> None:
        """Record cost from a generation call."""
        self._daily_spend += cost

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost in USD from token counts."""
        input_cost = (input_tokens / 1_000_000) * _CLAUDE_INPUT_COST_PER_M
        output_cost = (output_tokens / 1_000_000) * _CLAUDE_OUTPUT_COST_PER_M
        return input_cost + output_cost

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate code candidates via Claude API with optional tool-call loop.

        When ``tools_enabled=True``, the model may respond with a 2b.2-tool
        schema response to request tool execution. The loop re-sends the
        conversation with tool results appended until the model returns a patch
        response or the iteration/budget limits are reached.

        Checks budget before calling, estimates cost after, and records spend
        for daily tracking.

        Raises
        ------
        RuntimeError
            ``claude_budget_exhausted`` if daily budget exceeded.
            ``claude-api_tool_loop_max_iterations`` if the model exceeds
            ``MAX_TOOL_ITERATIONS`` consecutive tool calls.
            ``claude-api_tool_loop_budget_exceeded`` if the accumulated prompt
            exceeds ``MAX_TOOL_LOOP_CHARS``.
            ``claude-api_schema_invalid:...`` on schema validation failure.
        """
        self._maybe_reset_daily_budget()

        if self._daily_spend >= self._daily_budget:
            raise RuntimeError("claude_budget_exhausted")

        client = self._ensure_client()
        repo_root = self._repo_root or Path.cwd()
        executor = None  # lazy init on first tool call

        prompt_text = _build_codegen_prompt(
            context,
            repo_root=self._repo_root,
            repo_roots=self._repo_roots,
            tools_enabled=self._tools_enabled,
        )
        # Build messages array for multi-turn conversation
        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt_text}]
        accumulated_chars = len(prompt_text)
        tool_rounds = 0
        total_cost = 0.0
        start = time.monotonic()

        while True:
            timeout_s = max(1.0, (deadline - datetime.now(tz=timezone.utc)).total_seconds())
            msg = await asyncio.wait_for(
                client.messages.create(
                    model=self._model,
                    max_tokens=min(self._max_tokens, 8192),
                    temperature=0.2,
                    system=_CODEGEN_SYSTEM_PROMPT,
                    messages=messages,
                ),
                timeout=timeout_s,
            )
            raw = msg.content[0].text if msg.content else ""
            input_tokens = getattr(msg.usage, "input_tokens", 0)
            output_tokens = getattr(msg.usage, "output_tokens", 0)
            cost = self._estimate_cost(input_tokens, output_tokens)
            self._record_cost(cost)
            total_cost += cost
            if total_cost >= self._max_cost_per_op:
                raise RuntimeError(f"claude_budget_exhausted_op:{total_cost:.4f}")

            # Attempt tool call parse when tools are enabled
            if self._tools_enabled:
                tool_call = _parse_tool_call_response(raw)
                if tool_call is not None:
                    if tool_rounds >= MAX_TOOL_ITERATIONS:
                        raise RuntimeError(
                            f"claude-api_tool_loop_max_iterations:{MAX_TOOL_ITERATIONS}"
                        )
                    if executor is None:
                        from backend.core.ouroboros.governance.tool_executor import ToolExecutor
                        executor = ToolExecutor(repo_root=repo_root)
                    tool_result = executor.execute(tool_call)
                    result_text = (
                        f"Tool result for {tool_call.name}:\n"
                        f"{tool_result.output if not tool_result.error else 'ERROR: ' + tool_result.error}\n"
                        "Now either call another tool or return the patch JSON."
                    )
                    # Append assistant + user turns for multi-turn conversation
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content": result_text})
                    # Multi-turn: track delta as assistant response + user follow-up (no
                    # full-string re-measurement — messages array grows, not a flat string).
                    accumulated_chars += len(raw) + len(result_text)
                    if accumulated_chars > MAX_TOOL_LOOP_CHARS:
                        raise RuntimeError(
                            f"claude-api_tool_loop_budget_exceeded:{accumulated_chars}"
                        )
                    tool_rounds += 1
                    continue

            # Not a tool call (or tools disabled) — parse as patch response
            duration = time.monotonic() - start
            source_hash = ""
            source_path = context.target_files[0] if context.target_files else ""
            if source_path:
                abs_path = (repo_root / source_path) if repo_root else Path(source_path)
                try:
                    content_bytes = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else ""
                    source_hash = _file_source_hash(content_bytes)
                except OSError:
                    pass

            result = _parse_generation_response(
                raw,
                self.provider_name,
                duration,
                context,
                source_hash,
                source_path,
                repo_roots=self._repo_roots,
                repo_root=self._repo_root,
            )

            logger.info(
                "[ClaudeProvider] %d candidates in %.1fs (tool_rounds=%d), cost=$%.4f",
                len(result.candidates), duration, tool_rounds, total_cost,
            )
            return result

    async def health_probe(self) -> bool:
        """Lightweight API ping. Returns True if API responds."""
        try:
            client = self._ensure_client()
            await client.messages.create(
                model=self._model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:
            logger.debug("[ClaudeProvider] Health probe failed", exc_info=True)
            return False

    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a lightweight planning prompt; return raw string response.

        Used by ContextExpander for expansion rounds. Caller parses expansion.1 JSON.
        Counts against daily budget (low token usage).
        """
        self._maybe_reset_daily_budget()
        if self._daily_spend >= self._daily_budget:
            raise RuntimeError("claude_budget_exhausted")

        client = self._ensure_client()
        message = await client.messages.create(
            model=self._model,
            max_tokens=512,
            system=(
                "You are a code context analyst for the JARVIS self-programming pipeline. "
                "Identify additional files needed for context. "
                "Respond with valid JSON only matching schema_version expansion.1. "
                "No markdown, no preamble."
            ),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        input_tokens = getattr(message.usage, "input_tokens", 0)
        output_tokens = getattr(message.usage, "output_tokens", 0)
        self._record_cost(self._estimate_cost(input_tokens, output_tokens))
        return message.content[0].text
