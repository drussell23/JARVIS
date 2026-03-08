# Phase 2B: Code Generation with File Context — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire real LLM code generation into the Ouroboros pipeline: enriched prompts with actual file contents, strict multi-candidate schema, connectivity preflight, per-candidate ledger provenance, source-drift check before APPLY, and winner traceability.

**Architecture:** Four existing files modified, zero new modules. `providers.py` gets file-reading prompt enrichment and a strict `schema_version: "2b.1"` parser. `governed_loop_service.py` gets a connectivity preflight in `submit()`. `orchestrator.py` gets per-candidate ledger entries, source-drift check, and winner metadata. `op_context.py` gets `model_id` on `GenerationResult`.

**Tech Stack:** Python 3.11+, asyncio, hashlib, ast, pathlib, pytest (asyncio_mode=auto), `GovernedOrchestrator`, `CandidateGenerator`, `PrimeProvider`, `ClaudeProvider`, `LanguageRouter`

---

## Context (read before touching any file)

Key locations — verify line numbers with grep before editing:
- `backend/core/ouroboros/governance/op_context.py` — `GenerationResult` ~line 143
- `backend/core/ouroboros/governance/providers.py` — `_build_codegen_prompt` ~line 64, `_parse_generation_response` ~line 110, `PrimeProvider` ~line 201, `ClaudeProvider` ~line 291
- `backend/core/ouroboros/governance/governed_loop_service.py` — `submit()` ~line 289, pipeline_deadline stamp ~line 340, `_build_components()` ~line 395
- `backend/core/ouroboros/governance/orchestrator.py` — GENERATE phase ~line 208, VALIDATE phase ~line 252, `_run_validation()` ~line 515, `_build_change_request()` ~line 651
- `backend/core/ouroboros/governance/test_runner.py` — `BlockedPathError` ~line 49

Key invariants:
- `asyncio_mode = auto` in pytest.ini — never add `@pytest.mark.asyncio`
- `OperationContext` is a frozen dataclass — never mutate, always `.advance()` or `.with_pipeline_deadline()`
- `OperationState` enum values: `PLANNED`, `GATING`, `APPLIED`, `COMPLETE`, `FAILED`, `BLOCKED`, `ROLLED_BACK`
- Candidate dict keys BEFORE this plan: `file`, `content`. AFTER this plan: `file_path`, `full_content`, `candidate_id`, `candidate_hash`, `rationale`, `source_hash`, `source_path`
- `failure_class` taxonomy: `"test"`, `"build"`, `"infra"`, `"budget"`, `"security"` — never `"parse"`
- Pre-existing pyright `Import "backend.*" could not be resolved` is a venv false positive — ignore IDE errors, verify with `python3 -m pyright <files>`

---

## Task 1: Add `model_id` to `GenerationResult` in `op_context.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/op_context.py` (~line 143)
- Test: `tests/governance/self_dev/test_op_context_phase2b.py`

### Step 1: Write the failing tests

```python
# tests/governance/self_dev/test_op_context_phase2b.py
"""Tests for Phase 2B op_context changes."""
from backend.core.ouroboros.governance.op_context import GenerationResult


def test_generation_result_model_id_defaults_to_empty_string():
    """model_id has backward-compatible default."""
    gr = GenerationResult(
        candidates=(),
        provider_name="gcp-jprime",
        generation_duration_s=0.5,
    )
    assert gr.model_id == ""


def test_generation_result_model_id_can_be_set():
    """model_id can be set explicitly."""
    gr = GenerationResult(
        candidates=(),
        provider_name="gcp-jprime",
        generation_duration_s=0.5,
        model_id="llama-3.3-70b",
    )
    assert gr.model_id == "llama-3.3-70b"


def test_generation_result_is_still_frozen():
    """GenerationResult remains immutable."""
    import pytest
    gr = GenerationResult(candidates=(), provider_name="p", generation_duration_s=0.1)
    with pytest.raises((AttributeError, TypeError)):
        gr.model_id = "changed"  # type: ignore[misc]
```

### Step 2: Run — expect FAIL

```bash
python3 -m pytest tests/governance/self_dev/test_op_context_phase2b.py -v
```
Expected: FAIL — `TypeError: GenerationResult.__init__() got an unexpected keyword argument 'model_id'`

### Step 3: Add `model_id` field to `GenerationResult`

In `op_context.py`, find `GenerationResult` (~line 143). Add `model_id: str = ""` as the last field (with default, so existing callers still work):

```python
@dataclass(frozen=True)
class GenerationResult:
    candidates: Tuple[Dict[str, Any], ...]
    provider_name: str
    generation_duration_s: float
    model_id: str = ""    # provider model identifier; empty = not reported
```

### Step 4: Run — expect PASS

```bash
python3 -m pytest tests/governance/self_dev/test_op_context_phase2b.py -v
```
Expected: 3 passed

### Step 5: Full governance suite — no regressions

```bash
python3 -m pytest tests/governance/ -q 2>&1 | tail -5
```
Expected: 328 passed (all existing tests still pass)

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/op_context.py tests/governance/self_dev/test_op_context_phase2b.py
git commit -m "feat(ouroboros): add model_id to GenerationResult — backward-compatible default"
```

---

## Task 2: Path safety + context discovery + enriched prompt (`providers.py`)

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (lines 1–84, ~200 lines)
- Test: `tests/governance/self_dev/test_prompt_enrichment.py`

### Step 1: Write the failing tests

```python
# tests/governance/self_dev/test_prompt_enrichment.py
"""Tests for Phase 2B prompt enrichment — file context, path safety, truncation."""
import hashlib
import json
import pytest
from pathlib import Path

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.providers import (
    _build_codegen_prompt,
    _find_context_files,
    _read_with_truncation,
    _safe_context_path,
)
from backend.core.ouroboros.governance.test_runner import BlockedPathError

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ctx(target_files, description="improve the code", repo_root=None):
    root = repo_root or REPO_ROOT
    return OperationContext.create(
        target_files=tuple(str(Path(f).relative_to(root)) if Path(f).is_absolute() else f
                           for f in target_files),
        description=description,
    )


# ── _safe_context_path ────────────────────────────────────────────────────

