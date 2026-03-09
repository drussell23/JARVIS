# EventBridge → CommProtocol + J-Prime Multi-Repo Patch Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Close the final two gaps in JARVIS autonomous multi-repo development: (1) real-time narration of multi-repo governance activity via EventBridge → CommProtocol, and (2) J-Prime generating valid multi-repo patches (schema 2c.1) that the SagaApplyStrategy can apply across jarvis/prime/reactor-core.

**Architecture:**
Feature A wires `EventBridge` as a `CommProtocol` transport (so governance events reach `CrossRepoEventBus`) and adds a `CrossRepoNarrator` that converts inbound bus events into spoken narration via `CommProtocol`. Feature B extends `_build_codegen_prompt()` to include all cross-repo target files, adds schema `2c.1` with a per-repo `patches` dict, teaches `_parse_generation_response()` to build `RepoPatch` objects from 2c.1 responses, and wires the new path through `PrimeProvider`.

**Tech Stack:** Python 3.12, asyncio, existing CommProtocol transport protocol, CrossRepoEventBus, RepoPatch/PatchedFile from saga_types, pytest/asyncio_mode=auto.

---

## Key File Map

| File | Purpose |
|------|---------|
| `backend/core/ouroboros/governance/integration.py:438–545` | `create_governance_stack()` — wire EventBridge into CommProtocol here |
| `backend/core/ouroboros/governance/event_bridge.py:112–142` | `EventBridge.send()` — CommProtocol-compatible transport already exists |
| `backend/core/ouroboros/cross_repo.py:257–376` | `CrossRepoEventBus.register_handler(event_type, async_handler)` |
| `backend/core/ouroboros/cross_repo.py:87–197` | `EventType` enum — `IMPROVEMENT_REQUEST`, `IMPROVEMENT_COMPLETE`, `IMPROVEMENT_FAILED` |
| `backend/core/ouroboros/governance/comms/voice_narrator.py` | Pattern to follow for new `CrossRepoNarrator` |
| `backend/core/ouroboros/governance/comm_protocol.py:56–83` | `CommMessage` dataclass, `MessageType` enum |
| `backend/core/ouroboros/governance/providers.py:41–45` | `_CODEGEN_SYSTEM_PROMPT` |
| `backend/core/ouroboros/governance/providers.py:158–272` | `_build_codegen_prompt()` |
| `backend/core/ouroboros/governance/providers.py:298–424` | `_parse_generation_response()` — strict 2b.1 parser |
| `backend/core/ouroboros/governance/providers.py:432–528` | `PrimeProvider.__init__` / `generate()` |
| `backend/core/ouroboros/governance/saga/saga_types.py:12–65` | `FileOp`, `PatchedFile`, `RepoPatch` |
| `backend/core/ouroboros/governance/orchestrator.py:846–870` | `_execute_saga_apply()` — reads `best_candidate["patches"]` |

---

## Feature A: EventBridge → CommProtocol

### Task 1: Wire EventBridge into CommProtocol in create_governance_stack()

**The gap:** In `integration.py:455`, `comm = _build_comm_protocol(config=config)` is built BEFORE `_event_bridge` (line 479). So EventBridge is never in the transport list and governance events never reach CrossRepoEventBus.

