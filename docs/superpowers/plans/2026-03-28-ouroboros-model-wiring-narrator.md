# Ouroboros Model Wiring + DaemonNarrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the Ouroboros cognitive layer by wiring Doubleword 397B model calls into the Synthesis Engine and Architecture Agent, and add DaemonNarrator for voice transparency of autonomous activity.

**Architecture:** `prompt_only()` method on DoublewordProvider bypasses governance OperationContext. Synthesis Engine and Architect call it with structured output prompts + deterministic context shedding. DaemonNarrator listens to event callbacks from REM/Synthesis/Saga and speaks significant events via rate-limited `safe_say()`.

**Tech Stack:** Python 3.12, asyncio, aiohttp (existing Doubleword HTTP), JSON schema validation, existing safe_say() voice path

**Spec:** `docs/superpowers/specs/2026-03-28-ouroboros-model-wiring-narrator-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `backend/core/ouroboros/daemon_narrator.py` | DaemonNarrator — rate-limited voice for autonomous events |
| `backend/core/ouroboros/roadmap/synthesis_prompt.py` | Prompt builder + context shedding for synthesis |
| `backend/core/ouroboros/architect/design_prompt.py` | Prompt builder + context shedding for architect |
| `tests/core/ouroboros/test_daemon_narrator.py` | Narrator tests |
| `tests/core/ouroboros/test_prompt_only.py` | prompt_only() tests |
| `tests/core/ouroboros/roadmap/test_synthesis_prompt.py` | Synthesis prompt tests |
| `tests/core/ouroboros/architect/test_design_prompt.py` | Architect prompt tests |
| `tests/core/ouroboros/test_model_wiring_integration.py` | E2E integration test |

### Modified Files
| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/doubleword_provider.py` | Add `prompt_only()` method |
| `backend/core/ouroboros/roadmap/synthesis_engine.py` | Wire `_run_doubleword()` replacing v2 placeholder |
| `backend/core/ouroboros/architect/reasoning_agent.py` | Wire `_generate_plan()` replacing v1 return None |
| `backend/core/ouroboros/daemon.py` | Create + wire DaemonNarrator |
| `backend/core/ouroboros/daemon_config.py` | Add narrator + prompt config fields |
| `backend/core/ouroboros/rem_sleep.py` | Emit epoch events + call narrator |
| `backend/core/ouroboros/architect/saga_orchestrator.py` | Emit saga events + accept spinal_cord param |

---

## Task 1: prompt_only() on DoublewordProvider

**Files:**
- Modify: `backend/core/ouroboros/governance/doubleword_provider.py`
- Create: `tests/core/ouroboros/test_prompt_only.py`

- [ ] **Step 1: Write prompt_only tests**

```python
# tests/core/ouroboros/test_prompt_only.py
"""Tests for DoublewordProvider.prompt_only() — inference without governance context."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.ouroboros.governance.doubleword_provider import (
    DoublewordProvider,
)


@pytest.fixture
def provider():
    return DoublewordProvider(api_key="test-key", base_url="https://api.test.com/v1")


@pytest.mark.asyncio
async def test_prompt_only_returns_string(provider):
    """Mock the full HTTP cycle: upload -> create batch -> poll -> retrieve."""
    mock_session = AsyncMock()

    # Mock file upload response
    upload_resp = AsyncMock()
    upload_resp.status = 200
    upload_resp.json = AsyncMock(return_value={"id": "file-123"})
    upload_resp.__aenter__ = AsyncMock(return_value=upload_resp)
    upload_resp.__aexit__ = AsyncMock(return_value=False)

    # Mock batch create response
    create_resp = AsyncMock()
    create_resp.status = 200
    create_resp.json = AsyncMock(return_value={"id": "batch-456"})
    create_resp.__aenter__ = AsyncMock(return_value=create_resp)
    create_resp.__aexit__ = AsyncMock(return_value=False)

    # Mock batch poll response (completed)
    poll_resp = AsyncMock()
    poll_resp.status = 200
    poll_resp.json = AsyncMock(return_value={
        "status": "completed",
        "output_file_id": "out-789",
    })
    poll_resp.__aenter__ = AsyncMock(return_value=poll_resp)
    poll_resp.__aexit__ = AsyncMock(return_value=False)

    # Mock file retrieve response
    result_line = json.dumps({
        "custom_id": "prompt_only",
        "response": {
            "body": {
                "choices": [{"message": {"content": '{"gaps": []}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }
        }
    })
    retrieve_resp = AsyncMock()
    retrieve_resp.status = 200
    retrieve_resp.text = AsyncMock(return_value=result_line)
    retrieve_resp.__aenter__ = AsyncMock(return_value=retrieve_resp)
    retrieve_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session.post = MagicMock(side_effect=[upload_resp, create_resp])
    mock_session.get = MagicMock(side_effect=[poll_resp, retrieve_resp])

    provider._session = mock_session
    result = await provider.prompt_only(
        prompt="Analyze this", caller_id="test",
    )
    assert isinstance(result, str)
    assert "gaps" in result


@pytest.mark.asyncio
async def test_prompt_only_tracks_cost(provider):
    """Verify cost is tracked under caller_id bucket."""
    # This test verifies the stats tracking mechanism exists
    assert hasattr(provider, "_stats")
    assert hasattr(provider._stats, "total_batches")


def test_prompt_only_method_exists(provider):
    """Verify prompt_only is callable."""
    assert hasattr(provider, "prompt_only")
    assert callable(provider.prompt_only)


@pytest.mark.asyncio
async def test_prompt_only_raises_on_missing_api_key():
    """Provider with empty API key should raise early."""
    provider = DoublewordProvider(api_key="", base_url="https://api.test.com/v1")
    with pytest.raises(Exception):
        await provider.prompt_only(prompt="test", caller_id="test")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_prompt_only.py -v`
Expected: FAIL — `prompt_only` not found or not implemented