def test_safe_context_path_allows_valid_repo_file(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("x = 1")
    result = _safe_context_path(tmp_path, f)
    assert result == f.resolve()


def test_safe_context_path_rejects_file_outside_repo(tmp_path):
    outside = Path("/etc/passwd")
    with pytest.raises(BlockedPathError):
        _safe_context_path(tmp_path, outside)


def test_safe_context_path_rejects_symlink(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("x = 1")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    with pytest.raises(BlockedPathError):
        _safe_context_path(tmp_path, link)


# ── _read_with_truncation ─────────────────────────────────────────────────

def test_read_with_truncation_short_file_full(tmp_path):
    f = tmp_path / "short.py"
    content = "x = 1\n" * 10
    f.write_text(content)
    result = _read_with_truncation(f, max_chars=6000)
    assert result == content
    assert "TRUNCATED" not in result


def test_read_with_truncation_large_file_has_marker(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("# head\n" + "x = 1\n" * 2000)
    result = _read_with_truncation(f, max_chars=6000)
    assert "TRUNCATED" in result
    assert "# head" in result   # head preserved
    assert len(result) < len("# head\n" + "x = 1\n" * 2000)


# ── _build_codegen_prompt ─────────────────────────────────────────────────

def test_prompt_includes_file_content(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("def hello():\n    return 42\n")
    ctx = _ctx([str(target)], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "def hello():" in prompt
    assert "return 42" in prompt


def test_prompt_includes_sha256_header(tmp_path):
    target = tmp_path / "mymod.py"
    content = "def hello():\n    return 42\n"
    target.write_text(content)
    expected_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    ctx = _ctx([str(target)], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert expected_hash in prompt


def test_prompt_includes_schema_version_2b1(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("x = 1\n")
    ctx = _ctx([str(target)], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert '"schema_version"' in prompt
    assert "2b.1" in prompt


def test_prompt_includes_candidate_id_field(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("x = 1\n")
    ctx = _ctx([str(target)], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "candidate_id" in prompt
    assert "full_content" in prompt
    assert "file_path" in prompt
    assert "rationale" in prompt


def test_prompt_truncates_large_file(tmp_path):
    target = tmp_path / "big.py"
    target.write_text("# TOP\n" + "x = 1\n" * 2000 + "# BOTTOM\n")
    ctx = _ctx([str(target)], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "TRUNCATED" in prompt
    assert "# TOP" in prompt


def test_prompt_includes_op_id(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("x = 1\n")
    ctx = _ctx([str(target)], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert ctx.op_id in prompt


# ── _find_context_files ───────────────────────────────────────────────────

def test_find_context_files_cap_import_files(tmp_path):
    # Create many importable modules in repo
    for i in range(10):
        (tmp_path / f"mod{i}.py").write_text(f"x{i} = {i}")
    target = tmp_path / "main.py"
    target.write_text("\n".join(f"from mod{i} import x{i}" for i in range(10)))
    import_files, test_files = _find_context_files(target, tmp_path)
    assert len(import_files) <= 5    # hard cap


def test_find_context_files_cap_test_files(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for i in range(5):
        (tests_dir / f"test_mod{i}.py").write_text("import main\n")
    target = tmp_path / "main.py"
    target.write_text("x = 1\n")
    _, test_files = _find_context_files(target, tmp_path)
    assert len(test_files) <= 2      # hard cap
```

### Step 2: Run — expect FAIL

```bash
python3 -m pytest tests/governance/self_dev/test_prompt_enrichment.py -v 2>&1 | head -30
```
Expected: FAIL — `ImportError: cannot import name '_safe_context_path' from 'providers'` (or similar)

### Step 3: Add helpers and rewrite `_build_codegen_prompt` in `providers.py`

**Replace the top of `providers.py` (imports + constants + helpers), keeping classes untouched:**

At the top of `providers.py`, add/update imports:
```python
import hashlib
import re
from pathlib import Path
from typing import List, Optional, Tuple
```

**Add these four helper functions before `_build_codegen_prompt` (around line 39):**

```python
# ── Phase 2B: size/security constants ────────────────────────────────────
_MAX_TARGET_FILE_CHARS = 6000      # full content below this
_TARGET_FILE_HEAD_CHARS = 4000     # head kept on truncation
_TARGET_FILE_TAIL_CHARS = 1000     # tail kept on truncation
_MAX_IMPORT_CONTEXT_CHARS = 1500   # total across all discovered import files
_MAX_TEST_CONTEXT_CHARS = 1500     # total across all discovered test files
_MAX_IMPORT_FILES = 5              # hard cap on discovered import sources
_MAX_TEST_FILES = 2                # hard cap on discovered test files
_SCHEMA_VERSION = "2b.1"


def _safe_context_path(repo_root: Path, target: Path) -> Path:
    """Resolve target path and verify it stays within repo_root.

    Raises BlockedPathError if the resolved path is outside repo_root
    or if the path is a symlink.
    """
    from backend.core.ouroboros.governance.test_runner import BlockedPathError
    resolved = target.resolve()
    if resolved.is_symlink():
        raise BlockedPathError(f"Symlink not allowed in context discovery: {target}")
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
```

**Replace `_build_codegen_prompt` (lines 64-84) with this implementation:**

```python
def _build_codegen_prompt(
    ctx: "OperationContext",
    repo_root: Optional[Path] = None,
) -> str:
    """Build an enriched codegen prompt with file contents, context, and schema.

    Reads each target file from disk, hashes it, applies truncation, discovers
    surrounding import/test context (capped), and injects the schema_version 2b.1
    output specification.
    """
    from backend.core.ouroboros.governance.test_runner import BlockedPathError

    if repo_root is None:
        repo_root = Path.cwd()

    # ── 1. Build source snapshot for each target file ──────────────────
    file_sections: List[str] = []
    for raw_path in ctx.target_files:
        abs_path = (repo_root / raw_path).resolve()
        try:
            abs_path = _safe_context_path(repo_root, abs_path)
        except BlockedPathError as exc:
            file_sections.append(f"## File: {raw_path}\n[BLOCKED: {exc}]\n")
            continue

        content = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else ""
        source_hash = _file_source_hash(content)
        size_bytes = len(content.encode())
        line_count = content.count("\n")
        truncated = _read_with_truncation(abs_path)

        file_sections.append(
            f"## File: {raw_path} [SHA-256: {source_hash[:12]}]"
            f" [{size_bytes} bytes, {line_count} lines]\n"
            f"```\n{truncated}\n```"
        )

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
```

**Also update `_CODEGEN_SYSTEM_PROMPT` (lines 39-42) to reflect Phase 2B:**
```python
_CODEGEN_SYSTEM_PROMPT = (
    "You are a precise code modification assistant for the JARVIS codebase. "
    "You MUST respond with valid JSON only, matching schema_version 2b.1. "
    "No markdown preamble, no explanations outside the JSON. Only the JSON object."
)
```

**Remove `_CODEGEN_SCHEMA_INSTRUCTION` entirely** — the schema is now embedded in `_build_codegen_prompt`.

**Update `PrimeProvider.generate()` and `ClaudeProvider.generate()`** to pass `repo_root` to `_build_codegen_prompt`. Find the call site in each (`prompt = _build_codegen_prompt(context)`) and change to:
```python
prompt = _build_codegen_prompt(context, repo_root=self._repo_root if hasattr(self, "_repo_root") else None)
```

Also add `repo_root: Optional[Path] = None` parameter to both provider constructors and store as `self._repo_root = repo_root`.

Update `GovernedLoopService._build_components()` to pass `repo_root=self._config.project_root` when constructing `PrimeProvider` and `ClaudeProvider`.

### Step 4: Run tests — expect PASS

```bash
python3 -m pytest tests/governance/self_dev/test_prompt_enrichment.py -v
```
Expected: all tests pass

### Step 5: Pyright check

```bash
python3 -m pyright backend/core/ouroboros/governance/providers.py 2>&1 | tail -5
```
Expected: 0 errors

### Step 6: Full governance suite

```bash
python3 -m pytest tests/governance/ -q 2>&1 | tail -5
```
Expected: 328+ passed

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/providers.py \
        backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/self_dev/test_prompt_enrichment.py
git commit -m "feat(ouroboros): enriched codegen prompt — file contents, SHA-256 headers, truncation, context discovery, schema 2b.1"
```

---

## Task 3: Strict `schema_version: "2b.1"` parser in `providers.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (`_parse_generation_response` ~line 110)
- Test: `tests/governance/self_dev/test_candidate_parser.py`

### Step 1: Write the failing tests

```python
# tests/governance/self_dev/test_candidate_parser.py
"""Tests for Phase 2B strict candidate parser (schema_version: 2b.1)."""
import ast
import hashlib
import json
import pytest

from backend.core.ouroboros.governance.providers import _parse_generation_response


def _valid_payload(candidates=None, extras=None):
    """Build a valid 2b.1 payload."""
    cands = candidates or [
        {
            "candidate_id": "c1",
            "file_path": "backend/core/foo.py",
            "full_content": "x = 1\n",
            "rationale": "simple assignment",
        }
    ]
    payload = {
        "schema_version": "2b.1",
        "candidates": cands,
        "provider_metadata": {"model_id": "llama-3", "reasoning_summary": "test"},
    }
    if extras:
        payload.update(extras)
    return json.dumps(payload)


# ── Schema version ────────────────────────────────────────────────────────

def test_parse_rejects_missing_schema_version():
    raw = json.dumps({"candidates": [], "provider_metadata": {}})
    with pytest.raises(RuntimeError, match="wrong_schema_version"):
        _parse_generation_response(raw, "test", 0.1)


def test_parse_rejects_wrong_schema_version():
    raw = json.dumps({"schema_version": "1.0", "candidates": [], "provider_metadata": {}})
    with pytest.raises(RuntimeError, match="wrong_schema_version:1.0"):
        _parse_generation_response(raw, "test", 0.1)


# ── Extra keys — strict reject ────────────────────────────────────────────

def test_parse_rejects_extra_top_level_keys():
    raw = _valid_payload(extras={"unexpected_field": "bad"})
    with pytest.raises(RuntimeError, match="unexpected_keys"):
        _parse_generation_response(raw, "test", 0.1)


def test_parse_rejects_extra_candidate_keys():
    cands = [
        {
            "candidate_id": "c1",
            "file_path": "f.py",
            "full_content": "x=1\n",
            "rationale": "r",
            "extra_field": "bad",  # not allowed
        }
    ]
    raw = _valid_payload(candidates=cands)
    with pytest.raises(RuntimeError, match="unexpected_keys"):
        _parse_generation_response(raw, "test", 0.1)


# ── Candidate count ───────────────────────────────────────────────────────

def test_parse_rejects_empty_candidates():
    raw = _valid_payload(candidates=[])
    with pytest.raises(RuntimeError, match="candidates_empty"):
        _parse_generation_response(raw, "test", 0.1)


def test_parse_truncates_more_than_3_candidates(caplog):
    cands = [
        {"candidate_id": f"c{i}", "file_path": "f.py", "full_content": "x=1\n", "rationale": "r"}
        for i in range(1, 6)
    ]
    raw = _valid_payload(candidates=cands)
    result = _parse_generation_response(raw, "test", 0.1)
    assert len(result.candidates) == 3


# ── Required candidate fields ─────────────────────────────────────────────

def test_parse_rejects_missing_candidate_id():
    cands = [{"file_path": "f.py", "full_content": "x=1\n", "rationale": "r"}]
    raw = _valid_payload(candidates=cands)
    with pytest.raises(RuntimeError, match="missing_candidate_id"):
        _parse_generation_response(raw, "test", 0.1)


def test_parse_rejects_missing_full_content():
    cands = [{"candidate_id": "c1", "file_path": "f.py", "rationale": "r"}]
    raw = _valid_payload(candidates=cands)
    with pytest.raises(RuntimeError, match="missing_full_content"):
        _parse_generation_response(raw, "test", 0.1)


# ── AST validation — maps to failure_class="build" ───────────────────────

def test_parse_skips_python_candidate_with_syntax_error():
    cands = [
        {"candidate_id": "c1", "file_path": "bad.py", "full_content": "def f(:\n", "rationale": "r"},
        {"candidate_id": "c2", "file_path": "good.py", "full_content": "x = 1\n", "rationale": "r"},
    ]
    raw = _valid_payload(candidates=cands)
    result = _parse_generation_response(raw, "test", 0.1)
    # c1 skipped due to SyntaxError; c2 included
    assert len(result.candidates) == 1
    assert result.candidates[0]["candidate_id"] == "c2"


def test_parse_raises_if_all_candidates_have_syntax_errors():
    cands = [
        {"candidate_id": f"c{i}", "file_path": "bad.py", "full_content": "def f(:\n", "rationale": "r"}
        for i in range(1, 3)
    ]
    raw = _valid_payload(candidates=cands)
    with pytest.raises(RuntimeError, match="all_candidates_syntax_error"):
        _parse_generation_response(raw, "test", 0.1)


# ── Candidate hash + provenance ───────────────────────────────────────────

def test_parse_adds_candidate_hash():
    raw = _valid_payload()
    result = _parse_generation_response(raw, "test", 0.1)
    c = result.candidates[0]
    expected_hash = hashlib.sha256(c["full_content"].encode()).hexdigest()
    assert c["candidate_hash"] == expected_hash


def test_parse_sets_model_id_on_result():
    raw = _valid_payload()
    result = _parse_generation_response(raw, "test", 0.1)
    assert result.model_id == "llama-3"


# ── Valid round-trip ──────────────────────────────────────────────────────

def test_parse_valid_payload_returns_generation_result():
    from backend.core.ouroboros.governance.op_context import GenerationResult
    raw = _valid_payload()
    result = _parse_generation_response(raw, "gcp-jprime", 1.23)
    assert isinstance(result, GenerationResult)
    assert result.provider_name == "gcp-jprime"
    assert result.generation_duration_s == 1.23
    assert len(result.candidates) == 1
    assert result.candidates[0]["candidate_id"] == "c1"
```

### Step 2: Run — expect FAIL

```bash
python3 -m pytest tests/governance/self_dev/test_candidate_parser.py -v 2>&1 | head -30
```
Expected: most tests FAIL — parser doesn't yet validate schema_version, extra keys, etc.

### Step 3: Rewrite `_parse_generation_response` in `providers.py`

Replace the existing `_parse_generation_response` function (lines 110-193) with:

```python
# Allowed top-level keys in schema_version 2b.1
_SCHEMA_TOP_LEVEL_KEYS = frozenset({"schema_version", "candidates", "provider_metadata"})
# Allowed per-candidate keys
_CANDIDATE_KEYS = frozenset({"candidate_id", "file_path", "full_content", "rationale"})
# failure_class for SyntaxError in candidate
_PARSE_FAILURE_CLASS = "build"   # SyntaxError = build failure, not "parse"


def _parse_generation_response(
    raw: str,
    provider_name: str,
    duration_s: float,
    source_hash: str = "",
    source_path: str = "",
) -> "GenerationResult":
    """Parse and validate a model JSON response into a GenerationResult (schema 2b.1).

    Raises RuntimeError with deterministic reason code on any validation failure.
    """
    import ast as _ast
    import hashlib as _hashlib
    import logging as _logging

    _log = _logging.getLogger(__name__)

    def _fail(detail: str) -> RuntimeError:
        return RuntimeError(f"{provider_name}_schema_invalid:{detail}")

    # 1. JSON parse
    json_str = _extract_json_block(raw)
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _fail("json_parse_error") from exc

    if not isinstance(data, dict):
        raise _fail("expected_object")

    # 2. schema_version
    actual_version = data.get("schema_version", "<missing>")
    if actual_version != _SCHEMA_VERSION:
        raise _fail(f"wrong_schema_version:{actual_version}")

    # 3. Extra top-level keys — strict reject
    extra_keys = set(data.keys()) - _SCHEMA_TOP_LEVEL_KEYS
    if extra_keys:
        raise _fail(f"unexpected_keys:{','.join(sorted(extra_keys))}")

    # 4. candidates array
    candidates_raw = data.get("candidates")
    if not candidates_raw:
        raise _fail("candidates_empty")
    if not isinstance(candidates_raw, list):
        raise _fail("candidates_not_array")

    # 5. >3 candidates — ledger normalization event, keep first 3
    if len(candidates_raw) > 3:
        dropped = [c.get("candidate_id", f"c{i+4}") for i, c in enumerate(candidates_raw[3:])]
        _log.warning(
            "[Phase2B] Model returned %d candidates; normalizing to 3. Dropped: %s",
            len(candidates_raw), dropped,
        )
        # Caller should also write a ledger entry; we log here for traceability
        candidates_raw = candidates_raw[:3]

    # 6. Per-candidate validation
    validated: list = []
    for i, cand in enumerate(candidates_raw):
        if not isinstance(cand, dict):
            raise _fail(f"candidate_{i}_not_object")

        # Extra candidate keys — strict reject
        extra_cand_keys = set(cand.keys()) - _CANDIDATE_KEYS
        if extra_cand_keys:
            raise _fail(f"candidate_{i}_unexpected_keys:{','.join(sorted(extra_cand_keys))}")

        # Required fields
        for field in ("candidate_id", "file_path", "full_content", "rationale"):
            val = cand.get(field)
            if not val or not isinstance(val, str):
                raise _fail(f"missing_{field}")  # e.g., missing_candidate_id

        file_path: str = cand["file_path"]
        full_content: str = cand["full_content"]

        # AST validation for Python files — skip candidate (failure_class="build"), continue
        if file_path.endswith(".py"):
            try:
                _ast.parse(full_content)
            except SyntaxError as exc:
                _log.warning(
                    "[Phase2B] Candidate %s failed AST check (%s); skipping (failure_class=build)",
                    cand["candidate_id"], exc,
                )
                continue  # skip this candidate, try next

        # Compute candidate_hash
        candidate_hash = _hashlib.sha256(full_content.encode()).hexdigest()

        validated.append({
            "candidate_id":   cand["candidate_id"],
            "file_path":      file_path,
            "full_content":   full_content,
            "rationale":      cand["rationale"],
            "candidate_hash": candidate_hash,
            "source_hash":    source_hash,    # hash of original file at generation time
            "source_path":    source_path,    # path of original file
        })

    if not validated:
        raise _fail("all_candidates_syntax_error")

    # 7. Extract provider metadata
    meta = data.get("provider_metadata") or {}
    model_id = meta.get("model_id", "") if isinstance(meta, dict) else ""

    return GenerationResult(
        candidates=tuple(validated),
        provider_name=provider_name,
        generation_duration_s=duration_s,
        model_id=model_id,
    )
```

Also update `PrimeProvider.generate()` and `ClaudeProvider.generate()` to pass `source_hash` and `source_path` to `_parse_generation_response`. Extract hash from the first target file before calling the API. Pattern:

```python
# In PrimeProvider.generate() and ClaudeProvider.generate(),
# before calling _parse_generation_response:
import hashlib as _hl
source_hash = ""
source_path = ""
if context.target_files:
    fp = (self._repo_root / context.target_files[0]) if self._repo_root else Path(context.target_files[0])
    try:
        raw_content = fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""
        source_hash = _hl.sha256(raw_content.encode()).hexdigest()
        source_path = context.target_files[0]
    except OSError:
        pass

result = _parse_generation_response(
    response.content, provider_name, duration, source_hash=source_hash, source_path=source_path
)
```

### Step 4: Run tests — expect PASS

```bash
python3 -m pytest tests/governance/self_dev/test_candidate_parser.py -v
```
Expected: all tests pass

### Step 5: Full suite

```bash
python3 -m pytest tests/governance/ -q 2>&1 | tail -5
```
Expected: 328+ passed

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/providers.py \
        tests/governance/self_dev/test_candidate_parser.py
git commit -m "feat(ouroboros): strict schema_version 2b.1 parser — reject extra keys, AST→build failure, candidate_hash, source provenance"
```

---

## Task 4: Connectivity preflight in `governed_loop_service.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (`submit()` ~line 289)
- Test: `tests/governance/self_dev/test_preflight.py`

### Step 1: Write the failing tests

```python
# tests/governance/self_dev/test_preflight.py
"""Tests for Phase 2B connectivity preflight in GovernedLoopService.submit()."""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_service(fsm_state_name="PRIMARY_READY", primary_healthy=True):
    """Build a GovernedLoopService with controlled FSM and health probe."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )

    config = GovernedLoopConfig(project_root=REPO_ROOT)
    mock_stack = MagicMock()
    mock_stack.ledger.append = AsyncMock()
    mock_stack.comm.emit_heartbeat = AsyncMock()

    svc = GovernedLoopService(stack=mock_stack, prime_client=None, config=config)

    # Wire a mock generator with controllable FSM
    mock_fsm = MagicMock()
    mock_fsm.state = MagicMock()
    mock_fsm.state.name = fsm_state_name

    mock_primary = MagicMock()
    mock_primary.health_probe = AsyncMock(return_value=primary_healthy)

    mock_generator = MagicMock()
    mock_generator.fsm = mock_fsm
    mock_generator.primary = mock_primary

    svc._generator = mock_generator
    svc._state = MagicMock()
    svc._state.name = "ACTIVE"
    svc._in_flight_ops = {}
    svc._completed_op_ids = set()
    svc._orchestrator = MagicMock()
    svc._orchestrator.run = AsyncMock(return_value=MagicMock(
        phase=OperationPhase.COMPLETE,
    ))
    svc._approval_provider = MagicMock()

    return svc, mock_stack


def _ctx():
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="test preflight",
    )


async def test_preflight_healthy_primary_proceeds():
    """Healthy primary → pipeline runs normally."""
    svc, _ = _make_service(primary_healthy=True)
    result = await svc.submit(_ctx())
    svc._orchestrator.run.assert_called_once()


async def test_preflight_queue_only_cancels_without_running_pipeline():
    """QUEUE_ONLY FSM state → early CANCELLED, orchestrator never called."""
    svc, stack = _make_service(fsm_state_name="QUEUE_ONLY", primary_healthy=False)
    result = await svc.submit(_ctx())
    svc._orchestrator.run.assert_not_called()
    # Ledger must have an entry with reason_code=provider_unavailable
    ledger_calls = stack.ledger.append.call_args_list
    assert any(
        "provider_unavailable" in str(call) for call in ledger_calls
    ), f"No provider_unavailable ledger entry: {ledger_calls}"


async def test_preflight_primary_fail_with_fallback_continues():
    """Primary fail + fallback (non-QUEUE_ONLY) → pipeline continues."""
    svc, stack = _make_service(fsm_state_name="FALLBACK_ACTIVE", primary_healthy=False)
    result = await svc.submit(_ctx())
    svc._orchestrator.run.assert_called_once()
    # Ledger should have informational entry for primary_unavailable_fallback_active
    ledger_calls = [str(c) for c in stack.ledger.append.call_args_list]
    assert any("primary_unavailable_fallback_active" in c for c in ledger_calls), \
        f"No primary_unavailable_fallback_active ledger: {ledger_calls}"


async def test_preflight_budget_exhausted_before_generation():
    """Budget < MIN_GENERATION_BUDGET_S after stamp → CANCELLED pre-generation."""
    from backend.core.ouroboros.governance.governed_loop_service import MIN_GENERATION_BUDGET_S

    svc, stack = _make_service(primary_healthy=True)
    # Override pipeline_timeout_s to be less than minimum budget
    svc._config = svc._config.__class__(
        project_root=REPO_ROOT,
        pipeline_timeout_s=MIN_GENERATION_BUDGET_S - 1.0,
    )
    result = await svc.submit(_ctx())
    svc._orchestrator.run.assert_not_called()
    ledger_calls = [str(c) for c in stack.ledger.append.call_args_list]
    assert any("budget_exhausted_pre_generation" in c for c in ledger_calls)
```

### Step 2: Run — expect FAIL

```bash
python3 -m pytest tests/governance/self_dev/test_preflight.py -v 2>&1 | head -20
```
Expected: FAIL — no `MIN_GENERATION_BUDGET_S`, preflight not implemented

### Step 3: Update `governed_loop_service.py`

**Add module-level constant** (near other imports, top of file):

```python
MIN_GENERATION_BUDGET_S: float = float(
    __import__("os").getenv("JARVIS_MIN_GENERATION_BUDGET_S", "30.0")
)
```

**Update `submit()` method** (around lines 340-343). Replace the deadline-stamp block and the `await self._orchestrator.run(ctx)` call with:

```python
# ── Stamp pipeline_deadline (single budget owner) ──────────────────────
ctx = ctx.with_pipeline_deadline(
    datetime.now(tz=timezone.utc) + timedelta(seconds=self._config.pipeline_timeout_s)
)

# ── Budget pre-check ───────────────────────────────────────────────────
remaining_s = (ctx.pipeline_deadline - datetime.now(tz=timezone.utc)).total_seconds()
if remaining_s < MIN_GENERATION_BUDGET_S:
    ctx = ctx.advance(OperationPhase.CANCELLED)
    await self._stack.ledger.append(LedgerEntry(
        op_id=ctx.op_id,
        phase=ctx.phase.value,
        state=OperationState.FAILED,
        data={"reason_code": "budget_exhausted_pre_generation",
              "remaining_s": remaining_s,
              "min_required_s": MIN_GENERATION_BUDGET_S},
    ))
    return OperationResult(
        op_id=ctx.op_id,
        terminal_phase=ctx.phase,
        provider_used=None,
        generation_duration_s=0.0,
        total_duration_s=0.0,
    )

# ── Connectivity preflight (spends from deadline budget) ───────────────
if self._generator is not None:
    probe_timeout = min(5.0, remaining_s * 0.05)
    primary_ok = False
    try:
        primary_ok = await asyncio.wait_for(
            self._generator.primary.health_probe(),
            timeout=probe_timeout,
        )
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("[GovernedLoop] Preflight probe failed: %s", exc)

    if not primary_ok:
        if self._generator.fsm.state.name == "QUEUE_ONLY":
            ctx = ctx.advance(OperationPhase.CANCELLED)
            await self._stack.ledger.append(LedgerEntry(
                op_id=ctx.op_id,
                phase=ctx.phase.value,
                state=OperationState.FAILED,
                data={"reason_code": "provider_unavailable"},
            ))
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=ctx.phase,
                provider_used=None,
                generation_duration_s=0.0,
                total_duration_s=0.0,
            )
        else:
            # Fallback active — continue, log informational entry
            logger.warning("[GovernedLoop] Primary unhealthy; FSM routes to fallback")
            await self._stack.ledger.append(LedgerEntry(
                op_id=ctx.op_id,
                phase=ctx.phase.value,
                state=OperationState.BLOCKED,
                data={"reason_code": "primary_unavailable_fallback_active",
                      "fsm_state": self._generator.fsm.state.name},
            ))

terminal_ctx = await self._orchestrator.run(ctx)
```

Make sure `LedgerEntry`, `OperationState`, `OperationResult`, `OperationPhase`, `asyncio` are imported at the top of the file.

### Step 4: Run tests — expect PASS

```bash
python3 -m pytest tests/governance/self_dev/test_preflight.py -v
```
Expected: all tests pass

### Step 5: Full suite

```bash
python3 -m pytest tests/governance/ -q 2>&1 | tail -5
```
Expected: 328+ passed

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/self_dev/test_preflight.py
git commit -m "feat(ouroboros): connectivity preflight in submit() — budget pre-check, QUEUE_ONLY cancel, primary-fail+fallback continue"
```

---

## Task 5: Per-candidate ledger + new candidate keys in `orchestrator.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (VALIDATE phase ~lines 252-340, `_run_validation` ~line 515)
- Test: `tests/governance/self_dev/test_candidate_ledger.py`

### Step 1: Write the failing tests

```python
# tests/governance/self_dev/test_candidate_ledger.py
"""Tests for per-candidate ledger entries and new candidate key names."""
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.test_runner import AdapterResult, MultiAdapterResult, TestResult

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_candidate(candidate_id="c1", content="x = 1\n", file_path="backend/core/foo.py"):
    import hashlib
    full = content
    return {
        "candidate_id": candidate_id,
        "file_path": file_path,
        "full_content": full,
        "rationale": "test candidate",
        "candidate_hash": hashlib.sha256(full.encode()).hexdigest(),
        "source_hash": "abc123",
        "source_path": file_path,
    }


def _make_multi(passed, failure_class=None):
    fc = failure_class if failure_class else "none"
    ar = AdapterResult(
        adapter="python", passed=passed,
        failure_class=fc if not passed else "none",
        test_result=TestResult(
            passed=passed, total=1 if passed else 0, failed=0 if passed else 1,
            failed_tests=() if passed else ("test_foo",),
            duration_seconds=0.1, stdout="", flake_suspected=False,
        ),
        duration_s=0.1,
    )
    dominant = None if passed else ar
    return MultiAdapterResult(
        adapter_results=(ar,), passed=passed,
        dominant_failure=dominant,
        failure_class=failure_class, total_duration_s=0.1,
    )


def _make_orch(runner):
    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_stack.can_write.return_value = (True, "")

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(return_value=MagicMock(
        candidates=(_make_candidate("c1"),),
        provider_name="test", generation_duration_s=0.1, model_id="llama-3",
    ))

    config = OrchestratorConfig(project_root=REPO_ROOT, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack, generator=mock_gen,
        approval_provider=MagicMock(), config=config,
        validation_runner=runner,
    )
    return orch, mock_ledger


def _ctx():
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="test",
        pipeline_deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=300),
    )


async def test_per_candidate_ledger_entry_on_pass():
    """Passing candidate → ledger entry with candidate_id and outcome=pass."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=True))
    orch, ledger = _make_orch(runner)
    await orch.run(_ctx())

    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("candidate_validated" in e for e in entries), f"No candidate_validated entry: {entries}"
    assert any("c1" in e for e in entries)
    assert any("pass" in e for e in entries)


async def test_per_candidate_ledger_entry_on_fail():
    """Failing candidate → ledger entry with failure_class recorded."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=False, failure_class="test"))
    orch, ledger = _make_orch(runner)
    await orch.run(_ctx())

    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("candidate_validated" in e for e in entries)
    assert any("test" in e for e in entries)


async def test_no_candidate_valid_ledger_reason():
    """All candidates fail → CANCELLED with reason=no_candidate_valid in ledger."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=False, failure_class="test"))
    orch, ledger = _make_orch(runner)
    terminal = await orch.run(_ctx())
    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("no_candidate_valid" in e for e in entries)


async def test_run_validation_uses_file_path_and_full_content_keys():
    """_run_validation reads file_path and full_content, not old 'file'/'content' keys."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=True))
    orch, _ = _make_orch(runner)

    candidate = _make_candidate("c1", "x = 42\n")
    ctx = _ctx()
    result = await orch._run_validation(ctx, candidate, remaining_s=60.0)
    assert result.passed is True
    runner.run.assert_called_once()
```

### Step 2: Run — expect FAIL

```bash
python3 -m pytest tests/governance/self_dev/test_candidate_ledger.py -v 2>&1 | head -20
```
Expected: FAIL — `_run_validation` still uses `candidate["file"]` / `candidate["content"]`; no per-candidate ledger entries

### Step 3: Update `orchestrator.py`

**A. Update `_run_validation()` (~line 515) to use new candidate keys:**

Find all occurrences of `candidate["file"]` and `candidate["content"]` inside `_run_validation` and replace:
- `candidate["file"]` → `candidate["file_path"]`
- `candidate["content"]` → `candidate["full_content"]`

There are two spots:
1. Where the sandbox file is written: uses `candidate["content"]` to get the text
2. Where `changed_files` is built: uses `candidate["file"]` for the path

**B. Add per-candidate ledger entries to the VALIDATE loop (~lines 276-334):**

In the VALIDATE phase loop, after calling `_run_validation()`, add a ledger entry before the `if validation.passed:` check. Also add a no-winner entry at the end.

The VALIDATE loop structure after changes:

```python
for candidate in generation.candidates:
    t_start = time.monotonic()
    remaining_s = (
        (ctx.pipeline_deadline - datetime.now(tz=timezone.utc)).total_seconds()
        if ctx.pipeline_deadline else self._config.validation_timeout_s
    )

    if remaining_s <= 0.0:
        await self._record_ledger(ctx, OperationState.FAILED, {
            "event": "candidate_validated",
            "candidate_id": candidate.get("candidate_id", "unknown"),
            "candidate_hash": candidate.get("candidate_hash", ""),
            "validation_outcome": "skip",
            "failure_class": "budget",
            "duration_s": 0.0,
            "provider": generation.provider_name,
            "model": generation.model_id,
        })
        break

    validation = await self._run_validation(ctx, candidate, remaining_s)
    duration_s = time.monotonic() - t_start

    # Per-candidate ledger entry — always, pass or fail
    await self._record_ledger(ctx, OperationState.APPLIED, {
        "event": "candidate_validated",
        "candidate_id": candidate.get("candidate_id", "unknown"),
        "candidate_hash": candidate.get("candidate_hash", ""),
        "validation_outcome": "pass" if validation.passed else "fail",
        "failure_class": validation.failure_class,
        "duration_s": round(duration_s, 3),
        "provider": generation.provider_name,
        "model": generation.model_id,
    })

    if validation.passed:
        best_candidate = candidate
        break

    if validation.failure_class == "infra":
        ctx = ctx.advance(OperationPhase.POSTMORTEM)
        await self._record_ledger(ctx, OperationState.FAILED, {
            "reason": "validation_infra_failure",
            "failure_class": "infra",
        })
        return ctx

    if validation.failure_class == "budget":
        ctx = ctx.advance(OperationPhase.CANCELLED)
        await self._record_ledger(ctx, OperationState.FAILED, {
            "reason": "validation_budget_exhausted",
        })
        return ctx

    best_validation = validation  # track for no-winner path

# No winner after all candidates
if best_candidate is None:
    ctx = ctx.advance(OperationPhase.CANCELLED)
    await self._record_ledger(ctx, OperationState.FAILED, {
        "reason": "no_candidate_valid",
        "candidates_tried": [c.get("candidate_id", "?") for c in generation.candidates],
    })
    return ctx
```

Use `OperationState.APPLIED` for the informational per-candidate entry (it's a non-terminal state recording an event). Check existing uses of `OperationState` in the file to pick the best non-terminal state — `GATING` or `APPLIED` depending on convention.

### Step 4: Run tests — expect PASS

```bash
python3 -m pytest tests/governance/self_dev/test_candidate_ledger.py -v
```
Expected: all pass

### Step 5: Full suite — no regressions

```bash
python3 -m pytest tests/governance/ -q 2>&1 | tail -5
```
Expected: 328+ passed

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/governance/self_dev/test_candidate_ledger.py
git commit -m "feat(ouroboros): per-candidate ledger entries + update _run_validation to file_path/full_content keys + no_candidate_valid terminal"
```

---

## Task 6: Source-drift check + winner traceability in `orchestrator.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (GATE phase ~line 342, `_build_change_request` ~line 651)
- Test: `tests/governance/self_dev/test_source_drift.py`

### Step 1: Write the failing tests

```python
# tests/governance/self_dev/test_source_drift.py
"""Tests for source-drift check and winner traceability."""
import hashlib
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import (
    OperationContext, OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.test_runner import AdapterResult, MultiAdapterResult, TestResult

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_multi_pass():
    ar = AdapterResult(
        adapter="python", passed=True, failure_class="none",
        test_result=TestResult(passed=True, total=1, failed=0,
                               failed_tests=(), duration_seconds=0.1, stdout="", flake_suspected=False),
        duration_s=0.1,
    )
    return MultiAdapterResult(adapter_results=(ar,), passed=True,
                              dominant_failure=None, failure_class=None, total_duration_s=0.1)


def _make_orch_with_drift(source_hash_matches: bool, tmp_path: Path):
    """Build orchestrator where the target file's current hash may or may not match."""
    target_file = tmp_path / "foo.py"
    current_content = "x = 1\n"
    target_file.write_text(current_content)
    current_hash = hashlib.sha256(current_content.encode()).hexdigest()

    # The candidate's source_hash either matches or was set from a different version
    candidate_source_hash = current_hash if source_hash_matches else "deadbeef" * 8

    import hashlib as _hl
    full_content = "x = 2\n"  # the new content
    candidate = {
        "candidate_id": "c1",
        "file_path": "foo.py",
        "full_content": full_content,
        "rationale": "increment x",
        "candidate_hash": _hl.sha256(full_content.encode()).hexdigest(),
        "source_hash": candidate_source_hash,
        "source_path": "foo.py",
    }

    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_stack.can_write.return_value = (True, "")

    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi_pass())

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(return_value=MagicMock(
        candidates=(candidate,),
        provider_name="test", generation_duration_s=0.1, model_id="llama-3",
    ))

    config = OrchestratorConfig(project_root=tmp_path, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack, generator=mock_gen,
        approval_provider=MagicMock(), config=config,
        validation_runner=runner,
    )
    return orch, mock_ledger, target_file


def _ctx(tmp_path):
    return OperationContext.create(
        target_files=("foo.py",),
        description="increment x",
        pipeline_deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=300),
    )


async def test_source_drift_cancels_op(tmp_path):
    """File changed since generation → CANCELLED with source_drift_detected."""
    orch, ledger, target_file = _make_orch_with_drift(source_hash_matches=False, tmp_path=tmp_path)
    terminal = await orch.run(_ctx(tmp_path))
    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("source_drift_detected" in e for e in entries), \
        f"No source_drift_detected in ledger: {entries}"


async def test_no_source_drift_proceeds_to_apply(tmp_path):
    """File unchanged → drift check passes, pipeline continues past GATE."""
    orch, ledger, _ = _make_orch_with_drift(source_hash_matches=True, tmp_path=tmp_path)
    terminal = await orch.run(_ctx(tmp_path))
    # Should proceed past CANCELLED (to COMPLETE or wherever the mock ends)
    assert terminal.phase != OperationPhase.CANCELLED or \
           not any("source_drift_detected" in str(c) for c in ledger.append.call_args_list)


async def test_winner_ledger_contains_candidate_id_and_hash(tmp_path):
    """Winning candidate's id and hash appear in the validation_complete ledger entry."""
    orch, ledger, _ = _make_orch_with_drift(source_hash_matches=True, tmp_path=tmp_path)
    await orch.run(_ctx(tmp_path))
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("validation_complete" in e for e in entries), \
        f"No validation_complete entry: {entries}"
    assert any("winning_candidate_id" in e for e in entries)
    assert any("winning_candidate_hash" in e for e in entries)
```

### Step 2: Run — expect FAIL

```bash
python3 -m pytest tests/governance/self_dev/test_source_drift.py -v 2>&1 | head -20
```
Expected: FAIL — no drift check or winner traceability implemented

### Step 3: Add source-drift check and winner traceability in `orchestrator.py`

**A. Add a source-drift check helper (~line 500, before `_ast_preflight`):**

```python
@staticmethod
def _check_source_drift(
    candidate: Dict[str, Any],
    project_root: Path,
) -> Optional[str]:
    """Return None if file hash matches source_hash in candidate, else return current hash."""
    import hashlib as _hl
    source_hash = candidate.get("source_hash", "")
    if not source_hash:
        return None  # no hash recorded at generation time — skip check
    file_path = project_root / candidate.get("file_path", "")
    try:
        current_content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # file not found — let APPLY handle it
    current_hash = _hl.sha256(current_content.encode()).hexdigest()
    return current_hash if current_hash != source_hash else None
```

**B. After VALIDATE succeeds and before advancing to GATE (~line 340), add drift check + winner traceability:**

```python
# Source-drift check: verify file hasn't changed since generation
drift_hash = self._check_source_drift(best_candidate, self._config.project_root)
if drift_hash is not None:
    ctx = ctx.advance(OperationPhase.CANCELLED)
    await self._record_ledger(ctx, OperationState.FAILED, {
        "reason_code": "source_drift_detected",
        "file_path": best_candidate.get("file_path"),
        "expected_source_hash": best_candidate.get("source_hash"),
        "actual_source_hash": drift_hash,
    })
    return ctx

# Winner traceability ledger entry
await self._record_ledger(ctx, OperationState.GATING, {
    "event":                  "validation_complete",
    "winning_candidate_id":   best_candidate.get("candidate_id"),
    "winning_candidate_hash": best_candidate.get("candidate_hash"),
    "winning_file_path":      best_candidate.get("file_path"),
    "source_hash":            best_candidate.get("source_hash"),
    "source_path":            best_candidate.get("source_path"),
    "provider":               generation.provider_name,
    "model":                  generation.model_id,
    "total_candidates_tried": len(generation.candidates),
})

ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)
```

**C. Update `_build_change_request()` (~line 651) to use new candidate keys:**

Find `candidate["file"]` → `candidate["file_path"]`
Find `candidate["content"]` → `candidate["full_content"]`

Also add `candidate_id` and `candidate_hash` to the metadata passed to `ChangeRequest`:

```python
# In _build_change_request, add to ChangeRequest construction:
# (check the ChangeRequest dataclass signature first)
# Pass candidate_id and candidate_hash into the profile or as metadata
```

### Step 4: Run tests — expect PASS

```bash
python3 -m pytest tests/governance/self_dev/test_source_drift.py -v
```
Expected: all pass

### Step 5: Full suite

```bash
python3 -m pytest tests/governance/ -q 2>&1 | tail -5
```
Expected: 328+ passed

### Step 6: Pyright check on all modified files

```bash
python3 -m pyright \
  backend/core/ouroboros/governance/op_context.py \
  backend/core/ouroboros/governance/providers.py \
  backend/core/ouroboros/governance/orchestrator.py \
  backend/core/ouroboros/governance/governed_loop_service.py \
  2>&1 | tail -5
```
Expected: 0 errors

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/governance/self_dev/test_source_drift.py
git commit -m "feat(ouroboros): source-drift check pre-APPLY + winner traceability ledger entry — candidate_id/hash in validation_complete"
```

---

## Task 7: Acceptance tests — full Phase 2B pipeline

**Files:**
- Create: `tests/governance/integration/test_phase2b_acceptance.py`

### Step 1: Write the acceptance tests

```python
# tests/governance/integration/test_phase2b_acceptance.py
"""
Phase 2B acceptance tests: enriched generation + multi-candidate sequential validation.

Covers all Phase 2B guarantees end-to-end.
"""
import hashlib
import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.op_context import (
    OperationContext, OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.test_runner import AdapterResult, MultiAdapterResult, TestResult

REPO_ROOT = Path(__file__).resolve().parents[3]


def _candidate(cid, content="x = 1\n", file_path="backend/core/foo.py",
               source_hash=None):
    full = content
    ch = hashlib.sha256(full.encode()).hexdigest()
    sh = source_hash or hashlib.sha256(b"original").hexdigest()
    return {
        "candidate_id": cid,
        "file_path": file_path,
        "full_content": full,
        "rationale": f"approach {cid}",
        "candidate_hash": ch,
        "source_hash": sh,
        "source_path": file_path,
    }


def _multi(passed, fc=None):
    fc_val = fc or ("none" if passed else "test")
    ar = AdapterResult(
        adapter="python", passed=passed,
        failure_class="none" if passed else fc_val,
        test_result=TestResult(
            passed=passed, total=1 if passed else 0,
            failed=0 if passed else 1,
            failed_tests=() if passed else ("test_x",),
            duration_seconds=0.1, stdout="", flake_suspected=False,
        ),
        duration_s=0.1,
    )
    return MultiAdapterResult(
        adapter_results=(ar,), passed=passed,
        dominant_failure=None if passed else ar,
        failure_class=None if passed else fc_val,
        total_duration_s=0.1,
    )


def _make_orch(candidates, runner, project_root=REPO_ROOT):
    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_stack.can_write.return_value = (True, "")

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(return_value=MagicMock(
        candidates=tuple(candidates),
        provider_name="gcp-jprime",
        generation_duration_s=0.5,
        model_id="llama-3.3-70b",
    ))

    config = OrchestratorConfig(project_root=project_root, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack, generator=mock_gen,
        approval_provider=MagicMock(), config=config,
        validation_runner=runner,
    )
    return orch, mock_ledger


def _ctx(project_root=REPO_ROOT):
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="improve foo",
        pipeline_deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=300),
    )


# ── AC1: First passing candidate wins, stops immediately ─────────────────

async def test_first_passing_candidate_wins():
    """c1 passes → pipeline uses c1, c2 never validated."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2"), _candidate("c3")],
        runner,
    )
    terminal = await orch.run(_ctx())
    # Runner called exactly once (c1 passed, c2 and c3 skipped)
    assert runner.run.call_count == 1


async def test_c1_fails_c2_passes_c3_not_tried():
    """c1 fails tests, c2 passes → c3 never validated."""
    call_count = 0
    async def mock_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _multi(passed=(call_count == 2))  # c1=fail, c2=pass

    runner = MagicMock()
    runner.run = mock_run
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2"), _candidate("c3")],
        runner,
    )
    await orch.run(_ctx())
    assert call_count == 2  # c1 + c2, not c3


# ── AC2: No valid candidates → CANCELLED(no_candidate_valid) ─────────────

async def test_all_candidates_fail_produces_no_candidate_valid():
    """All 3 candidates fail → CANCELLED + ledger reason=no_candidate_valid."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, fc="test"))
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2"), _candidate("c3")],
        runner,
    )
    terminal = await orch.run(_ctx())
    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("no_candidate_valid" in e for e in entries)
    assert runner.run.call_count == 3  # all tried


# ── AC3: Per-candidate ledger provenance ─────────────────────────────────

async def test_per_candidate_ledger_has_required_fields():
    """Each validated candidate has a ledger entry with id, hash, outcome, failure_class."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, fc="test"))
    c1 = _candidate("c1")
    orch, ledger = _make_orch([c1], runner)
    await orch.run(_ctx())

    candidate_entries = [
        call.args[0] for call in ledger.append.call_args_list
        if hasattr(call.args[0], "data") and
        call.args[0].data.get("event") == "candidate_validated"
    ]
    assert len(candidate_entries) >= 1
    entry = candidate_entries[0]
    assert entry.data["candidate_id"] == "c1"
    assert "candidate_hash" in entry.data
    assert entry.data["validation_outcome"] in ("pass", "fail")
    assert "failure_class" in entry.data
    assert "duration_s" in entry.data
    assert "provider" in entry.data
    assert "model" in entry.data


# ── AC4: Winner traceability ──────────────────────────────────────────────

async def test_winner_traceability_in_ledger():
    """Passing candidate → ledger entry with winning_candidate_id + winning_candidate_hash."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))
    c1 = _candidate("c1", source_hash=hashlib.sha256(b"x = 1\n").hexdigest())

    # Make project_root point to a temp location where drift check won't trigger
    # by using a source_hash that matches the current file
    orch, ledger = _make_orch([c1], runner)

    # Patch source drift check to always return None (no drift)
    from unittest.mock import patch
    with patch.object(GovernedOrchestrator, "_check_source_drift", return_value=None):
        await orch.run(_ctx())

    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("validation_complete" in e for e in entries)
    assert any("winning_candidate_id" in e for e in entries)
    assert any("c1" in e for e in entries)


# ── AC5: Source drift detection ───────────────────────────────────────────

async def test_source_drift_cancels_before_apply():
    """File drifted since generation → CANCELLED(source_drift_detected) before APPLY."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))

    # source_hash in candidate doesn't match any real file
    c1 = _candidate("c1", source_hash="aabbccdd" * 8)
    orch, ledger = _make_orch([c1], runner)

    # Force the drift check to return a mismatch
    from unittest.mock import patch
    with patch.object(GovernedOrchestrator, "_check_source_drift",
                      return_value="different_hash_than_expected"):
        terminal = await orch.run(_ctx())

    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("source_drift_detected" in e for e in entries)


# ── AC6: op_id continuity ─────────────────────────────────────────────────

async def test_op_id_in_all_candidate_ledger_entries():
    """All candidate_validated ledger entries share the operation's op_id."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, fc="test"))
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2")], runner
    )
    ctx = _ctx()
    await orch.run(ctx)

    for call in ledger.append.call_args_list:
        entry = call.args[0]
        assert entry.op_id == ctx.op_id, f"op_id mismatch: {entry.op_id} != {ctx.op_id}"
```

### Step 2: Run — expect all pass

```bash
python3 -m pytest tests/governance/integration/test_phase2b_acceptance.py -v 2>&1 | tail -20
```
Expected: all 9+ tests pass

### Step 3: Full governance suite — final count

```bash
python3 -m pytest tests/governance/ -q 2>&1 | tail -5
```
Expected: 345+ passed (328 + ~17 new tests across all tasks)

### Step 4: Pyright clean check

```bash
python3 -m pyright \
  backend/core/ouroboros/governance/op_context.py \
  backend/core/ouroboros/governance/providers.py \
  backend/core/ouroboros/governance/orchestrator.py \
  backend/core/ouroboros/governance/governed_loop_service.py \
  2>&1 | tail -5
```
Expected: 0 errors, 0 warnings

### Step 5: Commit

```bash
git add tests/governance/integration/test_phase2b_acceptance.py
git commit -m "test(ouroboros): Phase 2B acceptance tests — multi-candidate sequential, source drift, per-candidate ledger, winner traceability, op_id continuity"
```

---

## Implementation Invariants (must hold after every task)

- `pipeline_deadline` stamped once in `submit()` — never re-stamped in orchestrator
- `failure_class="parse"` never appears — SyntaxError always maps to `"build"`
- Extra keys in LLM response → strict RuntimeError, never silent drop (except >3 candidates → normalize + ledger)
- `source_hash` + `source_path` present in every candidate dict post-parse
- `candidate_hash` = SHA-256 of `full_content` — computed in parser, verified in ledger
- `op_id` identical across all ledger entries for one operation
- No subprocess spawned when `remaining_s <= 0`
- All discovered context files pass `_safe_context_path()` — BlockedPathError on violation

---

## Pyright Verification (run after every task)

```bash
python3 -m pyright backend/core/ouroboros/governance/ 2>&1 | tail -5
```

Pre-existing false positives to ignore: `Import "pytest" could not be resolved`, `Import "backend.*" could not be resolved` — these are venv config issues, not real errors. Only act on NEW errors.
