# Sub-project B: The Eyes (Context Injection Quality)

**Date:** 2026-04-02
**Parent:** [Ouroboros HUD Pipeline Program](2026-04-02-ouroboros-hud-pipeline-program.md)
**Status:** Approved
**Depends on:** Sub-project A (CUExecutionSensor wiring — completed)

## Problem

The Ouroboros code generation prompt truncates target files at 12KB (8KB head + 2KB tail). For `cu_task_planner.py` (32KB, 790 lines), the `_filter_messaging_antipatterns` method at line 609 falls in the truncated middle. The 397B model literally cannot see the function it's asked to modify, causing blind duplication.

The model (Qwen3.5-397B-A17B) supports 262K native context. The 12KB limit is an app-side guard in `providers.py`, not a model constraint. A 32KB file is trivial for this context window.

Additionally, even with full file visibility, the model has no structural index ("what already exists") or temporal context ("what changed recently"), increasing the risk of duplication or conflicting edits.

## Changes

### 1. Env-Driven Target File Budget

**File:** `backend/core/ouroboros/governance/providers.py` (lines 72-78)

Replace hardcoded constants with env-driven values:

```python
_MAX_TARGET_FILE_CHARS = int(os.environ.get("JARVIS_CODEGEN_MAX_FILE_CHARS", "65536"))
_TARGET_FILE_HEAD_CHARS = int(os.environ.get("JARVIS_CODEGEN_HEAD_CHARS", "52000"))
_TARGET_FILE_TAIL_CHARS = int(os.environ.get("JARVIS_CODEGEN_TAIL_CHARS", "8000"))
```

**Overlap guard in `_read_with_truncation()`:** When `max_chars` is tuned down but head/tail stay large, head+tail can exceed content length or max_chars. The function must clamp:

```python
def _read_with_truncation(path: Path, max_chars: int = _MAX_TARGET_FILE_CHARS) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(content) <= max_chars:
        return content
    # Clamp: head_len uses configured HEAD but cannot exceed content-1 or 80% of budget.
    # tail_len uses configured TAIL but cannot exceed remaining content after head.
    # This prevents overlap when max_chars is tuned down while HEAD/TAIL stay large.
    head_len = min(_TARGET_FILE_HEAD_CHARS, max_chars * 4 // 5, len(content) - 1)
    tail_len = min(_TARGET_FILE_TAIL_CHARS, len(content) - head_len)
    head = content[:head_len]
    tail = content[-tail_len:] if tail_len > 0 else ""
    omitted_bytes = len(content.encode()) - len(head.encode()) - len(tail.encode())
    omitted_lines = content.count("\n") - head.count("\n") - tail.count("\n")
    marker = f"\n[TRUNCATED: {omitted_bytes} bytes, {omitted_lines} lines omitted]\n"
    return head + marker + tail
```

Note: The previous `len(content) // 2` guard over-clamped when the file was only slightly larger than `max_chars`. The corrected formula uses `len(content) - 1` for head and `len(content) - head_len` for tail, maximizing coverage while preventing overlap.

Import/test context budgets (`_MAX_IMPORT_CONTEXT_CHARS`, `_MAX_TEST_CONTEXT_CHARS`) stay as-is — they're supplementary.

**Provider cap note:** If the Doubleword batch API imposes an input limit, operators set `JARVIS_CODEGEN_MAX_FILE_CHARS` to respect it. Default 64KB is safe for 262K-token models.

### 2. Anti-Duplication System Prompt

**File:** `backend/core/ouroboros/governance/providers.py` (lines 48-69)

Append to `_CODEGEN_SYSTEM_PROMPT`:

```
"ANTI-DUPLICATION RULES: Before generating code, review the entire source snapshot "
"and the structural index (if provided). Do NOT generate functions, methods, or logic "
"blocks that duplicate or substantially overlap with code already present in the source. "
"If you are asked to add a feature that is already implemented, return a 2b.1-noop "
"response explaining it exists. When adding new code, match the existing code style "
"and patterns from the source snapshot. Make minimal edits — preserve existing behavior "
"and do not refactor code outside the scope of the requested change."
```

The "minimal edit / preserve existing behavior" phrasing prevents the model from interpreting "don't duplicate" as "refuse any change near existing code."

This is deterministic prompt content (a guardrail instruction). Sub-project C adds structural validation in VALIDATE as a hard gate.

### 3. AST Function Index

**File:** `backend/core/ouroboros/governance/providers.py` (new function)

Add `_build_function_index(content: str, file_path: str) -> str`:

