# Sub-project B: The Eyes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate blind code duplication by giving the Ouroboros code generation brain full file visibility, a structural index of existing functions, anti-duplication instructions, and recent commit history.

**Architecture:** Four changes to `providers.py`: env-driven truncation constants with overlap guard, anti-duplication system prompt, AST function index builder, and recent file history builder. All injected into the existing `_build_codegen_prompt()` assembly pipeline. One new test file covers all four changes.

**Tech Stack:** Python 3.12, ast module, subprocess (for git), pytest

**Spec:** `docs/superpowers/specs/2026-04-02-sub-b-context-injection-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/core/ouroboros/governance/providers.py` | Modify (lines 48-137, 836-889) | Env-driven constants, overlap guard, system prompt, new functions, prompt assembly |
| `tests/governance/test_codegen_context.py` | Create | All unit + integration tests for context injection |

---

### Task 1: Env-driven truncation constants + overlap guard

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py:71-137`
- Test: `tests/governance/test_codegen_context.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_codegen_context.py`:

```python
"""Tests for Ouroboros codegen context injection (Sub-project B)."""
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Task 1: Env-driven truncation + overlap guard
# ---------------------------------------------------------------------------

def test_default_max_target_file_chars():
    """Default should be 65536, not the old hardcoded 12000."""
    from backend.core.ouroboros.governance.providers import _MAX_TARGET_FILE_CHARS
    assert _MAX_TARGET_FILE_CHARS == 65536


def test_env_override_max_target_file_chars(monkeypatch):
    """JARVIS_CODEGEN_MAX_FILE_CHARS env var overrides the default."""
    monkeypatch.setenv("JARVIS_CODEGEN_MAX_FILE_CHARS", "20000")
    # Re-import to pick up env change — use importlib
    import importlib
    import backend.core.ouroboros.governance.providers as mod
    importlib.reload(mod)
    assert mod._MAX_TARGET_FILE_CHARS == 20000
    # Restore original
    monkeypatch.delenv("JARVIS_CODEGEN_MAX_FILE_CHARS", raising=False)
    importlib.reload(mod)


def test_no_truncation_for_small_files(tmp_path):
    """Files under max_chars should be returned verbatim."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    f = tmp_path / "small.py"
    content = "x = 1\n" * 100  # ~600 chars
    f.write_text(content)
    result = _read_with_truncation(f)
    assert result == content
    assert "[TRUNCATED" not in result


def test_truncation_for_large_files(tmp_path):
    """Files over max_chars should be truncated with marker."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    f = tmp_path / "large.py"
    content = "x = 1\n" * 20000  # ~120KB
    f.write_text(content)
    result = _read_with_truncation(f, max_chars=10000)
    assert "[TRUNCATED" in result
    assert len(result) < len(content)


def test_truncation_overlap_guard(tmp_path):
    """When max_chars is small, head+tail must not overlap or exceed content."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    f = tmp_path / "medium.py"
    # 25KB file, but max_chars=20000 with default HEAD=52000, TAIL=8000
    content = "".join(f"line {i}\n" for i in range(3000))  # ~25KB
    f.write_text(content)
    result = _read_with_truncation(f, max_chars=20000)
    assert "[TRUNCATED" in result
    # The result should not be longer than the original (no overlap)
    assert len(result) <= len(content)
    # Head and tail should not overlap (no duplicated lines)
    parts = result.split("[TRUNCATED")
    head_part = parts[0]
    tail_part = parts[1].split("]", 1)[1] if len(parts) > 1 else ""
    # Last line of head should not appear in tail
    head_lines = head_part.strip().split("\n")
    tail_lines = tail_part.strip().split("\n")
    if head_lines and tail_lines:
        assert head_lines[-1] != tail_lines[0], "Head and tail overlap!"


def test_truncation_osError_returns_empty(tmp_path):
    """Non-existent file should return empty string, not raise."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    result = _read_with_truncation(tmp_path / "nonexistent.py")
    assert result == ""
