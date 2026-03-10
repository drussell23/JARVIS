# Tool-Use Interface for J-Prime Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give J-Prime the ability to call read-only introspection tools (`search_code`, `read_file`, `list_symbols`, `run_tests`, `get_callers`) before writing a patch, enabling multi-turn generation with up to 5 tool-call rounds and a hard token budget wall.

**Architecture:** A new `ToolExecutor` class handles all tool dispatch with path-security enforcement. The generation prompt gains an "Available Tools" section. `PrimeProvider.generate()` and `ClaudeProvider.generate()` each wrap the `PrimeClient`/Anthropic call in a loop that detects `schema_version: "2b.2-tool"` responses, executes the tool, appends the result to the conversation, and re-sends until a patch response or max-iterations/budget is reached.

**Tech Stack:** Python 3.11 asyncio, `ast` (symbol listing), `subprocess`/`asyncio.create_subprocess_exec` (grep + pytest), existing `BlockedPathError`/`_safe_context_path` (security), `PrimeClient`, `AsyncAnthropic`.

---

## Key Constants and Schema

```
_TOOL_SCHEMA_VERSION = "2b.2-tool"
_TOOL_SCHEMA_KEYS    = frozenset({"schema_version", "tool_call"})
_TOOL_CALL_KEYS      = frozenset({"name", "arguments"})
MAX_TOOL_ITERATIONS  = 5
MAX_TOOL_LOOP_CHARS  = 32_000   # hard budget: total accumulated prompt chars
```

Tool call JSON from J-Prime:
```json
{
  "schema_version": "2b.2-tool",
  "tool_call": {
    "name": "search_code",
    "arguments": {"pattern": "scoring.*formula", "file_glob": "*.py"}
  }
}
```

Tool result injected back into conversation (single-turn providers like Prime — concatenated string; multi-turn providers like Claude — user message):
```
--- Tool Result: search_code ---
<output or error>
--- End Tool Result ---
Now continue. Either call another tool or return the patch JSON.
```

---

## Task 1: ToolExecutor — execution engine with security

**Files:**
- Create: `backend/core/ouroboros/governance/tool_executor.py`
- Test: `tests/test_ouroboros_governance/test_tool_use_interface.py`

**Step 1: Write failing tests**

```python
# tests/test_ouroboros_governance/test_tool_use_interface.py
"""Tests for Tool-Use Interface: ToolExecutor + provider tool loops."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.tool_executor import (
    ToolCall,
    ToolExecutor,
    ToolResult,
)


class TestToolExecutor:
    """ToolExecutor: execution of each tool type with security checks."""

    def test_read_file_returns_content(self, tmp_path: Path) -> None:
        (tmp_path / "sample.py").write_text("def foo():\n    pass\n")
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(name="read_file", arguments={"path": "sample.py"}))
        assert result.error is None
        assert "def foo" in result.output

    def test_read_file_with_line_range(self, tmp_path: Path) -> None:
        lines = "\n".join(f"line_{i}" for i in range(1, 21))
        (tmp_path / "big.py").write_text(lines)
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="read_file",
            arguments={"path": "big.py", "lines_from": 5, "lines_to": 10},
        ))
        assert result.error is None
        assert "line_5" in result.output
        assert "line_11" not in result.output

    def test_read_file_blocked_path(self, tmp_path: Path) -> None:
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="read_file",
            arguments={"path": "../../etc/passwd"},
        ))
        assert result.error is not None
        assert "blocked" in result.error.lower()

    def test_list_symbols_returns_functions_and_classes(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text(
            "class Foo:\n    def bar(self): pass\n\ndef standalone(): pass\n"
        )
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="list_symbols",
            arguments={"module_path": "mod.py"},
        ))
        assert result.error is None
        assert "Foo" in result.output
        assert "standalone" in result.output

    def test_unknown_tool_returns_error(self, tmp_path: Path) -> None:
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(name="nonexistent_tool", arguments={}))
        assert result.error is not None
        assert "unknown tool" in result.error.lower()
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py::TestToolExecutor -v
```
Expected: ImportError — `tool_executor` does not exist yet.

**Step 3: Implement ToolExecutor**