- [ ] **Step 3: Implement prompt_only()**

Read `doubleword_provider.py` fully. Add the `prompt_only()` method after the existing `generate()` method. It should:

1. Validate API key is set
2. Ensure aiohttp session exists (reuse `_ensure_session()` or create one)
3. Construct JSONL line (same format as `submit_batch` but with `custom_id="prompt_only"`)
4. Upload file via POST `/files`
5. Create batch via POST `/batches`
6. Poll via GET `/batches/{batch_id}` until completed (reuse existing poll interval)
7. Retrieve via GET `/files/{output_file_id}/content`
8. Parse JSONL response, extract `choices[0].message.content`
9. Track cost in `_stats` (tag with `caller_id` if extending stats)
10. Return content string

```python
async def prompt_only(
    self,
    prompt: str,
    model: Optional[str] = None,
    caller_id: str = "ouroboros_cognition",
    response_format: Optional[Dict] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Execute inference bypassing governance OperationContext.

    Reuses: auth, rate limiting, retry/backoff, polling, cost tracking.
    Bypasses: OperationContext, governance ledger, TelemetryBus.
    """
    if not self._api_key:
        raise ValueError("DOUBLEWORD_API_KEY not set")

    used_model = model or self._model
    used_max_tokens = max_tokens or self._max_tokens
    session = await self._ensure_session()

    # Build messages
    messages = [
        {"role": "system", "content": "You are a code analysis assistant for the Trinity AI ecosystem. Return valid JSON matching the requested schema."},
        {"role": "user", "content": prompt},
    ]
    body: Dict[str, Any] = {
        "model": used_model,
        "messages": messages,
        "max_tokens": used_max_tokens,
        "temperature": _DW_TEMPERATURE,
    }
    if response_format:
        body["response_format"] = response_format

    # Construct JSONL
    jsonl_line = json.dumps({
        "custom_id": f"prompt_only_{caller_id}",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": body,
    })

    # Upload -> Create batch -> Poll -> Retrieve (same as submit_batch + poll_and_retrieve)
    # ... (reuse existing HTTP patterns from submit_batch)
```

The implementer should read the existing `submit_batch()` and `poll_and_retrieve()` methods and extract their HTTP logic into the `prompt_only()` method directly, without creating OperationContext or PendingBatch objects.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_prompt_only.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ouroboros): add prompt_only() to DoublewordProvider for governance-free inference"
```

---

## Task 2: Synthesis Prompt Builder + Context Shedding

**Files:**
- Create: `backend/core/ouroboros/roadmap/synthesis_prompt.py`
- Create: `tests/core/ouroboros/roadmap/test_synthesis_prompt.py`

- [ ] **Step 1: Write prompt builder tests**

```python
# tests/core/ouroboros/roadmap/test_synthesis_prompt.py
"""Tests for synthesis prompt construction and context shedding."""
import time
import pytest
from backend.core.ouroboros.roadmap.synthesis_prompt import (
    build_synthesis_prompt,
    shed_context,
    ContextBudgetExceededError,
    SYNTHESIS_JSON_SCHEMA,
)
from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment, RoadmapSnapshot
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


def _frag(source_id="spec:test", summary="We need a WhatsApp agent", tier=0, ftype="spec"):
    return SnapshotFragment(
        source_id=source_id, uri="test.md", tier=tier,
        content_hash="abc", fetched_at=time.time(), mtime=time.time(),
        title="Test", summary=summary, fragment_type=ftype,
    )


def _snapshot(*frags):
    return RoadmapSnapshot.create(fragments=tuple(frags), previous_version=0)


def test_build_prompt_includes_fragments():
    snapshot = _snapshot(_frag(summary="Build a WhatsApp agent"))
    prompt = build_synthesis_prompt(snapshot, tier0_hints=[], oracle_summary="")
    assert "WhatsApp" in prompt
    assert "ROADMAP EVIDENCE" in prompt


def test_build_prompt_includes_tier0_hints():
    hint = FeatureHypothesis.new(
        description="Missing WhatsApp agent",
        evidence_fragments=("spec:test",),
        gap_type="missing_capability",
        confidence=0.85,
        confidence_rule_id="spec_symbol_miss",
        urgency="normal",
        suggested_scope="backend/agents/",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc",
        synthesis_input_fingerprint="fp",
    )
    snapshot = _snapshot(_frag())
    prompt = build_synthesis_prompt(snapshot, tier0_hints=[hint], oracle_summary="")
    assert "EXISTING GAPS" in prompt
    assert "WhatsApp" in prompt


def test_build_prompt_includes_json_schema():
    snapshot = _snapshot(_frag())
    prompt = build_synthesis_prompt(snapshot, tier0_hints=[], oracle_summary="")
    assert "description" in prompt
    assert "gap_type" in prompt


def test_shed_context_under_budget():
    text = "Short text"
    result = shed_context(text, max_tokens=1000)
    assert result == text


def test_shed_context_over_budget_raises():
    text = "word " * 10000  # way over any reasonable budget
    with pytest.raises(ContextBudgetExceededError):
        shed_context(text, max_tokens=100)


def test_shed_context_truncates_fragments():
    # Long text that needs shedding
    text = "ROADMAP EVIDENCE:\n" + ("x" * 5000) + "\nEND"
    result = shed_context(text, max_tokens=2000)
    assert len(result) < len(text)


