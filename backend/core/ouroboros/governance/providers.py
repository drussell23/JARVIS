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
import hashlib
import json
import logging
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
    "No markdown preamble, no explanations outside the JSON. Only the JSON object."
)

# ── Phase 2B: size/security constants ────────────────────────────────────
_MAX_TARGET_FILE_CHARS = 6000      # full content below this
_TARGET_FILE_HEAD_CHARS = 4000     # head kept on truncation
_TARGET_FILE_TAIL_CHARS = 1000     # tail kept on truncation
_MAX_IMPORT_CONTEXT_CHARS = 1500   # total across all discovered import files
_MAX_TEST_CONTEXT_CHARS = 1500     # total across all discovered test files
_MAX_IMPORT_FILES = 5              # hard cap on discovered import sources
_MAX_TEST_FILES = 2                # hard cap on discovered test files
_SCHEMA_VERSION = "2b.1"
_SCHEMA_VERSION_MULTI = "2c.1"
_SCHEMA_TOP_LEVEL_KEYS = frozenset({"schema_version", "candidates", "provider_metadata"})
_CANDIDATE_KEYS = frozenset({"candidate_id", "file_path", "full_content", "rationale"})


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


def _build_codegen_prompt(
    ctx: "OperationContext",
    repo_root: Optional[Path] = None,
    repo_roots: Optional[Dict[str, Path]] = None,
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

    # ── 3. Output schema instruction ────────────────────────────────────
    if ctx.cross_repo and repo_roots:
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
    return "\n\n".join([
        f"## Task\nOp-ID: {ctx.op_id}\nGoal: {ctx.description}",
        f"## Source Snapshot\n\n{file_block}",
        context_block,
        schema_instruction,
    ])


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
) -> "GenerationResult":
    """Parse and strictly validate a schema_version 2b.1 or 2c.1 generation response.

    Validation sequence (fail-fast):
      1. JSON parse
      2. Top-level type = dict
      3. schema_version routing: 2c.1 → _parse_multi_repo_response; other → fail-fast
      4. No extra top-level keys (2b.1 only)
      5. candidates: non-empty list, len 1-3 (>3 → normalize + continue)
      6. Per-candidate: required fields, no extras, AST check for .py files
         SyntaxError → skip candidate; all fail → RuntimeError
      7. Compute per-candidate candidate_hash; attach source_hash, source_path

    Returns GenerationResult with validated candidates as a tuple of dicts.
    """
    pfx = provider_name

    # Step 1: JSON parse
    try:
        data = json.loads(_extract_json_block(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{pfx}_schema_invalid:json_parse_error") from exc

    # Step 2: top-level type
    if not isinstance(data, dict):
        raise RuntimeError(f"{pfx}_schema_invalid:expected_object")

    # Step 3: schema_version — route multi-repo schema to dedicated parser
    actual_version = data.get("schema_version", "__missing__")
    if actual_version == _SCHEMA_VERSION_MULTI:
        if not repo_roots:
            raise RuntimeError(f"{pfx}_schema_invalid:2c1_requires_repo_roots")
        return _parse_multi_repo_response(data, provider_name, duration_s, repo_roots)

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
    ) -> None:
        self._client = prime_client
        self._max_tokens = max_tokens
        self._repo_root = repo_root
        self._repo_roots = repo_roots

    @property
    def provider_name(self) -> str:
        return "gcp-jprime"

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate code candidates via PrimeClient.

        Builds a structured prompt, calls PrimeClient.generate() with
        low temperature, and parses the response with strict schema
        validation.

        Raises
        ------
        RuntimeError
            On schema validation failure (``gcp-jprime_schema_invalid:...``).
        """
        prompt = _build_codegen_prompt(
            context, repo_root=self._repo_root, repo_roots=self._repo_roots
        )
        start = time.monotonic()

        response = await self._client.generate(
            prompt=prompt,
            system_prompt=_CODEGEN_SYSTEM_PROMPT,
            max_tokens=self._max_tokens,
            temperature=0.2,
        )

        duration = time.monotonic() - start

        # Compute source_hash for the first target file
        source_hash = ""
        source_path = ""
        if context.target_files:
            source_path = context.target_files[0]
            abs_path = (self._repo_root / source_path) if self._repo_root else Path(source_path)
            try:
                content_bytes = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else ""
                source_hash = _file_source_hash(content_bytes)
            except OSError:
                pass

        result = _parse_generation_response(
            response.content,
            self.provider_name,
            duration,
            context,
            source_hash,
            source_path,
            repo_roots=self._repo_roots,
        )

        logger.info(
            "[PrimeProvider] Generated %d candidates in %.1fs, model=%s, tokens=%d",
            len(result.candidates),
            duration,
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
        """Generate code candidates via Claude API.

        Checks budget before calling, estimates cost after, and records
        spend for daily tracking.

        Raises
        ------
        RuntimeError
            ``claude_budget_exhausted`` if daily budget exceeded.
            ``claude-api_schema_invalid:...`` on schema validation failure.
        """
        self._maybe_reset_daily_budget()

        if self._daily_spend >= self._daily_budget:
            raise RuntimeError("claude_budget_exhausted")

        client = self._ensure_client()
        prompt = _build_codegen_prompt(
            context, repo_root=self._repo_root, repo_roots=self._repo_roots
        )
        start = time.monotonic()

        message = await client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=_CODEGEN_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )

        duration = time.monotonic() - start
        raw_content = message.content[0].text

        # Track cost
        input_tokens = getattr(message.usage, "input_tokens", 0)
        output_tokens = getattr(message.usage, "output_tokens", 0)
        cost = self._estimate_cost(input_tokens, output_tokens)
        self._record_cost(cost)

        # Compute source_hash for the first target file
        source_hash = ""
        source_path = ""
        if context.target_files:
            source_path = context.target_files[0]
            abs_path = (self._repo_root / source_path) if self._repo_root else Path(source_path)
            try:
                content_bytes = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else ""
                source_hash = _file_source_hash(content_bytes)
            except OSError:
                pass

        result = _parse_generation_response(
            raw_content,
            self.provider_name,
            duration,
            context,
            source_hash,
            source_path,
            repo_roots=self._repo_roots,
        )

        logger.info(
            "[ClaudeProvider] Generated %d candidates in %.1fs, "
            "model=%s, tokens=%d+%d, cost=$%.4f, daily_spend=$%.4f/$%.2f",
            len(result.candidates),
            duration,
            getattr(message, "model", self._model),
            input_tokens,
            output_tokens,
            cost,
            self._daily_spend,
            self._daily_budget,
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