```python
# backend/core/ouroboros/governance/tool_executor.py
"""Tool execution engine for J-Prime's tool-use interface.

Provides a sandboxed executor for the five read-only introspection tools
available to J-Prime during multi-turn code generation.

Tools
-----
- ``read_file(path, lines_from, lines_to)``
- ``list_symbols(module_path)``
- ``search_code(pattern, file_glob)``
- ``run_tests(paths)``
- ``get_callers(function_name, file_path)``

Security
--------
All ``path`` / ``file_path`` arguments are validated against ``repo_root``
via ``_safe_context_path``.  Traversal attempts raise ``BlockedPathError``,
which the executor maps to a ``ToolResult.error`` string (never re-raised).
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """Result of executing one ToolCall."""

    tool_call: ToolCall
    output: str
    error: Optional[str] = None


_MAX_TOOL_OUTPUT_CHARS = 4_000   # truncate results exceeding this


class ToolExecutor:
    """Executes J-Prime tool calls in a sandboxed, read-only environment.

    Parameters
    ----------
    repo_root:
        The repository root directory.  All path arguments are validated
        to stay within this directory.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, tool_call: ToolCall) -> ToolResult:
        """Dispatch a ToolCall and return a ToolResult.

        Never raises — all errors are captured in ``ToolResult.error``.
        """
        dispatch = {
            "read_file": self._read_file,
            "list_symbols": self._list_symbols,
            "search_code": self._search_code,
            "run_tests": self._run_tests,
            "get_callers": self._get_callers,
        }
        handler = dispatch.get(tool_call.name)
        if handler is None:
            return ToolResult(
                tool_call=tool_call,
                output="",
                error=f"unknown tool: '{tool_call.name}'",
            )
        try:
            output = handler(tool_call.arguments)
            return ToolResult(tool_call=tool_call, output=output[:_MAX_TOOL_OUTPUT_CHARS])
        except Exception as exc:  # pylint: disable=broad-except
            return ToolResult(tool_call=tool_call, output="", error=str(exc))

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _safe_resolve(self, path_str: str) -> Path:
        """Resolve path_str against repo_root, raising on traversal."""
        from backend.core.ouroboros.governance.test_runner import BlockedPathError

        raw = Path(path_str)
        if raw.is_absolute():
            resolved = raw.resolve()
        else:
            resolved = (self._repo_root / raw).resolve()
        try:
            resolved.relative_to(self._repo_root.resolve())
        except ValueError:
            raise BlockedPathError(
                f"blocked path traversal: {path_str!r} escapes repo_root"
            )
        if resolved.is_symlink():
            raise BlockedPathError(f"blocked symlink: {path_str!r}")
        return resolved

    def _read_file(self, args: Dict[str, Any]) -> str:
        path_str: str = args["path"]
        lines_from: int = max(1, int(args.get("lines_from", 1)))
        lines_to: int = int(args.get("lines_to", 200))
        resolved = self._safe_resolve(path_str)
        if not resolved.exists():
            return f"(file not found: {path_str})"
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[lines_from - 1 : lines_to]
        return "\n".join(
            f"{lines_from + i}: {line}" for i, line in enumerate(selected)
        )

    def _list_symbols(self, args: Dict[str, Any]) -> str:
        module_path: str = args["module_path"]
        resolved = self._safe_resolve(module_path)
        if not resolved.exists():
            return f"(file not found: {module_path})"
        try:
            tree = ast.parse(resolved.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            return f"(SyntaxError: {exc})"
        lines: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.append(f"  function: {node.name} (line {node.lineno})")
            elif isinstance(node, ast.ClassDef):
                lines.append(f"  class: {node.name} (line {node.lineno})")
        return "\n".join(sorted(set(lines))) or "(no symbols found)"

    def _search_code(self, args: Dict[str, Any]) -> str:
        pattern: str = args["pattern"]
        file_glob: str = args.get("file_glob", "*.py")
        try:
            result = subprocess.run(
                ["grep", "-r", "--include", file_glob, "-n", "--", pattern, str(self._repo_root)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout or result.stderr
            lines = output.splitlines()
            if len(lines) > 50:
                lines = lines[:50] + [f"... ({len(lines) - 50} more lines truncated)"]
            return "\n".join(lines) or "(no matches)"
        except subprocess.TimeoutExpired:
            return "(search timed out after 10s)"

    def _run_tests(self, args: Dict[str, Any]) -> str:
        raw_paths = args.get("paths", [])
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        safe_paths: list[str] = []
        for p in raw_paths:
            try:
                resolved = self._safe_resolve(p)
                safe_paths.append(str(resolved))
            except Exception:  # pylint: disable=broad-except
                return f"(blocked path: {p!r})"
        cmd = ["python3", "-m", "pytest", "--tb=short", "-q"] + safe_paths
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self._repo_root),
            )
            combined = (result.stdout + result.stderr)[-_MAX_TOOL_OUTPUT_CHARS:]
            return combined or "(no output)"
        except subprocess.TimeoutExpired:
            return "(pytest timed out after 30s)"

    def _get_callers(self, args: Dict[str, Any]) -> str:
        function_name: str = args["function_name"]
        file_path: Optional[str] = args.get("file_path")
        search_root = str(self._repo_root)
        if file_path:
            try:
                resolved = self._safe_resolve(file_path)
                search_root = str(resolved.parent)
            except Exception:  # pylint: disable=broad-except
                pass
        pattern = rf"\b{function_name}\s*\("
        try:
            result = subprocess.run(
                ["grep", "-r", "--include", "*.py", "-n", "-E", "--", pattern, search_root],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = result.stdout.splitlines()
            if len(lines) > 30:
                lines = lines[:30] + [f"... ({len(lines) - 30} more)"]
            return "\n".join(lines) or "(no callers found)"
        except subprocess.TimeoutExpired:
            return "(search timed out)"
```

