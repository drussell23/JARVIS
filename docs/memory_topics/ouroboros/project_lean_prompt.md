---
title: Project Lean Prompt
modules: []
status: historical
source: project_lean_prompt.md
---

Lean tool-first prompt builder implemented (Apr 9 2026) to unblock O+V's first autonomous change:

- **`_build_lean_codegen_prompt`** in `providers.py` — sends ~3-6K tokens instead of ~9-35K
- **Sends**: task description, compressed strategic context (~150 tokens), structural index (function signatures), target region (~100 lines), tool instructions, output schema
- **Omits**: full file content, import context, test context, expanded context, full manifesto digest — model uses `read_file`/`search_code` to gather these
- **`_extract_target_region`**: smart region selection — finds function/class by name, centres on line references, falls back to file head
- **`_should_use_lean_prompt`**: routes to lean when tools enabled, not cross-repo, not repair iteration
- **Wired into**: DW RT path, ClaudeProvider, ClaudeAPI provider
- **Opt-out**: `JARVIS_LEAN_PROMPT=false`
- **MAX_TOOL_ITERATIONS**: raised 5 → 15 (env: `JARVIS_MAX_TOOL_ITERATIONS`)

**Why:** DW 397B burned entire time budget parsing 30-50K token mega-prompts before generating. CC pattern: minimal instruction + tool access. The skeleton (prompt) is deterministic; the nervous system (tool loop) is agentic.

**How to apply:** Default ON for all tool-loop-enabled providers. Batch path (no tools) still uses full prompt. Verify with debug.log: look for "using lean prompt (N chars, ~M tokens)".