def test_json_schema_has_required_fields():
    assert "description" in str(SYNTHESIS_JSON_SCHEMA)
    assert "gap_type" in str(SYNTHESIS_JSON_SCHEMA)
    assert "confidence" in str(SYNTHESIS_JSON_SCHEMA)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_synthesis_prompt.py -v`
Expected: FAIL

- [ ] **Step 3: Implement synthesis_prompt.py**

```python
# backend/core/ouroboros/roadmap/synthesis_prompt.py
"""Prompt builder and context shedding for Feature Synthesis Engine.

Deterministic: prompt construction and shedding are code, not model inference.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


class ContextBudgetExceededError(Exception):
    """Raised when context shedding cannot meet the token budget."""
    pass


# Approximate: 1 token ≈ 4 chars (conservative estimate)
_CHARS_PER_TOKEN = 4

SYNTHESIS_JSON_SCHEMA: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "capability_gaps",
        "schema": {
            "type": "object",
            "properties": {
                "gaps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "evidence_fragments": {"type": "array", "items": {"type": "string"}},
                            "gap_type": {"type": "string", "enum": ["missing_capability", "incomplete_wiring", "stale_implementation", "manifesto_violation"]},
                            "confidence": {"type": "number"},
                            "urgency": {"type": "string", "enum": ["critical", "high", "normal", "low"]},
                            "suggested_scope": {"type": "string"},
                            "suggested_repos": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["description", "evidence_fragments", "gap_type", "confidence", "urgency", "suggested_scope", "suggested_repos"],
                    },
                },
            },
            "required": ["gaps"],
        },
    },
}


def build_synthesis_prompt(
    snapshot: RoadmapSnapshot,
    tier0_hints: List[FeatureHypothesis],
    oracle_summary: str,
) -> str:
    """Build the synthesis prompt from snapshot fragments + tier0 hints."""
    # P0 fragments only
    p0_frags = [f for f in snapshot.fragments if f.tier == 0]

    sections = []
    sections.append("You are analyzing a software roadmap for capability gaps.\n")

    # Roadmap evidence
    sections.append("ROADMAP EVIDENCE (specs, plans, backlog):")
    for frag in p0_frags:
        sections.append(f"- [{frag.fragment_type}] {frag.source_id}: {frag.summary[:500]}")

    # Tier 0 hints
    if tier0_hints:
        sections.append("\nEXISTING GAPS ALREADY DETECTED (deterministic):")
        for hint in tier0_hints:
            sections.append(f"- [{hint.gap_type}] {hint.description} (confidence={hint.confidence:.2f})")

    # Oracle summary
    if oracle_summary:
        sections.append(f"\nCODEBASE STRUCTURE:\n{oracle_summary}")

    # Schema instructions
    sections.append("\nIdentify capability gaps between stated intent and current implementation.")
    sections.append("Return a JSON object with a 'gaps' array. Each gap object must have:")
    sections.append("- description: what is missing")
    sections.append("- evidence_fragments: array of source_ids from the roadmap evidence")
    sections.append("- gap_type: one of missing_capability, incomplete_wiring, stale_implementation, manifesto_violation")
    sections.append("- confidence: float 0-1")
    sections.append("- urgency: one of critical, high, normal, low")
    sections.append("- suggested_scope: directory or file path")
    sections.append("- suggested_repos: array of jarvis, jarvis-prime, reactor")

    return "\n".join(sections)


def shed_context(text: str, max_tokens: int) -> str:
    """Deterministic context shedding. Truncates to fit within token budget.

    Rules (in order):
    1. If under budget, return as-is
    2. Truncate to max_tokens * _CHARS_PER_TOKEN chars
    3. If still over (shouldn't happen), raise ContextBudgetExceededError
    """
    max_chars = max_tokens * _CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text

    # Truncate at char boundary
    truncated = text[:max_chars]

    # Find last newline to avoid cutting mid-line
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars * 0.5:
        truncated = truncated[:last_newline]

    if len(truncated) > max_chars * 1.5:
        raise ContextBudgetExceededError(
            f"Cannot shed to {max_tokens} tokens ({max_chars} chars). "
            f"Text is {len(text)} chars after shedding."
        )

    return truncated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_synthesis_prompt.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ouroboros/roadmap): add synthesis prompt builder with context shedding"
```

---

## Task 3: Wire Doubleword into Synthesis Engine

**Files:**
- Modify: `backend/core/ouroboros/roadmap/synthesis_engine.py`

- [ ] **Step 1: Write wiring test**

```python
# tests/core/ouroboros/roadmap/test_synthesis_doubleword.py
"""Tests for Synthesis Engine Doubleword 397B integration."""
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.roadmap.synthesis_engine import FeatureSynthesisEngine, SynthesisConfig
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment, RoadmapSnapshot


def _snapshot():
    frag = SnapshotFragment(
        source_id="spec:test", uri="test.md", tier=0,
        content_hash="abc", fetched_at=time.time(), mtime=time.time(),
        title="Test", summary="Build a WhatsApp agent", fragment_type="spec",
    )
    return RoadmapSnapshot.create(fragments=(frag,), previous_version=0)


def _mock_doubleword_returning(gaps_json):
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(return_value=json.dumps(gaps_json))
    return dw


def _mock_cache(tmp_path):
    return HypothesisCache(cache_dir=tmp_path)


@pytest.mark.asyncio
async def test_synthesis_calls_doubleword(tmp_path):
    dw = _mock_doubleword_returning({
        "gaps": [{
            "description": "Missing WhatsApp agent",
            "evidence_fragments": ["spec:test"],
            "gap_type": "missing_capability",
            "confidence": 0.9,
            "urgency": "normal",
            "suggested_scope": "backend/agents/",
            "suggested_repos": ["jarvis"],
        }]
    })
    engine = FeatureSynthesisEngine(
        oracle=MagicMock(find_nodes_by_name=MagicMock(return_value=[])),
        doubleword=dw,
        cache=_mock_cache(tmp_path),
        config=SynthesisConfig(min_interval_s=0),
    )
    snapshot = _snapshot()
    result = await engine.synthesize(snapshot, force=True)
    assert dw.prompt_only.called
    # Should have tier0 + model hypotheses
    assert any(h.provenance.startswith("model:") for h in result)


@pytest.mark.asyncio
async def test_synthesis_falls_back_on_doubleword_failure(tmp_path):
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(side_effect=Exception("API down"))
    engine = FeatureSynthesisEngine(
        oracle=MagicMock(find_nodes_by_name=MagicMock(return_value=[])),
        doubleword=dw,
        cache=_mock_cache(tmp_path),
        config=SynthesisConfig(min_interval_s=0),
    )
    snapshot = _snapshot()
    result = await engine.synthesize(snapshot, force=True)
    # Should still return tier0 hints (graceful fallback)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_synthesis_without_doubleword(tmp_path):
    engine = FeatureSynthesisEngine(
        oracle=MagicMock(find_nodes_by_name=MagicMock(return_value=[])),
        doubleword=None,  # no provider
        cache=_mock_cache(tmp_path),
        config=SynthesisConfig(min_interval_s=0),
    )
    result = await engine.synthesize(_snapshot(), force=True)
    assert isinstance(result, list)  # tier0 only
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_synthesis_doubleword.py -v`
Expected: FAIL — Doubleword not called (v2 placeholder still active)

- [ ] **Step 3: Wire _run_doubleword into synthesis_engine.py**

In `synthesis_engine.py`, replace the v2 placeholder (lines 235-237) with:

```python
# --- Doubleword 397B synthesis (v2) ---
model_hints: List[FeatureHypothesis] = []
if self._doubleword is not None and hasattr(self._doubleword, "prompt_only"):
    try:
        model_hints = await self._run_doubleword(snapshot, tier0)
    except Exception as exc:
        logger.warning("FeatureSynthesisEngine: Doubleword failed: %s", exc)
```

Add the `_run_doubleword` method:

```python
async def _run_doubleword(
    self,
    snapshot: RoadmapSnapshot,
    tier0_hints: List[FeatureHypothesis],
) -> List[FeatureHypothesis]:
    """Call Doubleword 397B for deep gap analysis."""
    from backend.core.ouroboros.roadmap.synthesis_prompt import (
        build_synthesis_prompt,
        shed_context,
        SYNTHESIS_JSON_SCHEMA,
        ContextBudgetExceededError,
    )

    oracle_summary = ""  # TODO: extract from self._oracle if available

    prompt = build_synthesis_prompt(snapshot, tier0_hints, oracle_summary)
    try:
        prompt = shed_context(prompt, max_tokens=6000)
    except ContextBudgetExceededError:
        logger.warning("FeatureSynthesisEngine: context budget exceeded, using tier0 only")
        return []

    response = await self._doubleword.prompt_only(
        prompt=prompt,
        caller_id="synthesis_engine",
        response_format=SYNTHESIS_JSON_SCHEMA,
    )

    return self._parse_doubleword_response(response, snapshot)


def _parse_doubleword_response(
    self,
    response: str,
    snapshot: RoadmapSnapshot,
) -> List[FeatureHypothesis]:
    """Parse structured JSON response into FeatureHypothesis list."""
    import json
    try:
        data = json.loads(response)
    except json.JSONDecodeError as exc:
        logger.warning("FeatureSynthesisEngine: invalid JSON from Doubleword: %s", exc)
        return []

    gaps = data.get("gaps", [])
    hypotheses = []
    for gap in gaps:
        try:
            h = FeatureHypothesis.new(
                description=gap["description"],
                evidence_fragments=tuple(gap.get("evidence_fragments", ())),
                gap_type=gap["gap_type"],
                confidence=float(gap.get("confidence", 0.7)),
                confidence_rule_id="model_inference",
                urgency=gap.get("urgency", "normal"),
                suggested_scope=gap.get("suggested_scope", "backend/"),
                suggested_repos=tuple(gap.get("suggested_repos", ("jarvis",))),
                provenance="model:doubleword-397b",
                synthesized_for_snapshot_hash=snapshot.content_hash,
                synthesis_input_fingerprint="doubleword",
            )
            hypotheses.append(h)
        except Exception as exc:
            logger.warning("FeatureSynthesisEngine: skipping invalid gap: %s", exc)
    return hypotheses
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_synthesis_doubleword.py -v`
Expected: All PASS

- [ ] **Step 5: Run existing synthesis tests for regression**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_synthesis_engine.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(ouroboros/roadmap): wire Doubleword 397B into Feature Synthesis Engine"
```

---

## Task 4: Architect Prompt Builder + Context Shedding

**Files:**
- Create: `backend/core/ouroboros/architect/design_prompt.py`
- Create: `tests/core/ouroboros/architect/test_design_prompt.py`

- [ ] **Step 1: Write prompt builder tests**

```python
# tests/core/ouroboros/architect/test_design_prompt.py
"""Tests for architect prompt construction and context shedding."""
import pytest
from unittest.mock import MagicMock
from backend.core.ouroboros.architect.design_prompt import (
    build_design_prompt,
    ARCHITECTURAL_PLAN_JSON_SCHEMA,
)


def _mock_hypothesis():
    h = MagicMock()
    h.description = "Missing WhatsApp agent"
    h.evidence_fragments = ("spec:manifesto",)
    h.gap_type = "missing_capability"
    h.suggested_scope = "backend/agents/"
    return h


def _mock_oracle_neighborhood():
    return "imports: base_agent.py\ncallers: agent_registry.py\ntests: test_agents.py"


def test_prompt_includes_hypothesis():
    prompt = build_design_prompt(
        hypothesis=_mock_hypothesis(),
        oracle_context=_mock_oracle_neighborhood(),
        max_steps=10,
    )
    assert "WhatsApp" in prompt
    assert "missing_capability" in prompt


def test_prompt_includes_constraints():
    prompt = build_design_prompt(
        hypothesis=_mock_hypothesis(),
        oracle_context="",
        max_steps=5,
    )
    assert "5" in prompt  # max steps
    assert "non_goals" in prompt.lower() or "non-goals" in prompt.lower()


def test_prompt_includes_json_schema_fields():
    prompt = build_design_prompt(
        hypothesis=_mock_hypothesis(),
        oracle_context="",
        max_steps=10,
    )
    assert "step_index" in prompt
    assert "target_paths" in prompt
    assert "acceptance_checks" in prompt


def test_json_schema_has_required_fields():
    schema_str = str(ARCHITECTURAL_PLAN_JSON_SCHEMA)
    assert "steps" in schema_str
    assert "acceptance_checks" in schema_str
    assert "title" in schema_str
```

- [ ] **Step 2: Implement design_prompt.py**

```python
# backend/core/ouroboros/architect/design_prompt.py
"""Prompt builder for Architecture Reasoning Agent. Deterministic."""
from __future__ import annotations

from typing import Any, Dict


ARCHITECTURAL_PLAN_JSON_SCHEMA: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "architectural_plan",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "repos_affected": {"type": "array", "items": {"type": "string"}},
                "non_goals": {"type": "array", "items": {"type": "string"}},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_index": {"type": "integer"},
                            "description": {"type": "string"},
                            "intent_kind": {"type": "string", "enum": ["create_file", "modify_file", "delete_file"]},
                            "target_paths": {"type": "array", "items": {"type": "string"}},
                            "ancillary_paths": {"type": "array", "items": {"type": "string"}},
                            "tests_required": {"type": "array", "items": {"type": "string"}},
                            "interface_contracts": {"type": "array", "items": {"type": "string"}},
                            "repo": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "integer"}},
                        },
                        "required": ["step_index", "description", "intent_kind", "target_paths", "repo"],
                    },
                },
                "acceptance_checks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "check_id": {"type": "string"},
                            "check_kind": {"type": "string", "enum": ["exit_code", "regex_stdout", "import_check"]},
                            "command": {"type": "string"},
                            "expected": {"type": "string"},
                        },
                        "required": ["check_id", "check_kind", "command"],
                    },
                },
            },
            "required": ["title", "description", "repos_affected", "non_goals", "steps", "acceptance_checks"],
        },
    },
}


def build_design_prompt(
    hypothesis: Any,
    oracle_context: str,
    max_steps: int = 10,
) -> str:
    """Build the architectural design prompt."""
    sections = []
    sections.append("You are designing a multi-file feature for the JARVIS Trinity ecosystem.\n")

    sections.append("CAPABILITY GAP:")
    sections.append(f"Description: {hypothesis.description}")
    sections.append(f"Evidence: {', '.join(hypothesis.evidence_fragments)}")
    sections.append(f"Gap type: {hypothesis.gap_type}")
    sections.append(f"Suggested scope: {getattr(hypothesis, 'suggested_scope', 'backend/')}")

    if oracle_context:
        sections.append(f"\nCODEBASE CONTEXT:\n{oracle_context}")

    sections.append(f"\nCONSTRAINTS:")
    sections.append(f"- Maximum {max_steps} implementation steps")
    sections.append("- Each step targets one file (create, modify, or delete)")
    sections.append("- Include test files for each new module")
    sections.append("- Include acceptance_checks (shell commands that verify correctness)")
    sections.append("- Explicitly list non_goals (what is out of scope)")
    sections.append("- All paths must be repo-relative (no '..' escape)")
    sections.append("- Steps must form an acyclic dependency graph")

    sections.append("\nReturn a JSON object matching the architectural_plan schema.")

    return "\n".join(sections)
```

- [ ] **Step 3: Run tests, commit**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_design_prompt.py -v`
Commit: `feat(ouroboros/architect): add design prompt builder with JSON schema`

---

## Task 5: Wire Doubleword into Architecture Agent

**Files:**
- Modify: `backend/core/ouroboros/architect/reasoning_agent.py`

- [ ] **Step 1: Write wiring test**

```python
# tests/core/ouroboros/architect/test_architect_doubleword.py
"""Tests for Architecture Agent Doubleword 397B integration."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.architect.reasoning_agent import (
    ArchitectureReasoningAgent, AgentConfig,
)