**Step 4: Run tests to verify pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py::TestToolExecutor -v
```
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/tool_executor.py \
        tests/test_ouroboros_governance/test_tool_use_interface.py
git commit -m "feat(ouroboros): add ToolExecutor for J-Prime tool-use interface"
```

---

## Task 2: Tool response parsing + prompt injection

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (constants + `_build_codegen_prompt` + `_parse_tool_call_response`)
- Test: `tests/test_ouroboros_governance/test_tool_use_interface.py` (append)

**Step 1: Write failing tests**

```python
# Append to test_tool_use_interface.py (inside the file, after TestToolExecutor)

class TestParseToolCallResponse:
    """Parsing 2b.2-tool schema from raw model output."""

    def test_valid_tool_call_parsed(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": {"name": "read_file", "arguments": {"path": "utils.py"}},
        })
        tc = _parse_tool_call_response(raw)
        assert tc is not None
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "utils.py"}

    def test_patch_response_returns_none(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "x.py", "full_content": "pass\n", "rationale": "ok"}
            ],
        })
        assert _parse_tool_call_response(raw) is None

    def test_invalid_json_returns_none(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        assert _parse_tool_call_response("not json") is None

    def test_tool_call_missing_name_returns_none(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": {"arguments": {"path": "x.py"}},
        })
        assert _parse_tool_call_response(raw) is None


class TestToolPromptInjection:
    """_build_codegen_prompt with tools_enabled=True."""

    def _make_ctx(self):
        from backend.core.ouroboros.governance.op_context import OperationContext
        return OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="Add helper function",
        )

    def test_tools_section_present_when_enabled(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        ctx = self._make_ctx()
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path, tools_enabled=True)
        assert "Available Tools" in prompt
        assert "search_code" in prompt
        assert "2b.2-tool" in prompt

    def test_tools_section_absent_when_disabled(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        ctx = self._make_ctx()
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path, tools_enabled=False)
        assert "Available Tools" not in prompt

    def test_tools_section_absent_by_default(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        ctx = self._make_ctx()
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "Available Tools" not in prompt
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py::TestParseToolCallResponse tests/test_ouroboros_governance/test_tool_use_interface.py::TestToolPromptInjection -v
```
Expected: ImportError / AttributeError — functions don't exist yet.

**Step 3: Implement in providers.py**

Add after the existing constants block (after line ~61):

```python
# ── Tool-use interface ────────────────────────────────────────────────
_TOOL_SCHEMA_VERSION = "2b.2-tool"
_TOOL_SCHEMA_KEYS    = frozenset({"schema_version", "tool_call"})
_TOOL_CALL_KEYS      = frozenset({"name", "arguments"})
MAX_TOOL_ITERATIONS  = 5
MAX_TOOL_LOOP_CHARS  = 32_000   # hard accumulated-prompt budget
```