```

- [ ] **Step 2: Run tests to see initial state**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -v -k "test_default or test_no_truncation or test_truncation_for_large or test_truncation_overlap or test_truncation_osError" 2>&1 | tail -20`

Expected: `test_default_max_target_file_chars` FAILS (current value is 12000). Others may pass or fail depending on current truncation behavior.

- [ ] **Step 3: Update constants and fix `_read_with_truncation`**

In `backend/core/ouroboros/governance/providers.py`, replace lines 72-74:

```python
_MAX_TARGET_FILE_CHARS = 12000     # full content below this (increased from 6000)
_TARGET_FILE_HEAD_CHARS = 8000     # head kept on truncation
_TARGET_FILE_TAIL_CHARS = 2000     # tail kept on truncation
```

With:

```python
_MAX_TARGET_FILE_CHARS = int(os.environ.get("JARVIS_CODEGEN_MAX_FILE_CHARS", "65536"))
_TARGET_FILE_HEAD_CHARS = int(os.environ.get("JARVIS_CODEGEN_HEAD_CHARS", "52000"))
_TARGET_FILE_TAIL_CHARS = int(os.environ.get("JARVIS_CODEGEN_TAIL_CHARS", "8000"))
```

Then replace the `_read_with_truncation` function (lines 124-137):

```python
def _read_with_truncation(path: Path, max_chars: int = _MAX_TARGET_FILE_CHARS) -> str:
    """Read file content, applying truncation with an explicit marker if needed."""
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -v -k "Task1 or test_default or test_env or test_no_truncation or test_truncation" 2>&1 | tail -20`
Expected: All Task 1 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py tests/governance/test_codegen_context.py
git commit -m "feat(codegen): env-driven file truncation budget with overlap guard

Default raised from 12KB to 64KB. Overlap guard prevents head+tail
from exceeding content length when max_chars is tuned down.
Env vars: JARVIS_CODEGEN_MAX_FILE_CHARS, _HEAD_CHARS, _TAIL_CHARS.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Anti-duplication system prompt

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py:48-69`
- Test: `tests/governance/test_codegen_context.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/governance/test_codegen_context.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: Anti-duplication system prompt
# ---------------------------------------------------------------------------

def test_system_prompt_contains_anti_duplication():
    """System prompt must contain anti-duplication instructions."""
    from backend.core.ouroboros.governance.providers import _CODEGEN_SYSTEM_PROMPT
    assert "ANTI-DUPLICATION" in _CODEGEN_SYSTEM_PROMPT
    assert "do NOT generate" in _CODEGEN_SYSTEM_PROMPT.lower()
    assert "2b.1-noop" in _CODEGEN_SYSTEM_PROMPT


def test_system_prompt_contains_minimal_edit_guidance():
    """Anti-duplication must include minimal-edit language to avoid over-refusal."""
    from backend.core.ouroboros.governance.providers import _CODEGEN_SYSTEM_PROMPT
    assert "minimal edit" in _CODEGEN_SYSTEM_PROMPT.lower() or "preserve existing" in _CODEGEN_SYSTEM_PROMPT.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_codegen_context.py::test_system_prompt_contains_anti_duplication -v`
Expected: FAIL — "ANTI-DUPLICATION" not in current system prompt

- [ ] **Step 3: Append anti-duplication rules to system prompt**

In `backend/core/ouroboros/governance/providers.py`, in the `_CODEGEN_SYSTEM_PROMPT` string (lines 48-69), add before the `JARVIS_CODEGEN_SYSTEM_PROMPT_EXTRA` conditional:

```python
    '{"schema_version": "2b.1-noop", "reason": "<why already done>"} instead of a diff. '
    # Anti-duplication mandate — prevents blind re-implementation of existing logic
    "ANTI-DUPLICATION RULES: Before generating code, review the entire source snapshot "
    "and the structural index (if provided). Do NOT generate functions, methods, or logic "
    "blocks that duplicate or substantially overlap with code already present in the source. "
    "If you are asked to add a feature that is already implemented, return a 2b.1-noop "
    "response explaining it exists. When adding new code, match the existing code style "
    "and patterns from the source snapshot. Make minimal edits — preserve existing behavior "
    "and do not refactor code outside the scope of the requested change. "
