# Context Expansion Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement Phase 1, Item 1 — insert a bounded CONTEXT_EXPANSION phase between ROUTE and GENERATE that lets J-Prime/Claude request additional files before code generation.

**Architecture:** A new `CONTEXT_EXPANSION` phase is added to the Ouroboros state machine. A `ContextExpander` class drives up to 2 expansion rounds (hardcoded governor): each round sends a lightweight planning prompt to the generator, parses the `expansion.1` JSON response, and reads confirmed files from disk. The enriched paths are stored in `OperationContext.expanded_context_files` via a new `with_expanded_files()` helper (no phase change). `_build_codegen_prompt()` then injects those files as read-only context sections for the GENERATE phase.

**Tech Stack:** Python 3.11+, asyncio, frozen dataclasses, pytest-asyncio (asyncio_mode = auto — never use `@pytest.mark.asyncio`).

---

### Task 1: Add CONTEXT_EXPANSION to op_context.py

**Files:**
- Modify: `backend/core/ouroboros/governance/op_context.py`
- Test: `tests/test_ouroboros_governance/test_op_context.py`

**Step 1: Write the failing tests**

Add this class at the end of `tests/test_ouroboros_governance/test_op_context.py`:

```python
class TestContextExpansionPhase:
    """CONTEXT_EXPANSION phase state machine additions."""

    def test_context_expansion_in_enum(self):
        from backend.core.ouroboros.governance.op_context import OperationPhase
        assert hasattr(OperationPhase, "CONTEXT_EXPANSION")

    def test_route_to_context_expansion_legal(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        assert ctx.phase is OperationPhase.CONTEXT_EXPANSION

    def test_context_expansion_to_generate_legal(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.advance(OperationPhase.GENERATE)
        assert ctx.phase is OperationPhase.GENERATE

    def test_context_expansion_to_cancelled_legal(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.advance(OperationPhase.CANCELLED)
        assert ctx.phase is OperationPhase.CANCELLED

    def test_route_to_generate_still_legal_direct(self):
        """ROUTE → GENERATE direct path must remain valid (expansion is optional)."""
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.GENERATE)
        assert ctx.phase is OperationPhase.GENERATE

    def test_expanded_context_files_default_empty(self):
        from backend.core.ouroboros.governance.op_context import OperationContext
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        assert ctx.expanded_context_files == ()

    def test_with_expanded_files_updates_field(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        enriched = ctx.with_expanded_files(("helpers.py", "utils.py"))
        assert enriched.expanded_context_files == ("helpers.py", "utils.py")
        assert enriched.phase is OperationPhase.CONTEXT_EXPANSION

    def test_with_expanded_files_updates_hash_chain(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        enriched = ctx.with_expanded_files(("helpers.py",))
        # previous_hash must chain from ctx
        assert enriched.previous_hash == ctx.context_hash
        # context_hash must differ after field update
        assert enriched.context_hash != ctx.context_hash
```

**Step 2: Run test to verify it fails**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py::TestContextExpansionPhase -v
```
Expected: FAIL — `AttributeError: CONTEXT_EXPANSION` or similar.

**Step 3: Implement changes in op_context.py**

*3a.* After line 63 (`ROUTE = auto()`), insert:
```python
    CONTEXT_EXPANSION = auto()
```

*3b.* Update the module-level docstring diagram (lines 17–25) to show the new phase:
```
    CLASSIFY -> ROUTE -> [CONTEXT_EXPANSION] -> GENERATE -> VALIDATE -> GATE -> APPROVE -> APPLY -> VERIFY -> COMPLETE
```

*3c.* Update `PHASE_TRANSITIONS` — change the ROUTE entry (lines 87–90) to:
```python
    OperationPhase.ROUTE: {
        OperationPhase.CONTEXT_EXPANSION,
        OperationPhase.GENERATE,
        OperationPhase.CANCELLED,
    },
```
Then add a new CONTEXT_EXPANSION entry immediately after:
```python
    OperationPhase.CONTEXT_EXPANSION: {
        OperationPhase.GENERATE,
        OperationPhase.CANCELLED,
    },