Add `_parse_tool_call_response` function (insert between `_extract_json_block` and `_parse_multi_repo_response`, ~line 412):

```python
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
```

Add `_build_tool_section` function (before `_build_codegen_prompt`):

```python
def _build_tool_section() -> str:
    """Return the "Available Tools" block injected into the generation prompt."""
    return f"""## Available Tools

If you need more information before writing the patch, respond with ONLY a
tool_call JSON (no other text):

```json
{{
  "schema_version": "{_TOOL_SCHEMA_VERSION}",
  "tool_call": {{
    "name": "<tool_name>",
    "arguments": {{...}}
  }}
}}
```

Available tools:
- `search_code(pattern, file_glob="*.py")` — search the codebase with a regex pattern
- `read_file(path, lines_from=1, lines_to=200)` — read file content (repo-relative path)
- `list_symbols(module_path)` — list functions and classes in a Python file
- `run_tests(paths)` — run pytest for the given test paths (list of strings), returns summary
- `get_callers(function_name, file_path=None)` — find call sites of a function

Max {MAX_TOOL_ITERATIONS} tool calls total. After gathering info, respond with the patch JSON."""
```

Update `_build_codegen_prompt` signature and body to accept `tools_enabled: bool = False` and append the tool section when enabled:

```python
def _build_codegen_prompt(
    ctx: "OperationContext",
    repo_root: Optional[Path] = None,
    repo_roots: Optional[Dict[str, Path]] = None,
    tools_enabled: bool = False,
) -> str:
    # ... (existing body unchanged) ...
    # ── 4. Assemble final prompt ──────────────────────────────────────
    file_block = "\n\n".join(file_sections) if file_sections else "_No target files._"
    parts = [
        f"## Task\nOp-ID: {ctx.op_id}\nGoal: {ctx.description}",
        f"## Source Snapshot\n\n{file_block}",
        context_block,
    ]
    if expanded_context_block:
        parts.append(expanded_context_block)
    if tools_enabled:
        parts.append(_build_tool_section())
    parts.append(schema_instruction)
    return "\n\n".join(parts)
```

(Only the signature line, the `tools_enabled` block insertion, and the final `return` line change — everything in between is unchanged.)

**Step 4: Run tests to verify pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py::TestParseToolCallResponse tests/test_ouroboros_governance/test_tool_use_interface.py::TestToolPromptInjection -v
```
Expected: 7 PASSED

**Step 5: Verify existing tests still pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py -q
```
Expected: 36 passed

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py \
        tests/test_ouroboros_governance/test_tool_use_interface.py
git commit -m "feat(providers): add tool-call schema parsing and tools prompt injection"
```

---

## Task 3: Tool loop in PrimeProvider.generate()

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` — `PrimeProvider.__init__` and `PrimeProvider.generate()`
- Test: `tests/test_ouroboros_governance/test_tool_use_interface.py` (append)

**Step 1: Write failing tests**