```

Note: Change the trailing `'` on the existing noop line (line 63) to `' '` to add a space before the new text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -k "system_prompt" -v`
Expected: Both tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py tests/governance/test_codegen_context.py
git commit -m "feat(codegen): add anti-duplication rules to system prompt

Instructs the model to check source snapshot before generating,
avoid duplicating existing logic, and make minimal edits.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: AST function index builder

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (new function after `_read_with_truncation`)
- Test: `tests/governance/test_codegen_context.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/governance/test_codegen_context.py`:

```python
# ---------------------------------------------------------------------------
# Task 3: AST function index
# ---------------------------------------------------------------------------

def test_function_index_basic_python():
    """Extracts top-level and class method definitions."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    source = textwrap.dedent('''\
        """Module docstring."""
        import os

        def top_level_func(x: int, y: str) -> bool:
            """Check something."""
            return True

        async def async_func():
            """Async helper."""
            pass

        class MyClass:
            """A class."""

            def method_one(self, data: list) -> None:
                """Process data."""
                pass

            @staticmethod
            def static_helper(n: int) -> int:
                """Helper."""
                return n * 2

            @property
            def name(self) -> str:
                """Get name."""
                return "foo"
    ''')
    result = _build_function_index(source, "example.py")
    assert "top_level_func" in result
    assert "async_func" in result
    assert "MyClass" in result
    assert "method_one" in result
    assert "static_helper" in result
    assert "name" in result  # @property
    assert "do NOT duplicate" in result.lower() or "DO NOT duplicate" in result


def test_function_index_syntax_error():
    """Unparseable Python returns empty string."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    result = _build_function_index("def broken(:\n  pass", "bad.py")
    assert result == ""


def test_function_index_non_python():
    """Non-.py files return empty string."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    result = _build_function_index("const x = 1;", "file.js")
    assert result == ""


def test_function_index_caps():
    """Index respects 50-entry cap."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    # Generate 60 functions
    lines = []
    for i in range(60):
        lines.append(f"def func_{i}():\n    pass\n")
    source = "\n".join(lines)
    result = _build_function_index(source, "many.py")
    # Should have at most 50 function entries
    func_count = result.count("def func_")
    assert func_count <= 50


def test_function_index_long_signature_truncated():
    """Very long signatures get truncated."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    long_params = ", ".join(f"param_{i}: str" for i in range(20))
    source = f"def long_func({long_params}) -> None:\n    pass\n"
    result = _build_function_index(source, "long.py")
    assert "long_func" in result
    # The signature line should not be excessively long
    for line in result.split("\n"):
        if "long_func" in line:
            assert len(line) <= 120, f"Signature line too long: {len(line)} chars"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -k "function_index" -v`
Expected: FAIL — `_build_function_index` does not exist

- [ ] **Step 3: Implement `_build_function_index`**

In `backend/core/ouroboros/governance/providers.py`, add after `_read_with_truncation` (after line ~145):

