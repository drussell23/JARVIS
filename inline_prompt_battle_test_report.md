# InlinePromptGate Slice 5b — Battle Test Report

**Branch**: `ouroboros/inline-prompt-gate/slice-5b-wireup`
**Date**: 2026-05-10
**Investigator**: Claude Code (claude-sonnet-4-6)

---

## Task 1 Finding: Wire-Up Already Present

**The `attach_phase_boundary_renderer` wire-up was already implemented prior to this session.**
Grep-first discipline applied per operating instructions — no redundant commit made.

### Evidence

```
$ grep -n 'attach_phase_boundary_renderer' \
    backend/core/ouroboros/battle_test/serpent_flow.py

635:                attach_phase_boundary_renderer,
638:                attach_phase_boundary_renderer(self.console.print)
```

**Location**: `backend/core/ouroboros/battle_test/serpent_flow.py:633–641`

```python
try:
    from backend.core.ouroboros.governance.inline_prompt_gate_renderer import (
        attach_phase_boundary_renderer,
    )
    self._unsub_inline_prompt_renderer: Callable[[], None] = (
        attach_phase_boundary_renderer(self.console.print)
    )
except Exception:
    self._unsub_inline_prompt_renderer = lambda: None
```

The implementation satisfies all requirements from the task brief:
- `self.console.print` is passed as the print callback (constructed before this block)
- Wrapped in `try/except Exception` — never blocks boot if controller resolution fails
- Lazy import mirrors the `StreamRenderer` pattern above it
- Returns an unsubscribe callable stored in `self._unsub_inline_prompt_renderer`
- No flag guard needed here: `attach_phase_boundary_renderer` itself calls
  `get_default_controller()`, which is a no-op if the controller is unavailable

### AST Validation

```
$ python3 -c "import ast; ast.parse(open(
    'backend/core/ouroboros/battle_test/serpent_flow.py').read()); print('AST OK')"
AST OK
```

### Inline-Prompt Test Suite

All 44 tests green after installing missing dependencies into pytest's uv
isolated environment:

```
tests/governance/test_inline_prompt_gate_renderer.py     39 passed
tests/governance/test_inline_prompt_gate_5b_serpent_wireup.py  5 passed
Total: 44 passed, 2 warnings in 0.86s
```

The `test_inline_prompt_gate_5b_serpent_wireup.py` suite includes
`TestEndToEndViaSerpentFlow::test_phase_boundary_prompt_renders_to_serpent_console`,
which directly exercises the `__init__` wire-up path that Task 1 describes.

---

## Task 2: Battle Test Run

### Attempt

```
JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED=false \
python3 scripts/ouroboros_battle_test.py \
    --headless \
    --cost-cap 0.50 \
    --idle-timeout 600 \
    --max-wall-seconds 2400 \
    -v
```

### Result: Boot Failure — No API Keys

```
  Preflight Checklist
  ────────────────────────────────────────────────────
  [OFF] Provider: DoubleWord 397B      $0.10/$0.40/M (Tier 0 PRIMARY)
  [OFF] Provider: Claude Sonnet        $3/$15/M (Tier 1 FALLBACK)
  ...
  ERROR: No API keys set.
  Export DOUBLEWORD_API_KEY or ANTHROPIC_API_KEY.
```

**Stop reason**: `sys.exit(1)` at preflight, before harness boot.

**Session ID**: None — harness never reached the session-creation phase.

**`session_outcome`**: N/A — no `summary.json` written (only written after
`_build_session_dir()`, which fires after preflight passes).

### Root Cause

Neither `ANTHROPIC_API_KEY` nor `DOUBLEWORD_API_KEY` is set in the execution
environment. The `anthropic` Python package is also not installed (not present
in `/usr/local/lib/python3.11/dist-packages` or `/root/.local/lib/`).

The harness contains a hard-fail guard at
`scripts/ouroboros_battle_test.py:469–472`:

```python
has_dw = bool(os.environ.get("DOUBLEWORD_API_KEY"))
has_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
if not has_dw and not has_claude:
    print(f"ERROR: No API keys set.")
    sys.exit(1)
```

This guard fires before the 6-layer stack is assembled — no asyncio loop is
started, no session directory is created, no `InlinePromptController` boot
path is exercised.

### Single-Flight Guard Note

An initial run attempt without `JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED=false`
was rejected with exit code 75 (`EX_TEMPFAIL`):

```
[single-flight] REJECTED — concurrent battle-test detected
  • pgrep: PID 16306
```

PID 16306 was confirmed dead (`kill -0 16306` → "No such process"). The
pgrep probe likely caught a sibling process from the same bash session in a
timing window (the pgrep fires before the current process sets its own PID
context). The second run with the flag bypassed this correctly and reached the
real failure: missing API keys.

---

## Renderer-Attach Evidence Summary

| Question | Answer |
|----------|--------|
| Was `attach_phase_boundary_renderer` call present in `__init__`? | **Yes** — line 638 |
| Was it already shipped before this session? | **Yes** — committed by previous O+V op |
| Does `serpent_flow.py` AST-parse cleanly? | **Yes** |
| Do all 44 inline-prompt tests pass? | **Yes** |
| Did the battle test reach the renderer-attach boot path? | **No** — boot aborted at preflight |
| Were any NOTIFY_APPLY operations observed? | **No** — harness never started |
| Were any phase-boundary prompts silently dropped? | **Unknown** — cannot determine without a live session |
| Did the wire-up break boot? | **No** — the early-exit is due to missing API keys, not the renderer |

---

## Regression Assessment

The wire-up (`attach_phase_boundary_renderer(self.console.print)`) is
structurally sound:

1. **Lazy import** — failure to import the renderer module (e.g., on a
   stripped install) falls through to a no-op lambda; SerpentFlow boots
   regardless.
2. **Exception guard** — any controller resolution failure (missing
   `get_default_controller`, wrong import path) is caught; never propagates
   to the caller.
3. **44/44 tests pass** — the test suite directly verifies the end-to-end
   path from `SerpentFlow.__init__` through `attach_phase_boundary_renderer`
   to the operator console.

---

## Prerequisites to Run Task 2 End-to-End

To obtain empirical battle-test evidence of the inline-prompt UX:

```bash
export ANTHROPIC_API_KEY=<key>          # or DOUBLEWORD_API_KEY=<key>
pip install anthropic                   # if not already installed

python3 scripts/ouroboros_battle_test.py \
    --headless \
    --cost-cap 0.50 \
    --idle-timeout 600 \
    --max-wall-seconds 2400 \
    -v 2>&1 | tee /tmp/inline_prompt_battle_test.log

# Verify renderer attach:
grep -i 'inline_prompt\|phase_boundary_renderer\|InlinePromptGateRenderer' \
    /tmp/inline_prompt_battle_test.log

# Verify session outcome:
jq '.session_outcome, .stop_reason' \
    .ouroboros/sessions/<session-id>/summary.json
```

Grep evidence to look for on a successful run:

```
[InlinePromptGateRenderer] attached controller=InlinePromptController
    print_cb=Console.print
```

This log line is emitted by `attach_phase_boundary_renderer` at INFO level
(`governance/inline_prompt_gate_renderer.py:404–409`) when the controller
subscription succeeds.