```

*3d.* Add `expanded_context_files` field to `OperationContext` after `schema_version` (after line 412):
```python
    expanded_context_files: Tuple[str, ...] = ()
```

*3e.* In `create()`, add `"expanded_context_files": ()` to `fields_for_hash` dict (inside the dict literal around lines 470–496):
```python
            "expanded_context_files": (),
```

*3f.* Add `with_expanded_files()` method to `OperationContext` after `with_pipeline_deadline()` (after line 620):
```python
    def with_expanded_files(self, files: Tuple[str, ...]) -> "OperationContext":
        """Return a new context with expanded_context_files set (no phase change).

        Called by ContextExpander after all expansion rounds complete.
        Uses the same hash-chain mechanics as with_pipeline_deadline().
        """
        intermediate = dataclasses.replace(
            self,
            expanded_context_files=files,
            previous_hash=self.context_hash,
            context_hash="",  # placeholder — recomputed below
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py -v
```
Expected: All existing tests pass, all 8 new `TestContextExpansionPhase` tests pass.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/op_context.py \
        tests/test_ouroboros_governance/test_op_context.py
git commit -m "feat(governance): add CONTEXT_EXPANSION phase, expanded_context_files field, with_expanded_files()"
```

---

### Task 2: Add plan() method to PrimeProvider, ClaudeProvider, CandidateGenerator

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py`
- Modify: `backend/core/ouroboros/governance/candidate_generator.py`
- Test: `tests/test_ouroboros_governance/test_providers.py`
- Test: `tests/test_ouroboros_governance/test_candidate_generator.py`

**Step 1: Write failing tests**

Add to end of `tests/test_ouroboros_governance/test_providers.py`:

```python
class TestPrimeProviderPlan:
    async def test_plan_calls_client_and_returns_string(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.providers import PrimeProvider

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        mock_client.generate = AsyncMock(return_value=mock_response)

        provider = PrimeProvider(prime_client=mock_client)
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await provider.plan("describe the task", deadline)

        assert isinstance(result, str)
        mock_client.generate.assert_called_once()
        # Must use low token budget (max_tokens <= 512) and temp=0.0
        call_kwargs = mock_client.generate.call_args.kwargs
        assert call_kwargs.get("max_tokens", 9999) <= 512
        assert call_kwargs.get("temperature", 1.0) == 0.0


class TestClaudeProviderPlan:
    async def test_plan_calls_api_and_returns_string(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        provider = ClaudeProvider(api_key="test-key")
        mock_message = MagicMock()
        mock_message.content = [MagicMock(
            text='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        )]
        mock_message.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        provider._client = mock_client

        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await provider.plan("describe the task", deadline)

        assert isinstance(result, str)
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs.get("max_tokens", 9999) <= 512
        assert call_kwargs.get("temperature", 1.0) == 0.0
```

Add to end of `tests/test_ouroboros_governance/test_candidate_generator.py`:

```python
class TestCandidateGeneratorPlan:
    async def test_plan_delegates_to_primary_when_ready(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator

        mock_primary = MagicMock()
        mock_primary.provider_name = "primary"
        mock_primary.plan = AsyncMock(return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}')
        mock_primary.generate = AsyncMock()
        mock_primary.health_probe = AsyncMock(return_value=True)

        mock_fallback = MagicMock()
        mock_fallback.provider_name = "fallback"
        mock_fallback.plan = AsyncMock(return_value='{}')
        mock_fallback.generate = AsyncMock()
        mock_fallback.health_probe = AsyncMock(return_value=True)

        gen = CandidateGenerator(primary=mock_primary, fallback=mock_fallback)
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await gen.plan("test prompt", deadline)

        assert isinstance(result, str)
        mock_primary.plan.assert_called_once()
        mock_fallback.plan.assert_not_called()

    async def test_plan_falls_back_when_primary_fails(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator

        mock_primary = MagicMock()
        mock_primary.provider_name = "primary"
        mock_primary.plan = AsyncMock(side_effect=RuntimeError("primary_down"))
        mock_primary.generate = AsyncMock()
        mock_primary.health_probe = AsyncMock(return_value=False)

        mock_fallback = MagicMock()
        mock_fallback.provider_name = "fallback"
        mock_fallback.plan = AsyncMock(return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "fallback"}')
        mock_fallback.generate = AsyncMock()
        mock_fallback.health_probe = AsyncMock(return_value=True)

        gen = CandidateGenerator(primary=mock_primary, fallback=mock_fallback)
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await gen.plan("test prompt", deadline)

        assert isinstance(result, str)
        mock_fallback.plan.assert_called_once()
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py::TestPrimeProviderPlan \
                  tests/test_ouroboros_governance/test_providers.py::TestClaudeProviderPlan \
                  tests/test_ouroboros_governance/test_candidate_generator.py::TestCandidateGeneratorPlan -v
```
Expected: FAIL — `AttributeError: plan`.

**Step 3: Implement**

*In `providers.py`*, add `plan()` to `PrimeProvider` class (after `health_probe()`, around line 733):

```python
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
```

Add `plan()` to `ClaudeProvider` class (after `health_probe()`, around line 912):

```python
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
```

*In `candidate_generator.py`*, update the `CandidateProvider` Protocol (after `health_probe()`, around line 118):

```python
    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a lightweight planning prompt; return the raw string response.

        Used by ContextExpander. Planning failures are soft — callers tolerate
        exceptions and skip expansion rounds gracefully.
        """
        ...  # pragma: no cover
```

Add `plan()` to `CandidateGenerator` (after `run_health_probe()`, around line 378):

```python
    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a planning prompt to the active provider, with soft fallback.

        Does NOT update the failback state machine on failure — planning errors
        are non-fatal and the orchestrator continues to GENERATE regardless.

        Raises RuntimeError("all_providers_exhausted") only if QUEUE_ONLY.
        """
        state = self.fsm.state

        if state is FailbackState.QUEUE_ONLY:
            raise RuntimeError("all_providers_exhausted")

        if state is FailbackState.PRIMARY_READY:
            try:
                remaining = self._remaining_seconds(deadline)
                async with self._primary_sem:
                    return await asyncio.wait_for(
                        self._primary.plan(prompt, deadline),
                        timeout=remaining,
                    )
            except (Exception, asyncio.CancelledError) as exc:
                logger.warning(
                    "[CandidateGenerator] Primary plan() failed (%s), trying fallback",
                    exc,
                )

        # FALLBACK_ACTIVE, PRIMARY_DEGRADED, or primary plan() just failed
        remaining = self._remaining_seconds(deadline)
        async with self._fallback_sem:
            return await asyncio.wait_for(
                self._fallback.plan(prompt, deadline),
                timeout=remaining,
            )
```

**Step 4: Run tests to verify pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py \
                  tests/test_ouroboros_governance/test_candidate_generator.py -v
```
Expected: All new tests pass, no regressions.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py \
        backend/core/ouroboros/governance/candidate_generator.py \
        tests/test_ouroboros_governance/test_providers.py \
        tests/test_ouroboros_governance/test_candidate_generator.py
git commit -m "feat(governance): add plan() method to PrimeProvider, ClaudeProvider, CandidateGenerator"
```

---

### Task 3: Create ContextExpander module

**Files:**
- Create: `backend/core/ouroboros/governance/context_expander.py`
- Create: `tests/test_ouroboros_governance/test_context_expander.py`

**Step 1: Write the failing tests**

Create `tests/test_ouroboros_governance/test_context_expander.py`:

```python
"""Tests for ContextExpander — bounded pre-generation context expansion."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase


def _make_ctx(target_files: tuple = ("foo.py",), description: str = "test op") -> OperationContext:
    ctx = OperationContext.create(target_files=target_files, description=description)
    ctx = ctx.advance(OperationPhase.ROUTE)
    ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
    return ctx


def _make_generator(response: str) -> MagicMock:
    gen = MagicMock()
    gen.plan = AsyncMock(return_value=response)
    return gen


class TestContextExpanderConstants:
    def test_max_rounds_is_2(self):
        from backend.core.ouroboros.governance.context_expander import MAX_ROUNDS
        assert MAX_ROUNDS == 2

    def test_max_files_per_round_is_5(self):
        from backend.core.ouroboros.governance.context_expander import MAX_FILES_PER_ROUND
        assert MAX_FILES_PER_ROUND == 5


class TestContextExpanderExpand:
    async def test_expand_returns_context_still_in_expansion_phase(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": [],
            "reasoning": "no extra files needed",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.phase is OperationPhase.CONTEXT_EXPANSION

    async def test_expand_empty_files_returns_empty_tuple(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": [],
            "reasoning": "sufficient context",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_adds_existing_files(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        (tmp_path / "helpers.py").write_text("# helper\n")

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": ["helpers.py"],
            "reasoning": "need helper context",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert "helpers.py" in result.expanded_context_files

    async def test_expand_skips_nonexistent_files(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": ["ghost.py", "phantom.py"],
            "reasoning": "want these",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_truncates_to_max_files_per_round(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander, MAX_FILES_PER_ROUND

        for i in range(8):
            (tmp_path / f"file{i}.py").write_text(f"# file {i}\n")

        gen = _make_generator(json.dumps({
            "schema_version": "expansion.1",
            "additional_files_needed": [f"file{i}.py" for i in range(8)],
            "reasoning": "want all",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert len(result.expanded_context_files) <= MAX_FILES_PER_ROUND

    async def test_expand_stops_after_max_rounds(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander, MAX_ROUNDS

        for i in range(10):
            (tmp_path / f"round{i}.py").write_text(f"# round {i}\n")

        call_count = 0

        async def counting_plan(prompt: str, deadline: datetime) -> str:
            nonlocal call_count
            call_count += 1
            return json.dumps({
                "schema_version": "expansion.1",
                "additional_files_needed": [f"round{call_count}.py"],
                "reasoning": "always want more",
            })

        gen = MagicMock()
        gen.plan = counting_plan

        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        await expander.expand(ctx, deadline)
        assert call_count <= MAX_ROUNDS

    async def test_expand_invalid_json_does_not_raise(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator("this is not json at all")
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_wrong_schema_version_returns_empty(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = _make_generator(json.dumps({
            "schema_version": "wrong.version",
            "additional_files_needed": ["something.py"],
            "reasoning": "test",
        }))
        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_generator_exception_does_not_raise(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        gen = MagicMock()
        gen.plan = AsyncMock(side_effect=RuntimeError("provider_down"))

        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files == ()

    async def test_expand_deduplicates_files(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        (tmp_path / "shared.py").write_text("# shared\n")

        call_count = 0

        async def dup_plan(prompt: str, deadline: datetime) -> str:
            nonlocal call_count
            call_count += 1
            # Both rounds request the same file
            return json.dumps({
                "schema_version": "expansion.1",
                "additional_files_needed": ["shared.py"],
                "reasoning": "same file both rounds",
            })

        gen = MagicMock()
        gen.plan = dup_plan

        expander = ContextExpander(generator=gen, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)

        result = await expander.expand(ctx, deadline)
        assert result.expanded_context_files.count("shared.py") == 1
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_context_expander.py -v
```
Expected: FAIL — `ModuleNotFoundError: context_expander`.

**Step 3: Implement**

Create `backend/core/ouroboros/governance/context_expander.py`:

```python
"""
Context Expander — Pre-Generation Context Expansion Loop
=========================================================

Executes up to MAX_ROUNDS bounded expansion rounds before GENERATE.
Each round sends a lightweight planning prompt (description + filenames only,
NO file contents) and reads back additional_files_needed (capped at
MAX_FILES_PER_ROUND per Engineering Mandate).

Governor limits are HARDCODED — they cannot be changed at runtime.
No unconstrained loops. Bounded execution time guaranteed.

Schema version: expansion.1
  {"schema_version": "expansion.1", "additional_files_needed": [...], "reasoning": "..."}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List

from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

logger = logging.getLogger("Ouroboros.ContextExpander")

# ── Governor limits (Engineering Mandate — hardcoded, not configurable) ──
MAX_ROUNDS: int = 2
MAX_FILES_PER_ROUND: int = 5

_EXPANSION_SCHEMA_VERSION = "expansion.1"


class ContextExpander:
    """Drives bounded CONTEXT_EXPANSION rounds, enriching ctx.expanded_context_files.

    Parameters
    ----------
    generator:
        CandidateGenerator (or any object with plan(prompt, deadline) -> str).
    repo_root:
        Root path for resolving and safety-checking additional files.
    """

    def __init__(self, generator: Any, repo_root: Path) -> None:
        self._generator = generator
        self._repo_root = repo_root

    async def expand(
        self,
        ctx: OperationContext,
        deadline: datetime,
    ) -> OperationContext:
        """Run up to MAX_ROUNDS expansion rounds, enriching ctx.expanded_context_files.

        Each round:
          1. Builds lightweight prompt (description + filenames only — no file contents)
          2. Calls generator.plan(prompt, deadline) → raw string
          3. Parses expansion.1 JSON response
          4. Resolves file paths against repo_root (missing files silently skipped)
          5. Accumulates confirmed paths

        Stops early if:
          - additional_files_needed is empty
          - generator raises
          - response is invalid JSON or wrong schema_version
          - no confirmed files after resolution

        Returns ctx unchanged if no files were accumulated.
        Returns ctx.with_expanded_files(tuple) otherwise.
        Never raises — all errors produce the unmodified ctx.
        """
        accumulated: List[str] = []

        for round_num in range(MAX_ROUNDS):
            prompt = self._build_expansion_prompt(ctx, accumulated)

            try:
                raw = await self._generator.plan(prompt, deadline)
            except Exception as exc:
                logger.warning(
                    "[ContextExpander] op=%s round=%d plan() failed: %s; stopping expansion",
                    ctx.op_id, round_num + 1, exc,
                )
                break

            new_paths = self._parse_expansion_response(raw)
            if not new_paths:
                logger.debug(
                    "[ContextExpander] op=%s round=%d: no additional files requested",
                    ctx.op_id, round_num + 1,
                )
                break

            confirmed = self._resolve_files(new_paths)
            if not confirmed:
                logger.debug(
                    "[ContextExpander] op=%s round=%d: none of %d requested files found on disk",
                    ctx.op_id, round_num + 1, len(new_paths),
                )
                break

            accumulated.extend(confirmed)
            logger.info(
                "[ContextExpander] op=%s round=%d: added %d files (%d total accumulated)",
                ctx.op_id, round_num + 1, len(confirmed), len(accumulated),
            )

        if not accumulated:
            return ctx

        # Deduplicate while preserving order
        seen: set = set()
        deduped: List[str] = []
        for p in accumulated:
            if p not in seen:
                seen.add(p)
                deduped.append(p)

        return ctx.with_expanded_files(tuple(deduped))

    def _build_expansion_prompt(
        self,
        ctx: OperationContext,
        already_fetched: List[str],
    ) -> str:
        """Build a lightweight prompt — filenames only, no file contents."""
        target_list = "\n".join(f"  - {f}" for f in ctx.target_files)
        fetched_list = (
            "\n".join(f"  - {f}" for f in already_fetched)
            if already_fetched
            else "  (none yet)"
        )
        return (
            f"Task: {ctx.description}\n\n"
            f"Target files to be modified:\n{target_list}\n\n"
            f"Context files already fetched:\n{fetched_list}\n\n"
            f"Which additional files (if any) would help understand the context for this task?\n"
            f"List only files that exist in the codebase. Do NOT request the target files themselves.\n\n"
            f"Return ONLY this JSON:\n"
            f'{{"schema_version": "expansion.1", '
            f'"additional_files_needed": ["path/relative/to/repo.py", ...], '
            f'"reasoning": "<one sentence max 200 chars>"}}'
        )

    def _parse_expansion_response(self, raw: str) -> List[str]:
        """Parse expansion.1 JSON, returning up to MAX_FILES_PER_ROUND paths.

        Returns empty list on any error — expansion is best-effort.
        """
        try:
            stripped = raw.strip()
            # Strip markdown fences if present
            if stripped.startswith("```"):
                lines = stripped.split("\n")
                stripped = "\n".join(
                    line for line in lines if not line.startswith("```")
                ).strip()
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            logger.debug("[ContextExpander] Response is not valid JSON; skipping round")
            return []

        if not isinstance(data, dict):
            return []

        if data.get("schema_version") != _EXPANSION_SCHEMA_VERSION:
            logger.debug(
                "[ContextExpander] Wrong schema_version: %r (expected %r)",
                data.get("schema_version"),
                _EXPANSION_SCHEMA_VERSION,
            )
            return []

        files = data.get("additional_files_needed", [])
        if not isinstance(files, list):
            return []

        valid = [f for f in files if isinstance(f, str) and f.strip()]

        if len(valid) > MAX_FILES_PER_ROUND:
            logger.warning(
                "[ContextExpander] Response requested %d files; truncating to %d (governor limit)",
                len(valid), MAX_FILES_PER_ROUND,
            )
            valid = valid[:MAX_FILES_PER_ROUND]

        return valid

    def _resolve_files(self, paths: List[str]) -> List[str]:
        """Return paths that exist on disk within repo_root.

        Silently skips missing files, symlinks, and paths outside repo_root.
        """
        from backend.core.ouroboros.governance.providers import _safe_context_path
        from backend.core.ouroboros.governance.test_runner import BlockedPathError

        confirmed: List[str] = []
        for p in paths:
            abs_candidate = (self._repo_root / p).resolve()
            try:
                _safe_context_path(self._repo_root, abs_candidate)
            except BlockedPathError:
                logger.debug("[ContextExpander] Skipping blocked path: %s", p)
                continue
            if not abs_candidate.exists():
                logger.debug("[ContextExpander] Skipping missing file: %s", p)
                continue
            confirmed.append(p)

        return confirmed
```

**Step 4: Run tests to verify pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_context_expander.py -v
```
Expected: All 11 tests pass.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/context_expander.py \
        tests/test_ouroboros_governance/test_context_expander.py
git commit -m "feat(governance): add ContextExpander with MAX_ROUNDS=2, MAX_FILES_PER_ROUND=5 governor"
```

---

### Task 4: Update _build_codegen_prompt() for expanded context files

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py`
- Test: `tests/test_ouroboros_governance/test_providers.py`

**Step 1: Write the failing tests**

Add to `tests/test_ouroboros_governance/test_providers.py`:

```python
class TestBuildCodegenPromptExpandedContext:
    def test_expanded_files_appear_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

        (tmp_path / "target.py").write_text("def foo(): pass\n")
        (tmp_path / "helpers.py").write_text("def bar(): pass\n")

        ctx = OperationContext.create(
            target_files=("target.py",), description="update foo"
        )
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.with_expanded_files(("helpers.py",))
        ctx = ctx.advance(OperationPhase.GENERATE)

        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)

        assert "helpers.py" in prompt
        assert "CONTEXT ONLY" in prompt
        assert "DO NOT MODIFY" in prompt

    def test_no_expanded_files_omits_section(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        (tmp_path / "target.py").write_text("def foo(): pass\n")
        ctx = OperationContext.create(
            target_files=("target.py",), description="update foo"
        )

        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "CONTEXT ONLY" not in prompt

    def test_expanded_file_content_appears_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

        (tmp_path / "target.py").write_text("def foo(): pass\n")
        (tmp_path / "helpers.py").write_text("UNIQUE_MARKER_XYZ = 42\n")

        ctx = OperationContext.create(
            target_files=("target.py",), description="update foo"
        )
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.with_expanded_files(("helpers.py",))
        ctx = ctx.advance(OperationPhase.GENERATE)

        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "UNIQUE_MARKER_XYZ" in prompt
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py::TestBuildCodegenPromptExpandedContext -v
```
Expected: FAIL — "CONTEXT ONLY" not in prompt.

**Step 3: Implement**

In `providers.py`, in `_build_codegen_prompt()`, add section 2b between the existing section 2 (surrounding context) and section 3 (output schema). Find the line `# ── 3. Output schema instruction` and insert before it:

```python
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
```

Then update section 4 (assemble final prompt) to include the block when non-empty. Replace the existing `return "\n\n".join([...])` with:

```python
    # ── 4. Assemble final prompt ─────────────────────────────────────────
    file_block = "\n\n".join(file_sections) if file_sections else "_No target files._"
    parts = [
        f"## Task\nOp-ID: {ctx.op_id}\nGoal: {ctx.description}",
        f"## Source Snapshot\n\n{file_block}",
        context_block,
    ]
    if expanded_context_block:
        parts.append(expanded_context_block)
    parts.append(schema_instruction)
    return "\n\n".join(parts)
```

**Step 4: Run tests to verify pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_providers.py -v
```
Expected: All pass including new `TestBuildCodegenPromptExpandedContext`.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/providers.py \
        tests/test_ouroboros_governance/test_providers.py
git commit -m "feat(governance): inject expanded_context_files as read-only sections in codegen prompt"
```

---

### Task 5: Wire CONTEXT_EXPANSION into GovernedOrchestrator

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py`
- Test: `tests/test_ouroboros_governance/test_orchestrator.py`

**Step 1: Write the failing tests**

First read `tests/test_ouroboros_governance/test_orchestrator.py` to find existing mock helpers (`_mock_stack`, `_mock_generator` or similar), then add:

```python
class TestContextExpansionPhaseWiring:
    def test_orchestrator_config_expansion_defaults(self, tmp_path):
        from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
        config = OrchestratorConfig(project_root=tmp_path)
        assert config.context_expansion_enabled is True
        assert config.context_expansion_timeout_s == 30.0

    def test_orchestrator_config_expansion_disabled(self, tmp_path):
        from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
        config = OrchestratorConfig(project_root=tmp_path, context_expansion_enabled=False)
        assert config.context_expansion_enabled is False

    async def test_context_expansion_called_when_enabled(self, tmp_path):
        """ContextExpander.expand() must be called when context_expansion_enabled=True."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator, OrchestratorConfig,
        )
        from backend.core.ouroboros.governance.op_context import OperationContext

        # Build minimal mocks — reuse pattern from existing tests in this file
        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        stack.risk_engine.classify.return_value = MagicMock(
            tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
        )
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=True, rolled_back=False, op_id="test-op"
        ))
        stack.learning_bridge = None
        stack.canary.record_operation = MagicMock()

        from backend.core.ouroboros.governance.op_context import GenerationResult
        mock_gen = MagicMock()
        mock_gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=({"candidate_id": "c1", "file_path": "foo.py",
                         "full_content": "x = 1\n", "rationale": "test",
                         "candidate_hash": "abc", "source_hash": "", "source_path": "foo.py"},),
            provider_name="mock",
            generation_duration_s=0.1,
        ))

        config = OrchestratorConfig(
            project_root=tmp_path,
            context_expansion_enabled=True,
            context_expansion_timeout_s=5.0,
        )
        orch = GovernedOrchestrator(
            stack=stack, generator=mock_gen, approval_provider=None, config=config
        )

        expand_called = []

        async def fake_expand(ctx, deadline):
            expand_called.append(True)
            return ctx

        with patch(
            "backend.core.ouroboros.governance.orchestrator.ContextExpander"
        ) as MockExpander:
            instance = MagicMock()
            instance.expand = AsyncMock(side_effect=fake_expand)
            MockExpander.return_value = instance

            ctx = OperationContext.create(
                target_files=("foo.py",), description="test expansion wiring"
            )
            await orch.run(ctx)

        assert expand_called, "ContextExpander.expand() was never called"

    async def test_context_expansion_skipped_when_disabled(self, tmp_path):
        """ContextExpander must NOT be instantiated when context_expansion_enabled=False."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator, OrchestratorConfig,
        )
        from backend.core.ouroboros.governance.op_context import OperationContext, GenerationResult

        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        stack.risk_engine.classify.return_value = MagicMock(
            tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
        )
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=True, rolled_back=False, op_id="test-op"
        ))
        stack.learning_bridge = None
        stack.canary.record_operation = MagicMock()

        mock_gen = MagicMock()
        mock_gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=({"candidate_id": "c1", "file_path": "foo.py",
                         "full_content": "x = 1\n", "rationale": "test",
                         "candidate_hash": "abc", "source_hash": "", "source_path": "foo.py"},),
            provider_name="mock",
            generation_duration_s=0.1,
        ))

        config = OrchestratorConfig(
            project_root=tmp_path,
            context_expansion_enabled=False,
        )
        orch = GovernedOrchestrator(
            stack=stack, generator=mock_gen, approval_provider=None, config=config
        )

        with patch(
            "backend.core.ouroboros.governance.orchestrator.ContextExpander"
        ) as MockExpander:
            ctx = OperationContext.create(
                target_files=("foo.py",), description="test no expansion"
            )
            await orch.run(ctx)

        MockExpander.assert_not_called()
```

**Step 2: Run to verify fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py::TestContextExpansionPhaseWiring -v
```
Expected: FAIL — `TypeError: OrchestratorConfig has no field context_expansion_enabled`.

**Step 3: Implement**

*3a.* In `orchestrator.py`, add two fields to `OrchestratorConfig` after `max_validate_retries` (after line 102):

```python
    context_expansion_enabled: bool = True
    context_expansion_timeout_s: float = 30.0
```

*3b.* Add import at the top of `orchestrator.py` (with the other governance imports, around line 40–63):

```python
from backend.core.ouroboros.governance.context_expander import ContextExpander
```

*3c.* In `_run_pipeline()`, find Phase 2 (ROUTE). Replace:

```python
        # ---- Phase 2: ROUTE ----
        # Thin transition: just advance to GENERATE
        ctx = ctx.advance(OperationPhase.GENERATE)
```

with:

```python
        # ---- Phase 2: ROUTE ----
        if self._config.context_expansion_enabled:
            ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)

            # ---- Phase 2b: CONTEXT_EXPANSION ----
            try:
                expansion_deadline = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=self._config.context_expansion_timeout_s
                )
                expander = ContextExpander(
                    generator=self._generator,
                    repo_root=self._config.project_root,
                )
                ctx = await asyncio.wait_for(
                    expander.expand(ctx, expansion_deadline),
                    timeout=self._config.context_expansion_timeout_s,
                )
            except Exception as exc:
                logger.warning(
                    "[Orchestrator] Context expansion failed for op=%s: %s; "
                    "continuing to GENERATE",
                    ctx.op_id, exc,
                )

            ctx = ctx.advance(OperationPhase.GENERATE)
        else:
            # Expansion disabled: skip directly to GENERATE
            ctx = ctx.advance(OperationPhase.GENERATE)
```

*3d.* Update the module-level docstring (line 11) pipeline diagram to:

```
    CLASSIFY -> ROUTE -> [CONTEXT_EXPANSION] -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE
```

**Step 4: Run full governance test suite**

```bash
python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short 2>&1 | tail -40
```
Expected: All tests pass. No regressions.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/test_ouroboros_governance/test_orchestrator.py
git commit -m "feat(governance): wire CONTEXT_EXPANSION phase into GovernedOrchestrator pipeline"
```

---

## Final Verification

After all 5 tasks, run the complete governance test suite:

```bash
python3 -m pytest tests/test_ouroboros_governance/ -v 2>&1 | tail -20
```

Expected: All tests pass. Then run:

```bash
python3 -m pytest tests/ -x --tb=short 2>&1 | tail -30
```

Confirm no regressions across the full test suite.