**Fix:** Move `_event_bridge` construction to before `comm`, pass it as `extra_transports`.

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py:451–491`
- Test: `tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py`

**Step 1: Write the failing test**

```python
"""Tests that EventBridge is wired as a CommProtocol transport when event_bus is provided."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import pytest

async def test_event_bridge_added_to_comm_when_event_bus_provided():
    """EventBridge.send() must be called when comm.emit_intent() fires with event_bus present."""
    from backend.core.ouroboros.governance.integration import create_governance_stack
    from backend.core.ouroboros.governance.governance_config import GovernanceConfig
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        config = GovernanceConfig(project_root=Path(tmp))
        mock_event_bus = AsyncMock()
        mock_event_bus.emit = AsyncMock()

        stack = await create_governance_stack(config, event_bus=mock_event_bus)

        # Emit an intent — EventBridge should forward it to the bus
        await stack.comm.emit_intent(
            op_id="op-test-001",
            goal="Add utility function",
            target_files=["backend/core/utils.py"],
            risk_tier="safe_auto",
            blast_radius="low",
        )

        # EventBridge maps INTENT → IMPROVEMENT_REQUEST and emits to bus
        assert mock_event_bus.emit.called, "EventBridge.emit not called — not wired as transport"


async def test_no_event_bridge_when_event_bus_none():
    """When event_bus=None, GovernanceStack.event_bridge must be None."""
    from backend.core.ouroboros.governance.integration import create_governance_stack
    from backend.core.ouroboros.governance.governance_config import GovernanceConfig
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        config = GovernanceConfig(project_root=Path(tmp))
        stack = await create_governance_stack(config, event_bus=None)
        assert stack.event_bridge is None
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py -v
```
Expected: FAIL — `mock_event_bus.emit` is never called because EventBridge isn't in transports.

**Step 3: Implement — rearrange create_governance_stack() in integration.py**

In `create_governance_stack()` (lines 451–491), move event_bridge creation to BEFORE comm construction and wire as extra_transport. Replace lines 451–491 with:

```python
        # Build EventBridge FIRST so it can be wired as CommProtocol transport
        _event_bridge: Optional[Any] = None
        if event_bus is not None:
            try:
                _event_bridge = EventBridge(event_bus=event_bus)
                capabilities["event_bridge"] = CapabilityStatus(
                    enabled=True, reason="ok"
                )
            except Exception as exc:
                capabilities["event_bridge"] = CapabilityStatus(
                    enabled=False, reason=f"init_error: {exc}"
                )
        else:
            capabilities["event_bridge"] = CapabilityStatus(
                enabled=False, reason="dep_missing"
            )

        # Build CommProtocol — include EventBridge as extra transport if available
        _bridge_transports: List[Any] = [_event_bridge] if _event_bridge is not None else []
        comm = _build_comm_protocol(config=config, extra_transports=_bridge_transports)
```

Then DELETE the old `comm = _build_comm_protocol(config=config)` line (which was line 455) and the old `_event_bridge` block (old lines 476–490).

**Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py -v
```
Expected: PASS

**Step 5: Run broader suite to catch regressions**

```bash
python3 -m pytest tests/test_ouroboros_governance/ tests/governance/ -q --ignore=tests/test_ouroboros_governance/test_providers.py -x
```
Expected: all previously-passing tests still pass.

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py
git commit -m "feat(integration): wire EventBridge as CommProtocol transport in create_governance_stack"
```

---

### Task 2: Create CrossRepoNarrator for inbound bus events

**The gap:** When IMPROVEMENT events arrive on the CrossRepoEventBus (from Prime/Reactor-Core), nothing narrates them to the user. We need a handler class that converts `CrossRepoEvent` → CommProtocol calls → VoiceNarrator → safe_say.

**Files:**
- Create: `backend/core/ouroboros/governance/comms/cross_repo_narrator.py`
- Test: `tests/governance/comms/test_cross_repo_narrator.py`

**Step 1: Write the failing test**

```python
"""Tests for CrossRepoNarrator — inbound cross-repo event narration."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.core.ouroboros.cross_repo import CrossRepoEvent, EventType


def _make_event(event_type: EventType, repo: str = "prime", op_id: str = "op-001") -> CrossRepoEvent:
    return CrossRepoEvent(
        event_type=event_type,
        source_repo=repo,
        target_repo="jarvis",
        payload={"op_id": op_id, "goal": "Fix test", "reason_code": "validation_failed"},
        timestamp=0.0,
    )


async def test_improvement_request_calls_emit_intent():
    """IMPROVEMENT_REQUEST → comm.emit_intent with repo in goal."""
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator

    comm = MagicMock()
    comm.emit_intent = AsyncMock()
    narrator = CrossRepoNarrator(comm=comm)

    await narrator.on_improvement_request(_make_event(EventType.IMPROVEMENT_REQUEST, repo="prime"))

    comm.emit_intent.assert_awaited_once()
    call_kwargs = comm.emit_intent.call_args.kwargs
    assert "prime" in call_kwargs["goal"]


async def test_improvement_complete_calls_emit_decision_applied():
    """IMPROVEMENT_COMPLETE → comm.emit_decision with outcome=applied."""
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator

    comm = MagicMock()
    comm.emit_decision = AsyncMock()
    narrator = CrossRepoNarrator(comm=comm)

    await narrator.on_improvement_complete(_make_event(EventType.IMPROVEMENT_COMPLETE, repo="prime"))

    comm.emit_decision.assert_awaited_once()
    assert comm.emit_decision.call_args.kwargs["outcome"] == "applied"


async def test_improvement_failed_calls_emit_postmortem():
    """IMPROVEMENT_FAILED → comm.emit_postmortem."""
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator

    comm = MagicMock()
    comm.emit_postmortem = AsyncMock()
    narrator = CrossRepoNarrator(comm=comm)

    await narrator.on_improvement_failed(_make_event(EventType.IMPROVEMENT_FAILED, repo="reactor-core"))

    comm.emit_postmortem.assert_awaited_once()