```python
# Append to test_tool_use_interface.py

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)
from datetime import datetime, timezone


def _make_ctx(op_id: str = "op-test-tool-001") -> OperationContext:
    return OperationContext.create(
        target_files=("tests/test_utils.py",),
        description="Add test for edge case",
        op_id=op_id,
    )


def _prime_response(schema: str = "2b.1", **extra) -> str:
    """Build a minimal valid prime response JSON string."""
    if schema == "2b.2-tool":
        return json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": extra.get("tool_call", {"name": "search_code", "arguments": {"pattern": "foo"}}),
        })
    return json.dumps({
        "schema_version": "2b.1",
        "candidates": [
            {
                "candidate_id": "c1",
                "file_path": extra.get("file_path", "tests/test_utils.py"),
                "full_content": extra.get("content", "def test_edge():\n    assert True\n"),
                "rationale": "test",
            }
        ],
    })


class TestPrimeProviderToolLoop:
    """PrimeProvider: multi-turn tool-call loop."""

    def _mock_prime_client(self, responses: list[str]) -> MagicMock:
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.model = "prime-7b"
        mock_resp.latency_ms = 100.0
        mock_resp.tokens_used = 100
        mock_resp.metadata = {}
        # Cycle through responses on each generate() call
        call_count = [0]
        async def _generate(**kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            mock_resp.content = responses[i]
            return mock_resp
        client.generate = _generate
        return client

    async def test_tool_loop_disabled_by_default(self, tmp_path: Path) -> None:
        """With tools_enabled=False (default), generate() returns first response."""
        from backend.core.ouroboros.governance.providers import PrimeProvider
        client = self._mock_prime_client([_prime_response()])
        provider = PrimeProvider(client, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime(2026, 3, 9, 12, 5, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1

    async def test_tool_loop_single_tool_then_patch(self, tmp_path: Path) -> None:
        """One tool call then patch: generate() calls client twice, returns patch."""
        from backend.core.ouroboros.governance.providers import PrimeProvider
        responses = [
            _prime_response("2b.2-tool", tool_call={"name": "read_file", "arguments": {"path": "tests/test_utils.py"}}),
            _prime_response("2b.1"),
        ]
        client = self._mock_prime_client(responses)
        provider = PrimeProvider(client, repo_root=tmp_path, tools_enabled=True)
        ctx = _make_ctx()
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1

    async def test_tool_loop_exhausts_max_iterations(self, tmp_path: Path) -> None:
        """If model keeps calling tools past MAX_TOOL_ITERATIONS, raise RuntimeError."""
        from backend.core.ouroboros.governance.providers import PrimeProvider, MAX_TOOL_ITERATIONS
        # All responses are tool calls (never produces a patch)
        responses = [_prime_response("2b.2-tool")] * (MAX_TOOL_ITERATIONS + 2)
        client = self._mock_prime_client(responses)
        provider = PrimeProvider(client, repo_root=tmp_path, tools_enabled=True)
        ctx = _make_ctx()
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="tool_loop_max_iterations"):
            await provider.generate(ctx, deadline)

    async def test_tool_loop_token_budget_wall(self, tmp_path: Path) -> None:
        """When accumulated prompt exceeds MAX_TOOL_LOOP_CHARS, raise RuntimeError."""
        from backend.core.ouroboros.governance.providers import PrimeProvider
        # Make tool output enormous to blow budget
        huge_output = "x" * 40_000
        # First response is a tool call for search_code
        responses = [
            _prime_response("2b.2-tool", tool_call={"name": "search_code", "arguments": {"pattern": "foo"}}),
        ]
        client = self._mock_prime_client(responses)
        provider = PrimeProvider(client, repo_root=tmp_path, tools_enabled=True)
        # Mock executor to return huge output
        from backend.core.ouroboros.governance.tool_executor import ToolExecutor, ToolResult, ToolCall as TC
        with patch.object(ToolExecutor, "execute", return_value=ToolResult(
            tool_call=TC(name="search_code", arguments={"pattern": "foo"}),
            output=huge_output,
        )):
            ctx = _make_ctx()
            deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
            with pytest.raises(RuntimeError, match="tool_loop_budget_exceeded"):
                await provider.generate(ctx, deadline)
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py::TestPrimeProviderToolLoop -v
```
Expected: FAILED — `PrimeProvider` has no `tools_enabled` param yet.

**Step 3: Implement tool loop in PrimeProvider**

Update `PrimeProvider.__init__` to accept `tools_enabled: bool = False`:

```python
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
```

Replace `PrimeProvider.generate()` body with the tool-loop version:

```python
async def generate(
    self,
    context: OperationContext,
    deadline: datetime,
) -> GenerationResult:
    from backend.core.ouroboros.governance.tool_executor import ToolExecutor

    repo_root = self._repo_root or Path.cwd()
    executor = ToolExecutor(repo_root=repo_root)

    prompt = _build_codegen_prompt(
        context,
        repo_root=self._repo_root,
        repo_roots=self._repo_roots,
        tools_enabled=self._tools_enabled,
    )
    accumulated_chars = len(prompt)
    tool_rounds = 0
    start = time.monotonic()

    while True:
        response = await self._client.generate(
            prompt=prompt,
            system_prompt=_CODEGEN_SYSTEM_PROMPT,
            max_tokens=self._max_tokens,
            temperature=0.2,
        )
        raw = response.content

        # Attempt to parse as tool call
        if self._tools_enabled:
            tool_call = _parse_tool_call_response(raw)
            if tool_call is not None:
                if tool_rounds >= MAX_TOOL_ITERATIONS:
                    raise RuntimeError(
                        f"gcp-jprime_tool_loop_max_iterations:{MAX_TOOL_ITERATIONS}"
                    )
                # Execute the tool
                tool_result = executor.execute(tool_call)
                result_text = (
                    f"--- Tool Result: {tool_call.name} ---\n"
                    f"{tool_result.output if not tool_result.error else 'ERROR: ' + tool_result.error}\n"
                    "--- End Tool Result ---\n"
                    "Now continue. Either call another tool or return the patch JSON."
                )
                # Append tool exchange to prompt (single-turn Prime)
                prompt = (
                    f"{prompt}\n\n"
                    f"[You called: {tool_call.name}({json.dumps(tool_call.arguments)})]\n"
                    f"{result_text}"
                )
                accumulated_chars += len(result_text)
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
```