def _mock_hypothesis():
    h = MagicMock()
    h.description = "Missing WhatsApp agent"
    h.hypothesis_id = "h1"
    h.hypothesis_fingerprint = "fp1"
    h.evidence_fragments = ("spec:manifesto",)
    h.gap_type = "missing_capability"
    h.confidence = 0.9
    h.suggested_scope = "backend/agents/"
    h.suggested_repos = ("jarvis",)
    return h


def _mock_doubleword_returning(plan_json):
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(return_value=json.dumps(plan_json))
    return dw


@pytest.mark.asyncio
async def test_design_calls_doubleword():
    plan_response = {
        "title": "WhatsApp Agent",
        "description": "Add WhatsApp integration",
        "repos_affected": ["jarvis"],
        "non_goals": ["No UI"],
        "steps": [{
            "step_index": 0,
            "description": "Create agent",
            "intent_kind": "create_file",
            "target_paths": ["backend/agents/whatsapp.py"],
            "repo": "jarvis",
        }],
        "acceptance_checks": [{
            "check_id": "import",
            "check_kind": "exit_code",
            "command": "python3 -c 'import backend.agents.whatsapp'",
        }],
    }
    dw = _mock_doubleword_returning(plan_response)
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=dw, config=AgentConfig(),
    )
    result = await agent.design(
        _mock_hypothesis(),
        snapshot=MagicMock(content_hash="snap1"),
        oracle=MagicMock(),
    )
    assert dw.prompt_only.called
    # v2: should return an ArchitecturalPlan (or None if validation fails)
    # The plan may be None if parsing/validation fails on the mock data