```python
def _build_function_index(content: str, file_path: str) -> str:
    """Build a structural index of functions/classes in a Python file.

    Returns a compact listing of top-level and class-level definitions
    with line numbers, signatures, and first-line docstrings. Helps the
    code generation model understand what already exists in the file.

    Non-Python files or syntax errors return empty string.
    """
    if not file_path.endswith(".py"):
        return ""
    import ast as _ast
    try:
        tree = _ast.parse(content)
    except SyntaxError:
        return ""

    _MAX_ENTRIES = 50
    _MAX_TOTAL_CHARS = 3072
    _MAX_SIG_CHARS = 100
    entries: list[str] = []
    total_chars = 0

    def _first_docline(node: _ast.AST) -> str:
        """Extract first line of docstring, if any."""
        if (
            node.body
            and isinstance(node.body[0], _ast.Expr)
            and isinstance(node.body[0].value, (_ast.Constant, _ast.Str))
        ):
            val = getattr(node.body[0].value, "value", None) or getattr(node.body[0].value, "s", "")
            if isinstance(val, str):
                first = val.strip().split("\n")[0].strip()
                if len(first) > 60:
                    first = first[:57] + "..."
                return f': "{first}"'
        return ""

    def _sig(node: _ast.AST) -> str:
        """Build parameter signature string."""
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            return ""
        try:
            sig = _ast.unparse(node.args)
        except Exception:
            sig = "..."
        if len(sig) > _MAX_SIG_CHARS:
            sig = sig[:_MAX_SIG_CHARS - 3] + "..."
        return f"({sig})"

    def _add_entry(prefix: str, node: _ast.AST, kind: str) -> bool:
        nonlocal total_chars
        if len(entries) >= _MAX_ENTRIES or total_chars >= _MAX_TOTAL_CHARS:
            return False
        lineno = getattr(node, "lineno", None) or "?"
        name = getattr(node, "name", "?")
        if kind == "class":
            line = f"{prefix}L{lineno} class {name}{_first_docline(node)}"
        else:
            is_async = "async " if isinstance(node, _ast.AsyncFunctionDef) else ""
            line = f"{prefix}L{lineno} {is_async}def {name}{_sig(node)}{_first_docline(node)}"
        if len(line) > 120:
            line = line[:117] + "..."
        entries.append(line)
        total_chars += len(line)
        return True

    for node in tree.body:
        if isinstance(node, _ast.ClassDef):
            if not _add_entry("- ", node, "class"):
                break
            for item in node.body:
                if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    if not _add_entry("  - ", item, "func"):
                        break
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            if not _add_entry("- ", node, "func"):
                break

    if not entries:
        return ""
    header = "## Structural Index (what already exists — DO NOT duplicate)\n\n"
    return header + "\n".join(entries)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -k "function_index" -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py tests/governance/test_codegen_context.py
git commit -m "feat(codegen): add AST function index for anti-duplication context

Builds a structural index of existing functions/classes with line numbers,
signatures, and docstrings. Injected into codegen prompt so the model
knows what already exists before generating new code.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Recent file history builder

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (new function)
- Test: `tests/governance/test_codegen_context.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/governance/test_codegen_context.py`:

```python
# ---------------------------------------------------------------------------
# Task 4: Recent file history
# ---------------------------------------------------------------------------

def test_recent_file_history_with_git(tmp_path):
    """Returns last commits touching the file when .git exists."""
    import subprocess
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    # Set up a real git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "example.py"
    f.write_text("x = 1\n")
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, capture_output=True)
    f.write_text("x = 2\n")
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update value"], cwd=tmp_path, capture_output=True)

    result = _build_recent_file_history(f, tmp_path)
    assert "Recent Changes" in result
    assert "update value" in result
    assert "initial commit" in result


def test_recent_file_history_no_git(tmp_path):
    """Returns empty string when .git directory missing."""
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    f = tmp_path / "example.py"
    f.write_text("x = 1\n")
    result = _build_recent_file_history(f, tmp_path)
    assert result == ""


def test_recent_file_history_path_outside_repo(tmp_path):
    """Returns empty string when path is not under repo_root."""
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    (tmp_path / ".git").mkdir()  # fake .git dir
    outside = Path("/tmp/not_in_repo.py")
    result = _build_recent_file_history(outside, tmp_path)
    assert result == ""