**Step 4: Run tests to verify pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py::TestPrimeProviderToolLoop -v
```
Expected: 4 PASSED

**Step 5: Verify existing provider tests still pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py -q
```
Expected: 36 passed

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py \
        tests/test_ouroboros_governance/test_tool_use_interface.py
git commit -m "feat(providers): add tool loop to PrimeProvider.generate()"
```

---

## Task 4: Tool loop in ClaudeProvider + full suite

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` — `ClaudeProvider.__init__` and `ClaudeProvider.generate()`
- Test: `tests/test_ouroboros_governance/test_tool_use_interface.py` (append)

**Step 1: Write failing tests**

```python
# Append to test_tool_use_interface.py

class TestClaudeProviderToolLoop:
    """ClaudeProvider: multi-turn tool-call loop using messages API."""

    def _mock_claude_client(self, responses: list[str]) -> MagicMock:
        """Build a mock anthropic client cycling through response texts."""
        call_count = [0]
        async def _create(**kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            msg = MagicMock()
            msg.content = [MagicMock(text=responses[i])]
            msg.usage = MagicMock(input_tokens=100, output_tokens=100)
            msg.model = "claude-sonnet-4-6"
            return msg
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = _create
        return client

    async def test_tool_loop_disabled_by_default(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = self._mock_claude_client([_prime_response()])
        ctx = _make_ctx("op-claude-001")
        deadline = datetime(2026, 3, 9, 12, 5, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)

    async def test_tool_loop_single_tool_then_patch(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        responses = [
            _prime_response("2b.2-tool", tool_call={"name": "list_symbols", "arguments": {"module_path": "utils.py"}}),
            _prime_response("2b.1"),
        ]
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tools_enabled=True)
        provider._client = self._mock_claude_client(responses)
        ctx = _make_ctx("op-claude-002")
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1

    async def test_tool_loop_exhausts_max_iterations(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider, MAX_TOOL_ITERATIONS
        responses = [_prime_response("2b.2-tool")] * (MAX_TOOL_ITERATIONS + 2)
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tools_enabled=True)
        provider._client = self._mock_claude_client(responses)
        ctx = _make_ctx("op-claude-003")
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="tool_loop_max_iterations"):
            await provider.generate(ctx, deadline)
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py::TestClaudeProviderToolLoop -v
```
Expected: FAILED — `ClaudeProvider` has no `repo_root`/`tools_enabled` params yet.

**Step 3: Update ClaudeProvider**

Update `ClaudeProvider.__init__` to add `repo_root` and `tools_enabled`:

```python
def __init__(
    self,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 8192,
    max_cost_per_op: float = 2.0,
    daily_budget: float = 20.0,
    repo_root: Optional[Path] = None,
    repo_roots: Optional[Dict[str, Path]] = None,
    tools_enabled: bool = False,
) -> None:
    # ... (add self._repo_root = repo_root, self._tools_enabled = tools_enabled) ...
```

Wrap `ClaudeProvider.generate()` body in the tool loop (Claude uses multi-turn messages):