@pytest.mark.asyncio
async def test_design_returns_none_on_doubleword_failure():
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(side_effect=Exception("API down"))
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=dw, config=AgentConfig(),
    )
    result = await agent.design(
        _mock_hypothesis(),
        snapshot=MagicMock(content_hash="snap1"),
        oracle=MagicMock(),
    )
    assert result is None  # graceful fallback


@pytest.mark.asyncio
async def test_design_without_doubleword():
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=None, config=AgentConfig(),
    )
    result = await agent.design(
        _mock_hypothesis(),
        snapshot=MagicMock(content_hash="snap1"),
        oracle=MagicMock(),
    )
    assert result is None
```

- [ ] **Step 2: Wire _generate_plan into reasoning_agent.py**

In `reasoning_agent.py`, replace the v1 `return None` at line 166 with actual Doubleword call:

```python
# Replace the return None at line 166 with:
if self._doubleword is not None and hasattr(self._doubleword, "prompt_only"):
    try:
        return await self._generate_plan(hypothesis, snapshot, oracle)
    except Exception as exc:
        logger.warning("ArchitectureReasoningAgent: plan generation failed: %s", exc)
        return None
return None  # no doubleword available
```

Add `_generate_plan` and `_parse_plan` methods. Read `design_prompt.py` for prompt building, `plan.py` for ArchitecturalPlan.create(), `plan_validator.py` for validation.

The implementer should:
1. Build prompt via `build_design_prompt()`
2. Call `self._doubleword.prompt_only(prompt, caller_id="architecture_agent", response_format=ARCHITECTURAL_PLAN_JSON_SCHEMA)`
3. Parse JSON response into PlanStep objects (convert intent_kind strings to StepIntentKind enum)
4. Call `ArchitecturalPlan.create()` with parsed data
5. Run `PlanValidator().validate(plan)` — if fails, log and return None
6. Return the validated plan

- [ ] **Step 3: Run tests, commit**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_architect_doubleword.py -v`
Run: `python3 -m pytest tests/core/ouroboros/architect/test_reasoning_agent.py -v` (regression)
Commit: `feat(ouroboros/architect): wire Doubleword 397B into Architecture Reasoning Agent`