def test_recent_file_history_capped_length(tmp_path):
    """Output must not exceed 500 chars."""
    import subprocess
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "example.py"
    for i in range(10):
        f.write_text(f"x = {i}\n")
        subprocess.run(["git", "add", "example.py"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"commit {i} with a {'long ' * 20}message"], cwd=tmp_path, capture_output=True)

    result = _build_recent_file_history(f, tmp_path)
    # Header + content should be bounded
    assert len(result) <= 600  # small buffer for header
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -k "recent_file_history" -v`
Expected: FAIL — `_build_recent_file_history` does not exist

- [ ] **Step 3: Implement `_build_recent_file_history`**

In `backend/core/ouroboros/governance/providers.py`, add after `_build_function_index`:

```python
def _build_recent_file_history(path: Path, repo_root: Path) -> str:
    """Build a summary of recent commits touching a file.

    Returns empty string if .git is missing, path is outside repo_root,
    or git fails for any reason. Never raises.
    """
    if not (repo_root / ".git").exists():
        return ""
    try:
        rel_path = path.relative_to(repo_root)
    except ValueError:
        return ""

    import subprocess as _sp
    try:
        result = _sp.run(
            ["git", "log", "--oneline", "-5", "--", str(rel_path)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
    except (OSError, _sp.TimeoutExpired):
        return ""

    lines = result.stdout.strip().split("\n")[:5]
    body = "\n".join(f"- {line}" for line in lines)
    output = f"## Recent Changes (last {len(lines)} commits touching this file)\n\n{body}"
    return output[:500]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -k "recent_file_history" -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py tests/governance/test_codegen_context.py
git commit -m "feat(codegen): add recent file history for temporal context

Injects last 5 commits touching the target file into the codegen prompt.
Gracefully skips when .git missing, path outside repo, or git fails.
3-second timeout prevents CI/sandbox hangs.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Wire index and history into prompt assembly

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py:836-889`
- Test: `tests/governance/test_codegen_context.py`

- [ ] **Step 1: Write the integration test**

Append to `tests/governance/test_codegen_context.py`:

```python
# ---------------------------------------------------------------------------
# Task 5: Prompt assembly integration
# ---------------------------------------------------------------------------

def test_prompt_assembly_includes_index_and_history(tmp_path):
    """Full codegen prompt should contain structural index and recent history."""
    import subprocess
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt

    # Set up a git repo with a Python file
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    target = tmp_path / "module.py"
    target.write_text(textwrap.dedent('''\
        """Test module."""

        def existing_function(x: int) -> bool:
            """Already implemented."""
            return x > 0

        class Handler:
            def process(self, data: list) -> None:
                """Process data."""
                pass
    '''))
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add module"], cwd=tmp_path, capture_output=True)

    # Build a minimal OperationContext mock
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.op_id = "test-op-001"
    ctx.description = "Add a new feature"
    ctx.target_files = ["module.py"]
    ctx.human_instructions = ""
    ctx.strategic_memory_prompt = ""
    ctx.expanded_context_files = ()
    ctx.cross_repo = False
    ctx.repo_scope = set()

    prompt = _build_codegen_prompt(
        ctx=ctx,
        repo_root=tmp_path,
        repo_roots=None,
    )

    # Structural index should be present
    assert "Structural Index" in prompt
    assert "existing_function" in prompt
    assert "Handler" in prompt
    assert "process" in prompt

    # Recent history should be present
    assert "Recent Changes" in prompt
    assert "add module" in prompt

    # Source snapshot should contain the full file (no truncation for this small file)
    assert "def existing_function" in prompt
    assert "[TRUNCATED" not in prompt

    # Anti-duplication should be in system prompt (check module constant)
    from backend.core.ouroboros.governance.providers import _CODEGEN_SYSTEM_PROMPT
    assert "ANTI-DUPLICATION" in _CODEGEN_SYSTEM_PROMPT


def test_prompt_no_truncation_32kb_file(tmp_path):
    """A 32KB file should NOT be truncated with default 64KB budget."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    from unittest.mock import MagicMock

    target = tmp_path / "big.py"
    # Generate a ~32KB Python file
    lines = [f"def func_{i}():\n    return {i}\n" for i in range(800)]
    target.write_text("\n".join(lines))

    ctx = MagicMock()
    ctx.op_id = "test-op-002"
    ctx.description = "Modify big file"
    ctx.target_files = ["big.py"]
    ctx.human_instructions = ""
    ctx.strategic_memory_prompt = ""
    ctx.expanded_context_files = ()
    ctx.cross_repo = False
    ctx.repo_scope = set()

    prompt = _build_codegen_prompt(
        ctx=ctx,
        repo_root=tmp_path,
        repo_roots=None,
    )
    assert "[TRUNCATED" not in prompt, "32KB file should not be truncated with 64KB budget"
```

- [ ] **Step 2: Run integration tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -k "prompt_assembly or prompt_no_truncation" -v`
Expected: FAIL — prompt does not contain "Structural Index" or "Recent Changes" yet

- [ ] **Step 3: Wire index and history into `_build_codegen_prompt`**

In `backend/core/ouroboros/governance/providers.py`, in the `_build_codegen_prompt` function, find the assembly section around line 836-861. Currently it looks like:

```python
    # ── 4. Assemble final prompt ─────────────────────────────────────────
    file_block = "\n\n".join(file_sections) if file_sections else "_No target files._"
    parts = []
    # Human instructions ...
    ...
    if strategic_memory_prompt.strip():
        parts.append(strategic_memory_prompt)
    parts += [
        f"## Source Snapshot\n\n{file_block}",
        context_block,
    ]
```

Insert the structural index and recent history **after** `strategic_memory_prompt` and **before** `## Source Snapshot`:

```python
    if strategic_memory_prompt.strip():
        parts.append(strategic_memory_prompt)

    # ── 4a. Structural index + recent history (Sub-project B: The Eyes) ──
    if ctx.target_files:
        _primary_target = ctx.target_files[0]
        _primary_abs = (
            Path(_primary_target) if Path(_primary_target).is_absolute()
            else (effective_single_repo_root / _primary_target)
        )
        if _primary_abs.exists() and _primary_abs.suffix == ".py":
            try:
                _primary_content = _primary_abs.read_text(encoding="utf-8", errors="replace")
                _func_idx = _build_function_index(_primary_content, str(_primary_abs))
                if _func_idx:
                    parts.append(_func_idx)
            except OSError:
                pass
        _history = _build_recent_file_history(_primary_abs, effective_single_repo_root)
        if _history:
            parts.append(_history)

    parts += [
        f"## Source Snapshot\n\n{file_block}",
        context_block,
    ]
```

- [ ] **Step 4: Run integration tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -k "prompt_assembly or prompt_no_truncation" -v`
Expected: Both PASS

- [ ] **Step 5: Run full test file**

Run: `python3 -m pytest tests/governance/test_codegen_context.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py tests/governance/test_codegen_context.py
git commit -m "feat(codegen): wire structural index and recent history into prompt assembly

Codegen prompts now include an AST function index and last 5 commits
before the source snapshot. Gives the model structural awareness of
what exists and temporal context of what changed recently.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Run full regression and verify

**Files:** None (verification only)

- [ ] **Step 1: Run all governance tests**

Run: `python3 -m pytest tests/governance/ -v --timeout=30 2>&1 | tail -30`
Expected: No new failures introduced.

- [ ] **Step 2: Run intake tests (Sub-project A)**

Run: `python3 -m pytest tests/governance/intake/ -v --timeout=30 2>&1 | tail -15`
Expected: 114/114 pass (no regression from Sub-project B)

- [ ] **Step 3: Final commit if fixups needed**

If any regressions found, fix and commit:

```bash
git add -u
git commit -m "fix(codegen): address regression findings from context injection

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

If no fixups needed, this step is a no-op.