```python
async def generate(
    self,
    context: OperationContext,
    deadline: datetime,
) -> GenerationResult:
    from backend.core.ouroboros.governance.tool_executor import ToolExecutor

    self._maybe_reset_daily_budget()
    if self._daily_spend >= self._max_cost_per_op:
        raise RuntimeError("claude_budget_exhausted")

    repo_root = self._repo_root or Path.cwd()
    executor = ToolExecutor(repo_root=repo_root)

    system = _CODEGEN_SYSTEM_PROMPT
    prompt_text = _build_codegen_prompt(
        context,
        repo_root=self._repo_root,
        repo_roots=getattr(self, "_repo_roots", None),
        tools_enabled=self._tools_enabled,
    )
    # Build messages array for multi-turn
    messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt_text}]
    accumulated_chars = len(prompt_text)
    tool_rounds = 0
    start = time.monotonic()

    while True:
        timeout_s = max(1.0, (deadline - datetime.now(tz=timezone.utc)).total_seconds())
        msg = await asyncio.wait_for(
            self._client.messages.create(
                model=self._model,
                max_tokens=min(self._max_tokens, 8192),
                temperature=0.2,
                system=system,
                messages=messages,
            ),
            timeout=timeout_s,
        )
        raw = msg.content[0].text if msg.content else ""
        input_tokens = msg.usage.input_tokens
        output_tokens = msg.usage.output_tokens
        cost = self._estimate_cost(input_tokens, output_tokens)
        self._record_cost(cost)

        # Attempt tool call parse
        if self._tools_enabled:
            tool_call = _parse_tool_call_response(raw)
            if tool_call is not None:
                if tool_rounds >= MAX_TOOL_ITERATIONS:
                    raise RuntimeError(
                        f"claude-api_tool_loop_max_iterations:{MAX_TOOL_ITERATIONS}"
                    )
                tool_result = executor.execute(tool_call)
                result_text = (
                    f"Tool result for {tool_call.name}:\n"
                    f"{tool_result.output if not tool_result.error else 'ERROR: ' + tool_result.error}\n"
                    "Now either call another tool or return the patch JSON."
                )
                # Append assistant + user turns for multi-turn
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": result_text})
                accumulated_chars += len(raw) + len(result_text)
                if accumulated_chars > MAX_TOOL_LOOP_CHARS:
                    raise RuntimeError(
                        f"claude-api_tool_loop_budget_exceeded:{accumulated_chars}"
                    )
                tool_rounds += 1
                continue

        # Parse as patch response
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
            repo_roots=getattr(self, "_repo_roots", None),
        )
        logger.info(
            "[ClaudeProvider] %d candidates in %.1fs (tool_rounds=%d), cost=$%.4f",
            len(result.candidates), duration, tool_rounds, cost,
        )
        return result
```

Note: `_estimate_cost` and `_record_cost` are existing private methods. Preserve them unchanged.

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_tool_use_interface.py -v
```
Expected: All tests PASSED

**Step 5: Run full suite**

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```
Expected: 730+ passed (new tests added)

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py \
        tests/test_ouroboros_governance/test_tool_use_interface.py
git commit -m "feat(providers): add tool loop to ClaudeProvider.generate()"
```

---

## Final: Run full test suite

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q --tb=short
```
Expected: All tests pass.

---

## Notes for implementer

**Key invariants to preserve:**
- `_parse_generation_response` signature unchanged — tool loop sits ABOVE it
- `generate()` still returns `GenerationResult` — orchestrator API unchanged
- `tools_enabled=False` by default — zero behavior change for existing callers
- `ToolExecutor` is sync — no async subprocess needed for these tools
- The `MAX_TOOL_ITERATIONS` and `MAX_TOOL_LOOP_CHARS` constants are exported from `providers.py` so tests can import them

**Existing providers.py structure to navigate:**
- `_build_codegen_prompt` starts at ~line 162
- `_extract_json_block` at ~line 394
- `_parse_multi_repo_response` at ~line 412
- `_parse_generation_response` at ~line 517
- `PrimeProvider` class at ~line 657
- `ClaudeProvider` class starts after PrimeProvider (~line 780+)

**ClaudeProvider existing `__init__` signature** (read current before editing — preserve all existing params):
```python
def __init__(
    self,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 8192,
    max_cost_per_op: float = 2.0,
    daily_budget: float = 20.0,
) -> None:
```
Add `repo_root`, `repo_roots`, `tools_enabled` at the end with defaults.

**ClaudeProvider existing `generate()` body** — the existing method uses `asyncio.wait_for` around `self._client.messages.create(...)`. The new version wraps this in the tool loop. Read the current implementation before replacing.