---

## Task 6: DaemonNarrator

**Files:**
- Create: `backend/core/ouroboros/daemon_narrator.py`
- Create: `tests/core/ouroboros/test_daemon_narrator.py`

- [ ] **Step 1: Write narrator tests**

```python
# tests/core/ouroboros/test_daemon_narrator.py
"""Tests for DaemonNarrator — rate-limited voice for autonomous events."""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.daemon_narrator import DaemonNarrator


@pytest.fixture
def mock_say():
    return AsyncMock(return_value=True)


def test_narrator_starts_enabled(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say)
    assert narrator._enabled


@pytest.mark.asyncio
async def test_epoch_start_speaks(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)
    await narrator.on_event("rem.epoch_start", {"epoch_id": 1})
    mock_say.assert_called_once()
    assert "REM" in mock_say.call_args[0][0]


@pytest.mark.asyncio
async def test_epoch_complete_speaks_summary(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)
    await narrator.on_event("rem.epoch_complete", {
        "epoch_id": 1,
        "findings_count": 5,
        "envelopes_submitted": 3,
    })
    mock_say.assert_called_once()
    msg = mock_say.call_args[0][0]
    assert "5" in msg  # findings count
    assert "3" in msg  # patches


@pytest.mark.asyncio
async def test_saga_complete_speaks(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)
    await narrator.on_event("saga.complete", {"title": "WhatsApp Agent"})
    mock_say.assert_called_once()
    assert "WhatsApp" in mock_say.call_args[0][0]


@pytest.mark.asyncio
async def test_rate_limiting(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=60)
    await narrator.on_event("rem.epoch_start", {"epoch_id": 1})
    await narrator.on_event("rem.epoch_start", {"epoch_id": 2})  # within rate limit
    assert mock_say.call_count == 1  # second dropped


@pytest.mark.asyncio
async def test_different_categories_not_rate_limited(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=60)
    await narrator.on_event("rem.epoch_start", {"epoch_id": 1})
    await narrator.on_event("saga.complete", {"title": "Test"})
    assert mock_say.call_count == 2  # different categories


@pytest.mark.asyncio
async def test_disabled_narrator_silent(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, enabled=False)
    await narrator.on_event("rem.epoch_start", {"epoch_id": 1})
    mock_say.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_event_ignored(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)
    await narrator.on_event("unknown.event", {})
    mock_say.assert_not_called()


@pytest.mark.asyncio
async def test_synthesis_complete_speaks(mock_say):
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)
    await narrator.on_event("synthesis.complete", {"hypothesis_count": 3})
    mock_say.assert_called_once()
    assert "3" in mock_say.call_args[0][0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon_narrator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement daemon_narrator.py**

```python
# backend/core/ouroboros/daemon_narrator.py
"""DaemonNarrator — rate-limited voice for Ouroboros autonomous events.

Deterministic: all speech templates are explicit strings.
Rate-limited: max 1 announcement per category per rate_limit_s.
No model inference for speech generation.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Event type → (category for rate limiting, speech template)
# Templates use str.format() with event payload dict
_EVENT_TEMPLATES: Dict[str, tuple] = {
    "rem.epoch_start": (
        "rem",
        "Entering REM Sleep. Scanning the organism.",
    ),
    "rem.epoch_complete": (
        "rem",
        "REM complete. Found {findings_count} issues. {envelopes_submitted} patches submitted.",
    ),
    "synthesis.complete": (
        "synthesis",
        "Roadmap analysis complete. {hypothesis_count} capability gaps identified.",
    ),
    "saga.started": (
        "saga",
        "Designing {title}. {step_count} implementation steps.",
    ),
    "saga.complete": (
        "saga",
        "Feature implemented: {title}. PR ready for review.",
    ),
    "saga.aborted": (
        "saga",
        "Saga aborted: {reason}.",
    ),
    "governance.patch_applied": (
        "patch",
        "Patch applied: {description}.",
    ),
    "vital.warn": (
        "vital",
        "Boot scan: {warning_count} warnings. REM will address them.",
    ),
}


class DaemonNarrator:
    """Voices salient Ouroboros events via safe_say()."""

    def __init__(
        self,
        say_fn: Optional[Callable] = None,
        rate_limit_s: float = 60.0,
        enabled: bool = True,
    ) -> None:
        self._say_fn = say_fn
        self._rate_limit_s = rate_limit_s
        self._enabled = enabled
        self._last_spoken_at: Dict[str, float] = {}

    async def on_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Handle an event. Speak if salient and not rate-limited."""
        if not self._enabled or self._say_fn is None:
            return

        template_entry = _EVENT_TEMPLATES.get(event_type)
        if template_entry is None:
            return  # unknown event — ignore

        category, template = template_entry

        # Rate limiting per category
        now = time.time()
        last = self._last_spoken_at.get(category, 0.0)
        if (now - last) < self._rate_limit_s:
            return  # rate limited — drop

        # Format message with payload
        try:
            message = template.format(**payload)
        except (KeyError, IndexError):
            message = template  # use raw template if format fails

        self._last_spoken_at[category] = now

        try:
            await self._say_fn(message, source="ouroboros_narrator", skip_dedup=True)
        except Exception as exc:
            logger.debug("[DaemonNarrator] Speech failed: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon_narrator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ouroboros): add DaemonNarrator with rate-limited voice for autonomous events"
```

---

## Task 7: Event Emission (REM + Saga + Synthesis → SpinalCord + Narrator)

**Files:**
- Modify: `backend/core/ouroboros/rem_sleep.py`
- Modify: `backend/core/ouroboros/architect/saga_orchestrator.py`
- Modify: `backend/core/ouroboros/roadmap/synthesis_engine.py`

- [ ] **Step 1: Add narrator parameter to RemSleepDaemon**

In `rem_sleep.py __init__`, add `narrator: Any = None`. Store as `self._narrator`.

In `_run_epoch()`, after the epoch result is computed, emit events:

```python
# After result = await epoch.run(self._current_token):

# Emit epoch complete to SpinalCord
await self._spinal_cord.stream_up("rem.epoch_complete", {
    "epoch_id": epoch_id,
    "findings_count": result.findings_count,
    "envelopes_submitted": result.envelopes_submitted,
    "duration_s": result.duration_s,
})

# Notify narrator
if self._narrator is not None:
    await self._narrator.on_event("rem.epoch_complete", {
        "findings_count": result.findings_count,
        "envelopes_submitted": result.envelopes_submitted,
    })
```

Also emit `rem.epoch_start` at the beginning of `_run_epoch()`:

```python
# At start of _run_epoch(), after creating epoch_id:
await self._spinal_cord.stream_up("rem.epoch_start", {"epoch_id": epoch_id})
if self._narrator is not None:
    await self._narrator.on_event("rem.epoch_start", {"epoch_id": epoch_id})
```

- [ ] **Step 2: Add spinal_cord + narrator to SagaOrchestrator**

In `saga_orchestrator.py __init__`, add `spinal_cord: Any = None` and `narrator: Any = None`.

In `execute()`, emit saga events:

```python
# After saga.phase = SagaPhase.RUNNING:
if self._spinal_cord:
    await self._spinal_cord.stream_up("saga.started", {
        "saga_id": saga_id, "title": plan.title, "step_count": len(plan.steps),
    })
if self._narrator:
    await self._narrator.on_event("saga.started", {
        "title": plan.title, "step_count": len(plan.steps),
    })

# After saga.phase = SagaPhase.COMPLETE:
if self._spinal_cord:
    await self._spinal_cord.stream_up("saga.complete", {"saga_id": saga_id, "title": plan.title})
if self._narrator:
    await self._narrator.on_event("saga.complete", {"title": plan.title})

# After saga.phase = SagaPhase.ABORTED:
if self._spinal_cord:
    await self._spinal_cord.stream_up("saga.aborted", {"saga_id": saga_id, "reason": saga.abort_reason})
if self._narrator:
    await self._narrator.on_event("saga.aborted", {"reason": saga.abort_reason or "unknown"})
```

- [ ] **Step 3: Add narrator callback to SynthesisEngine**

In `synthesis_engine.py __init__`, add `narrator: Any = None`. Store it.

At the end of `_run_synthesis()`, after persisting:

```python
# After self._cache.save(...)
if self._narrator is not None:
    await self._narrator.on_event("synthesis.complete", {
        "hypothesis_count": len(merged),
    })
```

- [ ] **Step 4: Verify existing tests pass**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_sleep.py tests/core/ouroboros/architect/test_saga_orchestrator.py tests/core/ouroboros/roadmap/test_synthesis_engine.py -v --tb=short`
Expected: All PASS (narrator defaults to None)

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ouroboros): emit lifecycle events to SpinalCord + DaemonNarrator"
```

---

## Task 8: Daemon Config + Wiring

**Files:**
- Modify: `backend/core/ouroboros/daemon_config.py`
- Modify: `backend/core/ouroboros/daemon.py`

- [ ] **Step 1: Add narrator config fields**

In `daemon_config.py`, add after architect fields:

```python
    # DaemonNarrator
    narrator_enabled: bool = True
    narrator_rate_limit_s: float = 60.0