async def test_handler_never_raises_on_bad_event():
    """Handler exceptions must not propagate — bus must stay healthy."""
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator

    comm = MagicMock()
    comm.emit_intent = AsyncMock(side_effect=RuntimeError("comm down"))
    narrator = CrossRepoNarrator(comm=comm)

    # Must not raise
    await narrator.on_improvement_request(_make_event(EventType.IMPROVEMENT_REQUEST))
```

**Step 2: Run to verify failure**

```bash
python3 -m pytest tests/governance/comms/test_cross_repo_narrator.py -v
```
Expected: FAIL — module not found.

**Step 3: Implement CrossRepoNarrator**

Create `backend/core/ouroboros/governance/comms/cross_repo_narrator.py`:

```python
"""CrossRepoNarrator: converts inbound CrossRepoEventBus events to CommProtocol narration.

Registered as a handler on CrossRepoEventBus so that improvement events arriving
from Prime / Reactor-Core are spoken aloud via the CommProtocol → VoiceNarrator chain.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    from backend.core.ouroboros.cross_repo import CrossRepoEvent

logger = logging.getLogger(__name__)

_SOURCE = "cross_repo_narrator"


class CrossRepoNarrator:
    """Handles inbound CrossRepoEventBus events and routes them to CommProtocol.

    Usage::

        narrator = CrossRepoNarrator(comm=stack.comm)
        event_bus.register_handler(EventType.IMPROVEMENT_REQUEST, narrator.on_improvement_request)
        event_bus.register_handler(EventType.IMPROVEMENT_COMPLETE, narrator.on_improvement_complete)
        event_bus.register_handler(EventType.IMPROVEMENT_FAILED, narrator.on_improvement_failed)
    """

    def __init__(self, comm: "CommProtocol") -> None:
        self._comm = comm

    # ------------------------------------------------------------------
    # Handlers — each must never raise (bus safety contract)
    # ------------------------------------------------------------------

    async def on_improvement_request(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_REQUEST: narrate that JARVIS detected work in a remote repo."""
        try:
            repo = getattr(event, "source_repo", "unknown-repo")
            op_id = event.payload.get("op_id", f"cross-{repo}-unknown")
            goal = event.payload.get("goal", "improvement detected")
            await self._comm.emit_intent(
                op_id=op_id,
                goal=f"[{repo}] {goal}",
                target_files=[],
                risk_tier="unknown",
                blast_radius="cross_repo",
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_request failed; swallowing")

    async def on_improvement_complete(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_COMPLETE: narrate that a remote repo change was applied."""
        try:
            repo = getattr(event, "source_repo", "unknown-repo")
            op_id = event.payload.get("op_id", f"cross-{repo}-unknown")
            await self._comm.emit_decision(
                op_id=op_id,
                outcome="applied",
                reason_code="cross_repo_applied",
                diff_summary=f"Change applied to {repo}",
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_complete failed; swallowing")

    async def on_improvement_failed(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_FAILED: narrate that a remote repo change failed."""
        try:
            repo = getattr(event, "source_repo", "unknown-repo")
            op_id = event.payload.get("op_id", f"cross-{repo}-unknown")
            reason = event.payload.get("reason_code", "unknown_failure")
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=reason,
                failed_phase="apply",
                next_safe_action="review_cross_repo_logs",
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_failed failed; swallowing")
```

**Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/governance/comms/test_cross_repo_narrator.py -v
```
Expected: 4 PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/cross_repo_narrator.py tests/governance/comms/test_cross_repo_narrator.py
git commit -m "feat(comms): add CrossRepoNarrator to translate inbound bus events to CommProtocol narration"
```

---

### Task 3: Register CrossRepoNarrator handlers in create_governance_stack()

**The gap:** `CrossRepoNarrator` exists but nothing registers its handlers on the bus.

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Test: `tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py` (extend existing file)

**Step 1: Add a test**

Add to `tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py`:

```python
async def test_cross_repo_narrator_registered_when_event_bus_provided():
    """CrossRepoNarrator handlers must be registered on the bus when event_bus is provided."""
    from backend.core.ouroboros.governance.integration import create_governance_stack
    from backend.core.ouroboros.governance.governance_config import GovernanceConfig
    from backend.core.ouroboros.cross_repo import EventType, CrossRepoEvent
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        config = GovernanceConfig(project_root=Path(tmp))

        # Real event bus with handler tracking
        received: list = []
        mock_event_bus = AsyncMock()
        mock_event_bus.emit = AsyncMock()
        registered_handlers: dict = {}

        def fake_register(event_type, handler):
            registered_handlers[event_type] = handler

        mock_event_bus.register_handler = fake_register

        stack = await create_governance_stack(config, event_bus=mock_event_bus)

        # All three event types must have handlers registered
        from backend.core.ouroboros.cross_repo import EventType
        assert EventType.IMPROVEMENT_REQUEST in registered_handlers
        assert EventType.IMPROVEMENT_COMPLETE in registered_handlers
        assert EventType.IMPROVEMENT_FAILED in registered_handlers
```

**Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py::test_cross_repo_narrator_registered_when_event_bus_provided -v
```
Expected: FAIL

**Step 3: Register handlers in create_governance_stack()**

In `integration.py`, inside `create_governance_stack()`, after the `_event_bridge` block and the `comm` construction, add:

```python
        # Register CrossRepoNarrator on event_bus for inbound narration
        if event_bus is not None and _event_bridge is not None:
            try:
                from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator
                from backend.core.ouroboros.cross_repo import EventType
                narrator = CrossRepoNarrator(comm=comm)
                event_bus.register_handler(EventType.IMPROVEMENT_REQUEST, narrator.on_improvement_request)
                event_bus.register_handler(EventType.IMPROVEMENT_COMPLETE, narrator.on_improvement_complete)
                event_bus.register_handler(EventType.IMPROVEMENT_FAILED, narrator.on_improvement_failed)
                logger.info("[Integration] CrossRepoNarrator registered on event bus")
            except Exception as exc:
                logger.warning("[Integration] CrossRepoNarrator registration failed: %s", exc)
```

**Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py -v
```
Expected: all 3 tests PASS

**Step 5: Full suite check**

```bash
python3 -m pytest tests/test_ouroboros_governance/ tests/governance/ -q --ignore=tests/test_ouroboros_governance/test_providers.py -x
```

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py
git commit -m "feat(integration): register CrossRepoNarrator handlers on CrossRepoEventBus at stack startup"
```

---

## Feature B: J-Prime Multi-Repo Patch (schema 2c.1)

### Task 4: Extend _build_codegen_prompt() for multi-repo context

**The gap:** `_build_codegen_prompt()` reads `ctx.target_files` as a flat list without knowing which repo each file belongs to. For cross-repo ops, the prompt must group files by repo and request schema 2c.1 patches.

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (functions `_build_codegen_prompt`, `_CODEGEN_SYSTEM_PROMPT`)
- Test: `tests/test_ouroboros_governance/test_providers_multi_repo.py`

**Step 1: Write the failing test**

```python
"""Tests for multi-repo codegen prompt building (schema 2c.1)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _make_cross_repo_ctx(tmp_path: Path):
    """Build a cross-repo OperationContext with files in two repos."""
    from backend.core.ouroboros.governance.op_context import OperationContext
    jarvis_file = str(tmp_path / "jarvis" / "backend" / "utils.py")
    prime_file = str(tmp_path / "prime" / "api" / "handler.py")
    # Create the files so prompt builder can read them
    Path(jarvis_file).parent.mkdir(parents=True, exist_ok=True)
    Path(prime_file).parent.mkdir(parents=True, exist_ok=True)
    Path(jarvis_file).write_text("def hello(): pass\n")
    Path(prime_file).write_text("def handle(): pass\n")

    return OperationContext.create(
        target_files=(jarvis_file, prime_file),
        description="Add cross-repo feature",
        op_id="op-multi-001",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
    )


def test_cross_repo_prompt_includes_schema_2c1(tmp_path):
    """Prompt for cross_repo ctx must reference schema_version 2c.1."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = _make_cross_repo_ctx(tmp_path)
    assert ctx.cross_repo is True

    repo_roots = {
        "jarvis": tmp_path / "jarvis",
        "prime": tmp_path / "prime",
    }
    prompt = _build_codegen_prompt(ctx, repo_roots=repo_roots)
    assert "2c.1" in prompt, "Cross-repo prompt must specify schema 2c.1"
    assert "patches" in prompt, "Cross-repo prompt must describe patches dict"


def test_cross_repo_prompt_groups_files_by_repo(tmp_path):
    """Prompt must label each file with its repo name."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = _make_cross_repo_ctx(tmp_path)
    repo_roots = {
        "jarvis": tmp_path / "jarvis",
        "prime": tmp_path / "prime",
    }
    prompt = _build_codegen_prompt(ctx, repo_roots=repo_roots)
    assert "jarvis" in prompt
    assert "prime" in prompt


def test_single_repo_prompt_unchanged(tmp_path):
    """Single-repo ctx must still produce schema 2b.1 prompt (no regression)."""
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt

    f = tmp_path / "backend" / "utils.py"
    f.parent.mkdir(parents=True)
    f.write_text("def hello(): pass\n")

    ctx = OperationContext.create(
        target_files=(str(f),),
        description="Add utility",
        op_id="op-single-001",
    )
    assert ctx.cross_repo is False

    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "2b.1" in prompt
    assert "2c.1" not in prompt
```

**Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers_multi_repo.py -v
```
Expected: FAIL — `_build_codegen_prompt` doesn't accept `repo_roots` and doesn't produce 2c.1.

**Step 3: Implement**

In `providers.py`:

1. Add constant after line 45:
```python
_SCHEMA_VERSION_MULTI = "2c.1"
```

2. Change `_build_codegen_prompt` signature to accept optional `repo_roots`:
```python
def _build_codegen_prompt(
    ctx: "OperationContext",
    repo_root: Optional[Path] = None,
    repo_roots: Optional[Dict[str, Path]] = None,
) -> str:
```

3. When `ctx.cross_repo` and `repo_roots` is provided, use multi-repo schema instruction instead of the 2b.1 one. Add after the `context_block` block (around line 236):

```python
    # ── 3. Output schema instruction ────────────────────────────────────
    if ctx.cross_repo and repo_roots:
        repos_listed = "\n".join(f'      "{r}": [...]' for r in ctx.repo_scope)
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
```json
{{
  "file_path": "<path relative to that repo's root>",
  "full_content": "<complete modified file — not a diff>",
  "op": "modify"
}}
```

Rules:
- Return 1–3 candidates. c1 = primary approach, c2 = alternative.
- `full_content` must be the **complete** file.
- Python files must be syntactically valid (`ast.parse()`-clean).
- Only include repos that actually require changes. Omit unchanged repos.
- No extra keys at any level. Return ONLY the JSON object."""
    else:
        schema_instruction = f"""## Output Schema
...existing 2b.1 schema_instruction content..."""
```

> **IMPORTANT**: Do NOT copy-paste the `else` branch. Instead, wrap the existing `schema_instruction = f"""..."""` block (lines 237–263) in an `else:` clause. The `if ctx.cross_repo` block goes immediately before it.

4. Also group file sections by repo when cross_repo (in section 1 of the function, around line 173):

```python
    # ── 1. Build source snapshot for each target file ──────────────────
    file_sections: List[str] = []
    for raw_path in ctx.target_files:
        # Determine which repo this file belongs to (for cross-repo labelling)
        repo_label = ""
        if ctx.cross_repo and repo_roots:
            for repo_name, root in repo_roots.items():
                try:
                    rel = Path(raw_path).relative_to(root)
                    repo_label = f" [{repo_name}]"
                    break
                except ValueError:
                    pass
        # ... rest of existing file reading logic, just prepend repo_label to section header
```

Add `repo_label` to the file section header (e.g., `f"### {rel_path}{repo_label}"` or similar).

**Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers_multi_repo.py -v
```
Expected: 3 PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py tests/test_ouroboros_governance/test_providers_multi_repo.py
git commit -m "feat(providers): extend _build_codegen_prompt for cross-repo ops with schema 2c.1"
```

---

### Task 5: Add schema 2c.1 parsing to _parse_generation_response()

**The gap:** `_parse_generation_response()` only accepts schema 2b.1. A 2c.1 response (with `patches` dict per repo) raises a RuntimeError on schema_version mismatch.

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (`_parse_generation_response` + new helper `_parse_multi_repo_response`)
- Test: `tests/test_ouroboros_governance/test_providers_multi_repo.py` (extend)

**Step 1: Add parsing tests**

Add to `tests/test_ouroboros_governance/test_providers_multi_repo.py`:

```python
import json
from pathlib import Path


def _make_single_ctx(tmp_path: Path):
    from backend.core.ouroboros.governance.op_context import OperationContext
    return OperationContext.create(
        target_files=(str(tmp_path / "utils.py"),),
        description="Add util",
        op_id="op-parse-001",
    )


def _make_multi_ctx(tmp_path: Path, repos=("jarvis", "prime")):
    from backend.core.ouroboros.governance.op_context import OperationContext
    files = []
    for repo in repos:
        f = tmp_path / repo / "api.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("def api(): pass\n")
        files.append(str(f))
    return OperationContext.create(
        target_files=tuple(files),
        description="Cross-repo fix",
        op_id="op-parse-multi-001",
        repo_scope=repos,
        primary_repo=repos[0],
    )


def _valid_2c1_response(repos=("jarvis", "prime")):
    patches = {
        repo: [
            {
                "file_path": "api.py",
                "full_content": f"def api(): return '{repo}'\n",
                "op": "modify",
            }
        ]
        for repo in repos
    }
    return json.dumps({
        "schema_version": "2c.1",
        "candidates": [
            {
                "candidate_id": "c1",
                "patches": patches,
                "rationale": "Fixed cross-repo API",
            }
        ],
        "provider_metadata": {"model_id": "test-model", "reasoning_summary": "ok"},
    })


def test_parse_valid_2c1_response(tmp_path):
    """Valid 2c.1 response must produce GenerationResult with RepoPatch candidates."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response
    from backend.core.ouroboros.governance.saga.saga_types import RepoPatch

    ctx = _make_multi_ctx(tmp_path)
    repo_roots = {
        "jarvis": tmp_path / "jarvis",
        "prime": tmp_path / "prime",
    }
    raw = _valid_2c1_response()
    result = _parse_generation_response(
        raw, "gcp-jprime", 0.5, ctx, "hash-001", "api.py", repo_roots=repo_roots
    )

    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert "patches" in cand
    assert "jarvis" in cand["patches"]
    assert "prime" in cand["patches"]
    assert isinstance(cand["patches"]["jarvis"], RepoPatch)
    assert isinstance(cand["patches"]["prime"], RepoPatch)


def test_parse_2c1_file_content_written_to_repopatch(tmp_path):
    """RepoPatch.new_content must contain the file bytes from the 2c.1 response."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    ctx = _make_multi_ctx(tmp_path)
    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}
    raw = _valid_2c1_response()
    result = _parse_generation_response(
        raw, "gcp-jprime", 0.5, ctx, "hash-001", "api.py", repo_roots=repo_roots
    )

    jarvis_patch = result.candidates[0]["patches"]["jarvis"]
    contents = dict(jarvis_patch.new_content)
    assert "api.py" in contents
    assert b"return 'jarvis'" in contents["api.py"]


def test_parse_2c1_rejects_invalid_schema(tmp_path):
    """2c.1 response missing required patch fields must raise RuntimeError."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    ctx = _make_multi_ctx(tmp_path)
    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}

    bad = json.dumps({
        "schema_version": "2c.1",
        "candidates": [
            {"candidate_id": "c1", "patches": {"jarvis": [{"bad_field": "x"}]}, "rationale": "x"}
        ],
        "provider_metadata": {"model_id": "m", "reasoning_summary": "s"},
    })
    import pytest
    with pytest.raises(RuntimeError):
        _parse_generation_response(bad, "gcp-jprime", 0.5, ctx, "h", "f", repo_roots=repo_roots)


def test_2b1_response_still_works_after_change(tmp_path):
    """Existing schema 2b.1 single-repo responses must still parse correctly."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    f = tmp_path / "utils.py"
    f.write_text("def hello(): pass\n")
    ctx = _make_single_ctx(tmp_path)

    raw = json.dumps({
        "schema_version": "2b.1",
        "candidates": [
            {"candidate_id": "c1", "file_path": "utils.py", "full_content": "def hello(): return 1\n", "rationale": "test"}
        ],
        "provider_metadata": {"model_id": "m", "reasoning_summary": "s"},
    })
    result = _parse_generation_response(raw, "gcp-jprime", 0.5, ctx, "h", "utils.py")
    assert len(result.candidates) == 1
    assert "file_path" in result.candidates[0]
```

**Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers_multi_repo.py -v
```
Expected: new 4 tests FAIL — `_parse_generation_response` doesn't accept `repo_roots` or 2c.1.

**Step 3: Implement**

In `providers.py`, add `repo_roots` parameter to `_parse_generation_response` and add a 2c.1 detection branch:

```python
def _parse_generation_response(
    raw: str,
    provider_name: str,
    duration_s: float,
    ctx: "OperationContext",
    source_hash: str,
    source_path: str,
    repo_roots: Optional[Dict[str, "Path"]] = None,
) -> "GenerationResult":
    """Parse and strictly validate a generation response.

    Supports:
    - schema_version 2b.1: single-repo candidate with file_path + full_content
    - schema_version 2c.1: cross-repo candidate with patches dict per repo
    """
    pfx = provider_name
    # ... existing JSON parse + top-level validation ...

    schema_version = data.get("schema_version")

    if schema_version == _SCHEMA_VERSION_MULTI:
        return _parse_multi_repo_response(data, provider_name, duration_s, ctx, repo_roots or {})

    # ... existing 2b.1 validation continues unchanged ...
```

Add new helper `_parse_multi_repo_response()`:

```python
def _parse_multi_repo_response(
    data: dict,
    provider_name: str,
    duration_s: float,
    ctx: "OperationContext",
    repo_roots: Dict[str, "Path"],
) -> "GenerationResult":
    """Parse schema 2c.1 multi-repo response into GenerationResult with RepoPatch candidates."""
    from backend.core.ouroboros.governance.saga.saga_types import (
        FileOp,
        PatchedFile,
        RepoPatch,
    )

    raw_candidates = data.get("candidates", [])
    if not raw_candidates or not isinstance(raw_candidates, list):
        raise RuntimeError(f"{provider_name}_schema_invalid:no_candidates:2c.1")

    validated: list = []
    for raw_cand in raw_candidates[:3]:  # cap at 3
        patches_raw = raw_cand.get("patches")
        if not isinstance(patches_raw, dict):
            raise RuntimeError(f"{provider_name}_schema_invalid:missing_patches:2c.1")

        repo_patches: Dict[str, RepoPatch] = {}
        for repo_name, file_list in patches_raw.items():
            if not isinstance(file_list, list):
                raise RuntimeError(f"{provider_name}_schema_invalid:patches_not_list:{repo_name}")

            patched_files: list = []
            new_content: list = []

            for file_entry in file_list:
                file_path = file_entry.get("file_path")
                full_content = file_entry.get("full_content")
                op_str = file_entry.get("op", "modify")

                if not file_path or full_content is None:
                    raise RuntimeError(
                        f"{provider_name}_schema_invalid:missing_file_fields:{repo_name}:{file_path}"
                    )

                # AST check for Python files
                if str(file_path).endswith(".py"):
                    try:
                        import ast
                        ast.parse(full_content)
                    except SyntaxError as e:
                        raise RuntimeError(
                            f"{provider_name}_schema_invalid:syntax_error:{repo_name}:{file_path}:{e}"
                        )

                op = FileOp(op_str) if op_str in FileOp._value2member_map_ else FileOp.MODIFY

                # Read preimage for MODIFY ops
                preimage: Optional[bytes] = None
                if op in (FileOp.MODIFY, FileOp.DELETE):
                    repo_root = repo_roots.get(repo_name)
                    if repo_root is not None:
                        full_disk_path = Path(repo_root) / file_path
                        try:
                            preimage = full_disk_path.read_bytes()
                        except OSError:
                            preimage = b""  # file missing on disk — treat as create
                            op = FileOp.CREATE
                    else:
                        preimage = b""

                patched_files.append(PatchedFile(path=file_path, op=op, preimage=preimage))
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
        raise RuntimeError(f"{provider_name}_schema_invalid:all_candidates_failed:2c.1")

    model_id = data.get("provider_metadata", {}).get("model_id", provider_name)
    return GenerationResult(
        candidates=tuple(validated),
        provider_name=provider_name,
        generation_duration_s=duration_s,
        model_id=model_id,
    )
```

**Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers_multi_repo.py -v
```
Expected: all tests PASS

**Step 5: Run full providers test suite to check no regressions**

```bash
python3 -m pytest tests/test_ouroboros_governance/ -k "provider" -v
```

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py tests/test_ouroboros_governance/test_providers_multi_repo.py
git commit -m "feat(providers): add schema 2c.1 multi-repo patch parsing with RepoPatch construction"
```

---

### Task 6: Wire multi-repo path in PrimeProvider and update system prompt

**The gap:** `PrimeProvider` only holds a single `repo_root: Path`. For cross-repo ops, it needs `repo_roots: Dict[str, Path]` to pass to `_build_codegen_prompt()` and `_parse_generation_response()`.

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (PrimeProvider + system prompt)
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (pass repo_roots to PrimeProvider)
- Test: `tests/test_ouroboros_governance/test_providers_multi_repo.py` (extend)

**Step 1: Add PrimeProvider multi-repo test**

Add to `tests/test_ouroboros_governance/test_providers_multi_repo.py`:

```python
async def test_prime_provider_generates_multi_repo(tmp_path):
    """PrimeProvider.generate() must produce candidates with patches dict for cross-repo ctx."""
    from backend.core.ouroboros.governance.providers import PrimeProvider
    from backend.core.ouroboros.governance.op_context import OperationContext
    from unittest.mock import AsyncMock, MagicMock
    from datetime import datetime, timezone, timedelta
    import json

    jarvis_file = tmp_path / "jarvis" / "api.py"
    prime_file = tmp_path / "prime" / "api.py"
    jarvis_file.parent.mkdir(parents=True)
    prime_file.parent.mkdir(parents=True)
    jarvis_file.write_text("def jarvis_api(): pass\n")
    prime_file.write_text("def prime_api(): pass\n")

    ctx = OperationContext.create(
        target_files=(str(jarvis_file), str(prime_file)),
        description="Sync APIs",
        op_id="op-pprov-001",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
    )
    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}

    # Mock J-Prime returning a 2c.1 response
    mock_response = MagicMock()
    mock_response.content = json.dumps({
        "schema_version": "2c.1",
        "candidates": [{
            "candidate_id": "c1",
            "patches": {
                "jarvis": [{"file_path": "api.py", "full_content": "def jarvis_api(): return 1\n", "op": "modify"}],
                "prime":  [{"file_path": "api.py", "full_content": "def prime_api(): return 1\n",  "op": "modify"}],
            },
            "rationale": "Synced",
        }],
        "provider_metadata": {"model_id": "test", "reasoning_summary": "ok"},
    })
    mock_client = MagicMock()
    mock_client.generate = AsyncMock(return_value=mock_response)

    provider = PrimeProvider(mock_client, repo_roots=repo_roots)
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    result = await provider.generate(ctx, deadline)

    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert "patches" in cand
    assert "jarvis" in cand["patches"]
    assert "prime" in cand["patches"]
```

**Step 2: Run to verify failure**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers_multi_repo.py::test_prime_provider_generates_multi_repo -v
```
Expected: FAIL — PrimeProvider doesn't accept `repo_roots`.

**Step 3: Implement**

In `providers.py`, update `PrimeProvider.__init__()`:

```python
def __init__(
    self,
    prime_client: Any,
    max_tokens: int = 8192,
    repo_root: Optional[Path] = None,
    repo_roots: Optional[Dict[str, Path]] = None,  # NEW: multi-repo roots
) -> None:
    self._client = prime_client
    self._max_tokens = max_tokens
    self._repo_root = repo_root
    self._repo_roots = repo_roots  # NEW
```

Update `PrimeProvider.generate()` to pass `repo_roots` to both `_build_codegen_prompt` and `_parse_generation_response`:

```python
    async def generate(self, context: OperationContext, deadline: datetime) -> GenerationResult:
        prompt = _build_codegen_prompt(
            context,
            repo_root=self._repo_root,
            repo_roots=self._repo_roots,  # NEW
        )
        # ... existing client.generate() call ...
        result = _parse_generation_response(
            response.content,
            self.provider_name,
            duration,
            context,
            source_hash,
            source_path,
            repo_roots=self._repo_roots,  # NEW
        )
        return result
```

Update `_CODEGEN_SYSTEM_PROMPT` to accept both schemas:

```python
_CODEGEN_SYSTEM_PROMPT = (
    "You are a precise code modification assistant for the JARVIS multi-repo ecosystem. "
    "For single-repo requests respond with schema_version 2b.1. "
    "For cross-repo requests (where the prompt specifies schema_version 2c.1) "
    "respond with schema_version 2c.1 and a patches dict keyed by repo name. "
    "You MUST respond with valid JSON only. "
    "No markdown preamble, no explanations outside the JSON. Only the JSON object."
)
```

Update `governed_loop_service.py` `_build_components()` to pass `repo_roots` to PrimeProvider:

Find where `primary = PrimeProvider(self._prime_client, repo_root=self._config.project_root)` is created (around line 575) and change to:

```python
# Build repo_roots dict from registry for multi-repo generation
_repo_roots_map: Optional[Dict[str, Path]] = None
if 'repo_registry' in dir() and repo_registry is not None:
    _repo_roots_map = {
        rc.name: rc.local_path
        for rc in repo_registry.list_enabled()
    }

primary = PrimeProvider(
    self._prime_client,
    repo_root=self._config.project_root,
    repo_roots=_repo_roots_map,
)
```

> NOTE: `repo_registry` is constructed earlier in `_build_components()` so the variable is in scope. The name check is just defensive — use it directly.

**Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers_multi_repo.py -v
```
Expected: all tests PASS

**Step 5: Run full governance suite**

```bash
python3 -m pytest tests/test_ouroboros_governance/ tests/governance/ -q --ignore=tests/test_ouroboros_governance/test_providers.py -x
```

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py backend/core/ouroboros/governance/governed_loop_service.py tests/test_ouroboros_governance/test_providers_multi_repo.py
git commit -m "feat(providers): wire PrimeProvider with repo_roots for cross-repo generation via schema 2c.1"
```

---

## Final Verification

After all 6 tasks complete:

```bash
# Run all new tests
python3 -m pytest tests/test_ouroboros_governance/test_event_bridge_comm_wiring.py tests/governance/comms/test_cross_repo_narrator.py tests/test_ouroboros_governance/test_providers_multi_repo.py -v

# Run full governance suite (pre-existing failures in test_providers.py are unrelated)
python3 -m pytest tests/test_ouroboros_governance/ tests/governance/ -q --ignore=tests/test_ouroboros_governance/test_providers.py
```

Both features are done when all new tests pass and the pre-existing pass count is unchanged.