1. Python files only (`.py` extension). For non-Python files, return empty string.
2. Parse with `ast.parse(content)`. On `SyntaxError`, return empty string (full source is still injected).
3. Walk AST for `FunctionDef`, `AsyncFunctionDef`, and `ClassDef` nodes at top-level and one level deep (class methods).
4. For each function/method: extract name, `lineno` (skip if None), parameter signature (truncated to 80 chars), first line of docstring (if any).
5. Include `@property`, `@staticmethod`, `@classmethod` decorated methods — they're still methods.
6. Cap: max 50 entries, max 3KB total output. Stop emitting entries if either cap is reached.
7. Per-entry signature truncation: if the `def ...():` line exceeds 100 chars, truncate with `...`.

**Output format:**

```
## Structural Index (what already exists — do NOT duplicate)

- L45 class CUTaskPlanner
  - L102 def plan_goal(self, goal: str, current_frame: bytes) -> List[CUStep]: "Plan CU steps..."
  - L609 def _filter_messaging_antipatterns(steps: List[CUStep]) -> List[CUStep]: "Filter dangerous..."
```

**Injection point:** In `_build_codegen_prompt()`, after the task description section and before the source snapshot section. This gives the model a structural overview before seeing the full code.

### 4. Recent File History

**File:** `backend/core/ouroboros/governance/providers.py` (new function)

Add `_build_recent_file_history(path: Path, repo_root: Path) -> str`:

1. Check `(repo_root / ".git").exists()`. If not, return empty string. No subprocess, no crash.
2. Resolve path relative to repo_root using `path.relative_to(repo_root)`. Catch `ValueError` (path not under repo_root) and return empty string.
3. Run `git log --oneline -5 -- <relative_path>` with:
   - `cwd=repo_root`
   - `timeout=3` seconds (prevent CI/sandbox hangs)
   - `stdout=PIPE, stderr=DEVNULL`
4. On any failure (subprocess error, timeout, non-zero exit), return empty string.
5. Cap output at 500 chars.

**Output format:**

```
## Recent Changes (last 5 commits touching this file)
- a2793028 test(intake): add CUExecutionSensor spine tests
- 7e04a414 feat(intake): wire CUExecutionSensor to router
```

**Injection point:** After the structural index, before the source snapshot.

**Name note:** This is `_build_recent_file_history`, not "git blame" — it's a recent-commit summary, not line-level attribution.

## Prompt Assembly Order (Updated)

After these changes, `_build_codegen_prompt()` assembles:

1. Human Instructions (from `ctx.human_instructions`)
2. **## Task** (op_id + description)
3. ## System Context (optional)
4. Strategic Memory Prompt (optional)
5. **## Structural Index** (NEW — AST function index)
6. **## Recent Changes** (NEW — last 5 commits)
7. ## Source Snapshot (target files — now up to 64KB untruncated)
8. ## Surrounding Context (imports + tests)
9. ## Expanded Context Files (from CONTEXT_EXPANSION)
10. ## Available Tools (optional)
11. ## REPAIR ITERATION (optional)
12. ## Output Schema

## Testing Strategy

### Unit Tests

| Test | What it verifies |
|------|-----------------|
| `test_env_driven_truncation_defaults` | Default 64KB, env override works |
| `test_truncation_overlap_guard` | head+tail clamped when max_chars < head+tail |
| `test_no_truncation_for_small_files` | Files under max_chars returned verbatim |
| `test_function_index_python` | Correct AST extraction with classes, methods, async def |
| `test_function_index_syntax_error` | Returns empty string on unparseable files |
| `test_function_index_non_python` | Returns empty string for non-.py files |
| `test_function_index_caps` | Respects 50-entry and 3KB caps |
| `test_recent_file_history_success` | Returns last 5 commits in correct format |
| `test_recent_file_history_no_git` | Returns empty string when .git missing |
| `test_recent_file_history_timeout` | Returns empty string on subprocess timeout |
| `test_anti_duplication_in_system_prompt` | System prompt contains anti-duplication rules |

### Integration Test

One test that builds a codegen prompt for a 32KB+ file and asserts:
- No `[TRUNCATED]` marker in the source snapshot
- Structural index is present and lists functions
- Anti-duplication rules are in the system prompt

## Files Modified

| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/providers.py` | Env-driven constants, overlap guard, system prompt, new functions |
| `tests/governance/test_codegen_context.py` | New: unit + integration tests for all 4 changes |

## Out of Scope

- Duplication guard in VALIDATE phase (Sub-project C)
- Diff-aware GATE rejection (Sub-project C)
- pytest-based VERIFY (Sub-project C)
- DaemonNarrator wiring (Sub-project D)
- Doubleword batch API input cap verification (operational — check docs separately)