```

And in `from_env()`:

```python
    narrator_enabled=_env_bool("OUROBOROS_NARRATOR_ENABLED", True),
    narrator_rate_limit_s=_env_float("OUROBOROS_NARRATOR_RATE_LIMIT_S", 60.0),
```

- [ ] **Step 2: Wire DaemonNarrator into daemon.py**

In `daemon.py`, after Phase 2 (SpinalCord) and before Phase 3 (REM):

```python
# Between Phase 2 and Phase 3:
self._narrator = None
if self._config.narrator_enabled:
    try:
        from backend.core.ouroboros.daemon_narrator import DaemonNarrator
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        self._narrator = DaemonNarrator(
            say_fn=safe_say,
            rate_limit_s=self._config.narrator_rate_limit_s,
        )
        logger.info("[OuroborosDaemon] DaemonNarrator enabled")
    except Exception as exc:
        logger.warning("[OuroborosDaemon] DaemonNarrator init failed: %s", exc)
```

Pass `narrator=self._narrator` to RemSleepDaemon, SagaOrchestrator, and SynthesisEngine where they're constructed.

- [ ] **Step 3: Verify existing tests pass**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon.py tests/core/ouroboros/test_daemon_integration.py -v --tb=short`
Expected: All PASS (narrator defaults to None)

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(ouroboros): wire DaemonNarrator into daemon lifecycle with config"
```

---

## Task 9: Integration Test

**Files:**
- Create: `tests/core/ouroboros/test_model_wiring_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/core/ouroboros/test_model_wiring_integration.py
"""E2E: Doubleword prompt -> parse -> hypothesis/plan + narrator speaks."""
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.daemon_narrator import DaemonNarrator
from backend.core.ouroboros.roadmap.synthesis_engine import FeatureSynthesisEngine, SynthesisConfig
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment, RoadmapSnapshot
from backend.core.ouroboros.architect.reasoning_agent import ArchitectureReasoningAgent, AgentConfig


def _snapshot():
    frag = SnapshotFragment(
        source_id="spec:manifesto", uri="test.md", tier=0,
        content_hash="abc", fetched_at=time.time(), mtime=time.time(),
        title="Manifesto", summary="Build WhatsApp and Slack agents", fragment_type="spec",
    )
    return RoadmapSnapshot.create(fragments=(frag,), previous_version=0)


@pytest.mark.asyncio
async def test_synthesis_with_doubleword_produces_hypotheses(tmp_path):
    """Full pipeline: snapshot -> synthesis prompt -> 397B -> parsed hypotheses."""
    mock_dw = AsyncMock()
    mock_dw.prompt_only = AsyncMock(return_value=json.dumps({
        "gaps": [
            {
                "description": "Missing WhatsApp agent",
                "evidence_fragments": ["spec:manifesto"],
                "gap_type": "missing_capability",
                "confidence": 0.85,
                "urgency": "high",
                "suggested_scope": "backend/agents/",
                "suggested_repos": ["jarvis"],
            },
            {
                "description": "Missing Slack integration",
                "evidence_fragments": ["spec:manifesto"],
                "gap_type": "missing_capability",
                "confidence": 0.8,
                "urgency": "normal",
                "suggested_scope": "backend/agents/",
                "suggested_repos": ["jarvis"],
            },
        ]
    }))

    engine = FeatureSynthesisEngine(
        oracle=MagicMock(find_nodes_by_name=MagicMock(return_value=[])),
        doubleword=mock_dw,
        cache=HypothesisCache(cache_dir=tmp_path),
        config=SynthesisConfig(min_interval_s=0),
    )

    result = await engine.synthesize(_snapshot(), force=True)
    model_hyps = [h for h in result if h.provenance.startswith("model:")]
    assert len(model_hyps) >= 2
    assert any("WhatsApp" in h.description for h in model_hyps)


@pytest.mark.asyncio
async def test_narrator_speaks_on_synthesis_complete():
    """Narrator receives synthesis.complete event and speaks."""
    mock_say = AsyncMock(return_value=True)
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)

    await narrator.on_event("synthesis.complete", {"hypothesis_count": 3})
    mock_say.assert_called_once()
    assert "3" in mock_say.call_args[0][0]
    assert "gap" in mock_say.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_narrator_speaks_on_saga_complete():
    mock_say = AsyncMock(return_value=True)
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)

    await narrator.on_event("saga.complete", {"title": "WhatsApp Agent"})
    mock_say.assert_called_once()
    assert "WhatsApp" in mock_say.call_args[0][0]


@pytest.mark.asyncio
async def test_full_flow_synthesis_plus_narrator(tmp_path):
    """Synthesis completes + narrator fires."""
    mock_say = AsyncMock(return_value=True)
    narrator = DaemonNarrator(say_fn=mock_say, rate_limit_s=0)

    mock_dw = AsyncMock()
    mock_dw.prompt_only = AsyncMock(return_value=json.dumps({
        "gaps": [{
            "description": "Missing agent",
            "evidence_fragments": ["spec:test"],
            "gap_type": "missing_capability",
            "confidence": 0.9,
            "urgency": "normal",
            "suggested_scope": "backend/",
            "suggested_repos": ["jarvis"],
        }]
    }))

    engine = FeatureSynthesisEngine(
        oracle=MagicMock(find_nodes_by_name=MagicMock(return_value=[])),
        doubleword=mock_dw,
        cache=HypothesisCache(cache_dir=tmp_path),
        config=SynthesisConfig(min_interval_s=0),
        narrator=narrator,
    )

    result = await engine.synthesize(_snapshot(), force=True)
    assert len(result) > 0
    # Narrator should have been called with synthesis.complete
    assert mock_say.called
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/core/ouroboros/test_model_wiring_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Run full Ouroboros test suite**

Run: `python3 -m pytest tests/core/ouroboros/ -v --tb=short 2>&1 | tail -20`
Expected: All existing + new tests PASS

- [ ] **Step 4: Commit**

```bash
git commit -m "test(ouroboros): add E2E integration tests for model wiring + narrator"
```

---

## Summary

| Task | What it builds | New files | Modified files |
|------|---------------|-----------|---------------|
| 1 | prompt_only() on DoublewordProvider | 1 test | 1 |
| 2 | Synthesis prompt builder + shedding | 2 | 0 |
| 3 | Synthesis Engine Doubleword wiring | 1 test | 1 |
| 4 | Architect prompt builder | 2 | 0 |
| 5 | Architect Doubleword wiring | 1 test | 1 |
| 6 | DaemonNarrator | 2 | 0 |
| 7 | Event emission (REM + Saga + Synthesis) | 0 | 3 |
| 8 | Config + daemon wiring | 0 | 2 |
| 9 | Integration tests | 1 | 0 |
| **Total** | | **10 new** | **8 modified** |
