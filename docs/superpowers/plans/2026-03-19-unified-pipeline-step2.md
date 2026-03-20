# Unified Thinking Pipeline — Step 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `POST /v1/reason` — the canonical Mind API where J-Prime reasons through commands (classify, decompose, validate, assemble plan) and returns executable plans to JARVIS.

**Architecture:** LangGraph StateGraph on J-Prime with 4 nodes (Analysis, Planning, Validation, ExecutionPlanner). GPU inference via injected ModelProvider (off-thread, with timeout). Validation runs on ALL plan paths (LightValidation for trivial/light). JARVIS calls via MindClient.send_command() behind feature flag.

**Tech Stack:** Python 3.11+, Pydantic v2, LangGraph (StateGraph), llama-cpp-python (via ModelProvider), SQLite (idempotency), aiohttp (MindClient), pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-03-19-unified-pipeline-step2-design.md`

**Repos:** `jarvis-prime` (Tasks 1-9), `JARVIS-AI-Agent` (Tasks 10-11)

---

## File Structure

### J-Prime (new files)

| File | Responsibility |
|------|---------------|
| `jarvis_prime/reasoning/model_provider.py` | ModelProvider protocol + LlamaCppModelProvider + MockModelProvider |
| `jarvis_prime/reasoning/graph_nodes/__init__.py` | Package exports |
| `jarvis_prime/reasoning/graph_nodes/analysis_node.py` | Intent classification via ModelProvider |
| `jarvis_prime/reasoning/graph_nodes/planning_node.py` | Sub-goal decomposition via ModelProvider |
| `jarvis_prime/reasoning/graph_nodes/validation_node.py` | Rule-based 3-gate validation (shared Full + Light) |
| `jarvis_prime/reasoning/graph_nodes/execution_planner.py` | Per-step brain+tool assignment + plan assembly |
| `jarvis_prime/reasoning/reasoning_graph.py` | LangGraph StateGraph + depth routing |
| `jarvis_prime/reasoning/idempotency_store.py` | SQLite-backed request_id dedupe |
| `tests/reasoning/test_model_provider.py` | ModelProvider tests |
| `tests/reasoning/test_analysis_node.py` | AnalysisNode tests |
| `tests/reasoning/test_planning_node.py` | PlanningNode tests |
| `tests/reasoning/test_validation_node.py` | ValidationNode tests |
| `tests/reasoning/test_execution_planner.py` | ExecutionPlanner tests |
| `tests/reasoning/test_reasoning_graph.py` | Full graph integration tests |
| `tests/reasoning/test_idempotency.py` | Idempotency store tests |
| `tests/reasoning/test_handle_reason.py` | POST /v1/reason endpoint tests |

### J-Prime (modified files)

| File | Change |
|------|--------|
| `jarvis_prime/reasoning/protocol.py` | Add ReasoningGraphState |
| `jarvis_prime/reasoning/endpoints.py` | Add handle_reason() |
| `jarvis_prime/reasoning/__init__.py` | New exports |
| `jarvis_prime/server.py` | Register POST /v1/reason route |

### JARVIS (modified files)

| File | Change |
|------|--------|
| `backend/core/mind_client.py` | Add send_command() method |
| `backend/api/unified_command_processor.py` | Wire send_command() behind feature flag |

---

## Task 1: ModelProvider Protocol (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/model_provider.py`
- Test: `tests/reasoning/test_model_provider.py`

- [ ] **Step 1: Write the failing test**

File: `tests/reasoning/test_model_provider.py`

```python
"""Tests for ModelProvider protocol and implementations."""
import asyncio
import pytest
from jarvis_prime.reasoning.model_provider import (
    ModelProvider,
    MockModelProvider,
    LlamaCppModelProvider,
)


class TestMockModelProvider:
    @pytest.mark.asyncio
    async def test_mock_returns_canned_response(self):
        provider = MockModelProvider(response="test output")
        result = await provider.infer(messages=[{"role": "user", "content": "hi"}])
        assert result["content"] == "test output"

    @pytest.mark.asyncio
    async def test_mock_failure_mode(self):
        provider = MockModelProvider(should_fail=True)
        with pytest.raises(RuntimeError, match="Mock model failure"):
            await provider.infer(messages=[])

    def test_mock_is_model_loaded(self):
        assert MockModelProvider().is_model_loaded() is True
        assert MockModelProvider(should_fail=True).is_model_loaded() is False

    def test_mock_loaded_model_name(self):
        assert MockModelProvider().loaded_model_name() == "mock-model"

    def test_mock_satisfies_protocol(self):
        provider = MockModelProvider()
        assert isinstance(provider, ModelProvider)


class TestLlamaCppModelProvider:
    @pytest.mark.asyncio
    async def test_no_model_raises(self):
        provider = LlamaCppModelProvider(get_model_fn=lambda: None)
        with pytest.raises(RuntimeError, match="No model loaded"):
            await provider.infer(messages=[{"role": "user", "content": "hi"}])

    def test_is_model_loaded_false_when_none(self):
        provider = LlamaCppModelProvider(get_model_fn=lambda: None)
        assert provider.is_model_loaded() is False

    def test_satisfies_protocol(self):
        provider = LlamaCppModelProvider(get_model_fn=lambda: None)
        assert isinstance(provider, ModelProvider)

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        """Simulate a model that takes too long."""
        import time
        class SlowModel:
            def create_chat_completion(self, **kwargs):
                time.sleep(5)  # way over timeout
                return {"choices": [{"message": {"content": "slow"}}]}
        provider = LlamaCppModelProvider(get_model_fn=lambda: SlowModel())
        with pytest.raises(asyncio.TimeoutError):
            await provider.infer(messages=[], timeout_s=0.1)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_model_provider.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement model_provider.py**

File: `jarvis_prime/reasoning/model_provider.py`

```python
"""
ModelProvider — dependency injection interface for GPU model inference.

Graph nodes accept a ModelProvider via constructor, never import from server.py.
This enables testing with MockModelProvider and decouples reasoning from server lifecycle.

Spec: Section 12 of unified-pipeline-step2-design.md
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger("reasoning.model_provider")


@runtime_checkable
class ModelProvider(Protocol):
    """Interface for GPU model inference. Injected into graph nodes."""

    async def infer(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout_s: float = 10.0,
    ) -> Dict[str, Any]:
        """Run inference. Returns {"content": "..."}. Raises on failure."""
        ...

    def is_model_loaded(self) -> bool:
        ...

    def loaded_model_name(self) -> str:
        ...


class MockModelProvider:
    """Test double for ModelProvider. No GPU needed."""

    def __init__(
        self,
        response: str = "mock response",
        should_fail: bool = False,
    ) -> None:
        self._response = response
        self._should_fail = should_fail
        self.call_count = 0
        self.last_messages: Optional[list] = None

    async def infer(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout_s: float = 10.0,
    ) -> Dict[str, Any]:
        self.call_count += 1
        self.last_messages = messages
        if self._should_fail:
            raise RuntimeError("Mock model failure")
        return {"content": self._response}

    def is_model_loaded(self) -> bool:
        return not self._should_fail

    def loaded_model_name(self) -> str:
        return "mock-model" if not self._should_fail else ""


class LlamaCppModelProvider:
    """Production ModelProvider wrapping a llama-cpp-python Llama instance.

    GPU inference runs OFF the event loop via run_in_executor.
    Timeout enforced via asyncio.wait_for.
    """

    def __init__(self, get_model_fn: Callable[[], Optional[Any]]) -> None:
        self._get_model = get_model_fn

    async def infer(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout_s: float = 10.0,
    ) -> Dict[str, Any]:
        model = self._get_model()
        if model is None:
            raise RuntimeError("No model loaded")

        loop = asyncio.get_running_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                functools.partial(
                    model.create_chat_completion,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
            ),
            timeout=timeout_s,
        )
        return {"content": response["choices"][0]["message"]["content"]}

    def is_model_loaded(self) -> bool:
        return self._get_model() is not None

    def loaded_model_name(self) -> str:
        model = self._get_model()
        if model is None:
            return ""
        return getattr(model, "model_path", "unknown")
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_model_provider.py -v
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/model_provider.py tests/reasoning/test_model_provider.py
git commit -m "feat(reasoning): add ModelProvider protocol with DI for GPU inference

ModelProvider protocol, MockModelProvider for tests, LlamaCppModelProvider
for production. GPU inference runs off event loop via run_in_executor
with configurable timeout."
```

---

## Task 2: ReasoningGraphState + Plan Hash (J-Prime)

**Files:**
- Modify: `jarvis_prime/reasoning/protocol.py`
- Test: `tests/reasoning/test_protocol.py` (add tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/reasoning/test_protocol.py`:

```python
from jarvis_prime.reasoning.protocol import ReasoningGraphState, compute_plan_hash, Plan, SubGoal


class TestReasoningGraphState:
    def test_default_state(self):
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1", command="test"
        )
        assert state.phase == "initializing"
        assert state.served_mode == "LEVEL_0_PRIMARY"
        assert state.complexity == "light"
        assert state.confidence == 0.0
        assert state.approval_required is False

    def test_roundtrip(self):
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1",
            command="analyze", complexity="complex", confidence=0.87,
        )
        data = state.model_dump()
        restored = ReasoningGraphState.model_validate(data)
        assert restored.complexity == "complex"
        assert restored.confidence == 0.87


class TestPlanHash:
    def test_deterministic(self):
        plan = Plan(
            plan_id="p1", plan_hash="",
            sub_goals=[SubGoal(step_id="s1", action_id="a1", goal="open Safari",
                              task_type="system_command", brain_assigned="phi3_lightweight",
                              tool_required="app_control")],
            execution_strategy="sequential", approval_required=False,
        )
        h1 = compute_plan_hash(plan)
        h2 = compute_plan_hash(plan)
        assert h1 == h2
        assert len(h1) == 64  # full SHA-256

    def test_different_plans_different_hash(self):
        plan_a = Plan(plan_id="p1", plan_hash="",
                      sub_goals=[SubGoal(step_id="s1", action_id="a1", goal="open Safari",
                                        task_type="system_command", brain_assigned="phi3",
                                        tool_required="app_control")])
        plan_b = Plan(plan_id="p2", plan_hash="",
                      sub_goals=[SubGoal(step_id="s1", action_id="a1", goal="open Chrome",
                                        task_type="system_command", brain_assigned="phi3",
                                        tool_required="app_control")])
        assert compute_plan_hash(plan_a) != compute_plan_hash(plan_b)

    def test_plan_id_excluded_from_hash(self):
        """plan_id is excluded to avoid circular dependency."""
        plan_a = Plan(plan_id="aaa", plan_hash="", sub_goals=[
            SubGoal(step_id="s1", action_id="a1", goal="test",
                    task_type="t", brain_assigned="b", tool_required="x")])
        plan_b = Plan(plan_id="bbb", plan_hash="", sub_goals=[
            SubGoal(step_id="s1", action_id="a1", goal="test",
                    task_type="t", brain_assigned="b", tool_required="x")])
        assert compute_plan_hash(plan_a) == compute_plan_hash(plan_b)

    def test_float_normalization(self):
        """Floats in depends_on or other fields should not affect hash stability."""
        plan = Plan(plan_id="p1", plan_hash="",
                    sub_goals=[SubGoal(step_id="s1", action_id="a1", goal="test",
                                      task_type="t", brain_assigned="b", tool_required="x")],
                    approval_required=True)
        h = compute_plan_hash(plan)
        assert isinstance(h, str) and len(h) == 64
```

- [ ] **Step 2: Run tests to verify new tests fail**

- [ ] **Step 3: Add ReasoningGraphState and compute_plan_hash to protocol.py**

Add at the end of `jarvis_prime/reasoning/protocol.py`:

```python
import math

# ---------------------------------------------------------------------------
# Internal graph state (J-Prime only, not on the wire)
# ---------------------------------------------------------------------------

class ReasoningGraphState(BaseModel):
    """State that flows between LangGraph nodes. Internal to J-Prime."""
    request_id: str
    session_id: str
    trace_id: str
    command: str
    context: Dict[str, Any] = Field(default_factory=dict)

    phase: str = "initializing"
    served_mode: str = "LEVEL_0_PRIMARY"
    degraded_reason_code: Optional[str] = None

    intent: str = ""
    complexity: str = "light"
    confidence: float = 0.0
    inferred_goals: List[str] = Field(default_factory=list)
    analysis_brain_used: str = ""

    sub_goals: List[Dict[str, Any]] = Field(default_factory=list)
    action_graph: Dict[str, List[str]] = Field(default_factory=dict)
    execution_strategy: str = "sequential"
    planning_brain_used: str = ""

    approval_required: bool = False
    approval_reason_codes: List[str] = Field(default_factory=list)
    risk_level: str = "low"
    cost_gate_passed: bool = True
    resource_gate_passed: bool = True

    graph_depth: str = "fast"
    error_count: int = 0
    reasoning_trace: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Canonical plan hashing
# ---------------------------------------------------------------------------

def _normalize_value(v: Any) -> Any:
    """Normalize a value for canonical JSON hashing."""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return f"{v:.6f}".rstrip('0').rstrip('.')
    if isinstance(v, dict):
        return {k: _normalize_value(val) for k, val in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_normalize_value(item) for item in v]
    return v


def compute_plan_hash(plan: Plan) -> str:
    """Full SHA-256 of canonical JSON plan representation.

    Canonicalization: sorted keys, no whitespace, floats to 6dp,
    NaN/Inf as strings, depends_on sorted, plan_id excluded.
    """
    import hashlib
    import json

    hashable = _normalize_value({
        "sub_goals": [
            {
                "step_id": sg.step_id,
                "action_id": sg.action_id,
                "goal": sg.goal,
                "task_type": sg.task_type,
                "brain_assigned": sg.brain_assigned,
                "tool_required": sg.tool_required,
                "depends_on": sorted(sg.depends_on),
            }
            for sg in plan.sub_goals
        ],
        "execution_strategy": plan.execution_strategy,
        "approval_required": plan.approval_required,
    })
    canonical = json.dumps(hashable, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Update __init__.py exports**

- [ ] **Step 5: Run tests — verify all pass**

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/protocol.py jarvis_prime/reasoning/__init__.py tests/reasoning/test_protocol.py
git commit -m "feat(reasoning): add ReasoningGraphState + canonical plan hashing

Internal graph state model for LangGraph nodes. Full SHA-256 plan hash
with float normalization and deterministic key ordering."
```

---

## Task 3: IdempotencyStore (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/idempotency_store.py`
- Test: `tests/reasoning/test_idempotency.py`

- [ ] **Step 1: Write the failing test**

File: `tests/reasoning/test_idempotency.py`

```python
"""Tests for SQLite-backed idempotency store."""
import os
import pytest
import tempfile
from jarvis_prime.reasoning.idempotency_store import IdempotencyStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test_idempotency.db"
    return IdempotencyStore(db_path=str(db_path))


class TestIdempotencyStore:
    def test_store_and_retrieve(self, store):
        store.store("req-001", "sess-001", '{"status": "plan_ready"}')
        cached = store.get("req-001")
        assert cached is not None
        assert '"plan_ready"' in cached

    def test_miss_returns_none(self, store):
        assert store.get("nonexistent") is None

    def test_duplicate_returns_cached(self, store):
        store.store("req-002", "sess-001", '{"first": true}')
        store.store("req-002", "sess-001", '{"second": true}')
        cached = store.get("req-002")
        assert '"first"' in cached  # first write wins

    def test_prune_removes_old(self, store):
        store.store("req-old", "sess-001", '{"old": true}')
        # Force the entry to appear old
        store._conn.execute(
            "UPDATE idempotency SET created_at = datetime('now', '-25 hours') WHERE request_id = 'req-old'"
        )
        store._conn.commit()
        store.prune(window_hours=24)
        assert store.get("req-old") is None

    def test_max_entries_eviction(self, store):
        store._max_entries = 5
        for i in range(10):
            store.store(f"req-{i:03d}", "sess", f'{{"i": {i}}}')
        # Should have at most 5+1 entries (eviction runs after threshold)
        count = store._conn.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
        assert count <= 6

    def test_persists_across_instances(self, tmp_path):
        db_path = str(tmp_path / "persist.db")
        store1 = IdempotencyStore(db_path=db_path)
        store1.store("req-persist", "sess", '{"data": 1}')
        store1.close()
        store2 = IdempotencyStore(db_path=db_path)
        assert store2.get("req-persist") is not None
        store2.close()
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement idempotency_store.py**

File: `jarvis_prime/reasoning/idempotency_store.py`

```python
"""
SQLite-backed idempotency store for request_id dedupe.

Survives J-Prime restarts. Prunes entries older than configurable window.
Spec: Section 7 of unified-pipeline-step2-design.md
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger("reasoning.idempotency")

_DEFAULT_DB_PATH = os.path.expanduser("~/.jarvis-prime/reasoning/idempotency.db")


class IdempotencyStore:
    """SQLite-backed request_id deduplication."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        window_hours: int = 24,
        max_entries: int = 10_000,
    ) -> None:
        self._db_path = db_path or os.getenv("REASON_IDEMPOTENCY_DB", _DEFAULT_DB_PATH)
        self._window_hours = int(os.getenv("REASON_IDEMPOTENCY_WINDOW_H", str(window_hours)))
        self._max_entries = max_entries

        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_table()
        self.prune(self._window_hours)

    def _create_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS idempotency (
                request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                response_json TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_created ON idempotency(created_at)"
        )
        self._conn.commit()

    def get(self, request_id: str) -> Optional[str]:
        """Return cached response JSON or None."""
        row = self._conn.execute(
            "SELECT response_json FROM idempotency WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return row[0] if row else None

    def store(self, request_id: str, session_id: str, response_json: str) -> None:
        """Store response. First write wins (INSERT OR IGNORE)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO idempotency (request_id, session_id, response_json) VALUES (?, ?, ?)",
            (request_id, session_id, response_json),
        )
        self._conn.commit()
        self._maybe_evict()

    def prune(self, window_hours: Optional[int] = None) -> int:
        """Delete entries older than window. Returns count deleted."""
        hours = window_hours or self._window_hours
        cursor = self._conn.execute(
            "DELETE FROM idempotency WHERE created_at < datetime('now', ?)",
            (f"-{hours} hours",),
        )
        self._conn.commit()
        return cursor.rowcount

    def _maybe_evict(self) -> None:
        count = self._conn.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
        if count > self._max_entries:
            excess = count - self._max_entries
            self._conn.execute(
                "DELETE FROM idempotency WHERE request_id IN "
                "(SELECT request_id FROM idempotency ORDER BY created_at ASC LIMIT ?)",
                (excess,),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run tests — verify all pass**

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/idempotency_store.py tests/reasoning/test_idempotency.py
git commit -m "feat(reasoning): add SQLite idempotency store for request_id dedupe

Survives restarts, auto-prunes old entries, max 10k with eviction.
INSERT OR IGNORE ensures first-write-wins semantics."
```

---

## Task 4: AnalysisNode (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/graph_nodes/__init__.py`
- Create: `jarvis_prime/reasoning/graph_nodes/analysis_node.py`
- Test: `tests/reasoning/test_analysis_node.py`

- [ ] **Step 1: Create graph_nodes package**

```bash
mkdir -p jarvis_prime/reasoning/graph_nodes
touch jarvis_prime/reasoning/graph_nodes/__init__.py
```

- [ ] **Step 2: Write the failing test**

File: `tests/reasoning/test_analysis_node.py`

```python
"""Tests for AnalysisNode — intent classification via ModelProvider."""
import pytest
from jarvis_prime.reasoning.model_provider import MockModelProvider
from jarvis_prime.reasoning.protocol import ReasoningGraphState
from jarvis_prime.reasoning.graph_nodes.analysis_node import AnalysisNode


@pytest.fixture
def mock_provider():
    return MockModelProvider(response='{"intent": "browser_navigation", "complexity": "heavy", "confidence": 0.88, "goals": ["navigate to linkedin"]}')


@pytest.fixture
def failing_provider():
    return MockModelProvider(should_fail=True)


class TestAnalysisNode:
    @pytest.mark.asyncio
    async def test_successful_analysis(self, mock_provider):
        node = AnalysisNode(model_provider=mock_provider)
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1",
            command="go to LinkedIn",
        )
        result = await node.process(state)
        assert result.phase == "analyzing"
        assert result.intent != ""
        assert result.confidence > 0
        assert result.served_mode == "LEVEL_0_PRIMARY"

    @pytest.mark.asyncio
    async def test_model_failure_degrades(self, failing_provider):
        node = AnalysisNode(model_provider=failing_provider)
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1",
            command="analyze competitor strategy",
        )
        result = await node.process(state)
        assert result.served_mode == "LEVEL_1_DEGRADED"
        assert result.confidence <= 0.6
        assert result.degraded_reason_code is not None

    @pytest.mark.asyncio
    async def test_pattern_fallback_classifies(self, failing_provider):
        node = AnalysisNode(model_provider=failing_provider)
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1",
            command="open Safari",
        )
        result = await node.process(state)
        # Pattern fallback should still classify "open" as system_command/trivial
        assert result.intent != ""
        assert result.complexity in ("trivial", "light", "heavy", "complex")

    @pytest.mark.asyncio
    async def test_graph_depth_set(self, mock_provider):
        node = AnalysisNode(model_provider=mock_provider)
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1",
            command="go to LinkedIn",
        )
        result = await node.process(state)
        assert result.graph_depth in ("fast", "standard", "full")

    @pytest.mark.asyncio
    async def test_reasoning_trace_appended(self, mock_provider):
        node = AnalysisNode(model_provider=mock_provider)
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1",
            command="test",
        )
        result = await node.process(state)
        assert len(result.reasoning_trace) == 1
        assert result.reasoning_trace[0]["node"] == "analysis"
```

- [ ] **Step 3: Run test to verify it fails**

- [ ] **Step 4: Implement analysis_node.py**

File: `jarvis_prime/reasoning/graph_nodes/analysis_node.py`

```python
"""
AnalysisNode — intent classification for the ONE thinking pipeline.

Calls GPU model via injected ModelProvider for classification.
Falls back to pattern matching on model failure, marking response degraded.

Spec: Sections 2-3 of unified-pipeline-step2-design.md
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

from jarvis_prime.reasoning.model_provider import ModelProvider
from jarvis_prime.reasoning.protocol import ReasoningGraphState
from jarvis_prime.reasoning.unified_brain_selector import UnifiedBrainSelector

logger = logging.getLogger("reasoning.analysis")

_GRAPH_DEPTH = {"trivial": "fast", "light": "fast", "heavy": "standard", "complex": "full"}

# Pattern fallback — same keyword rules as UnifiedBrainSelector
_INTENT_PATTERNS: Dict[str, list] = {
    "system_command": ["open", "close", "launch", "quit", "volume", "brightness"],
    "browser_navigation": ["navigate", "go to", "browse", "visit", "search", "google"],
    "email_compose": ["email", "send", "reply", "draft", "compose", "message"],
    "calendar_query": ["schedule", "calendar", "meeting", "appointment", "free time"],
    "vision_action": ["click", "tap", "press", "scroll", "type", "fill"],
    "complex_reasoning": ["analyze", "compare", "evaluate", "investigate", "explain why"],
    "multi_step_planning": ["plan", "workflow", "automate", "orchestrate", "research.*build"],
}

_SYSTEM_PROMPT = """You are a command classifier for JARVIS AI assistant.
Given a user command, classify it as JSON with these fields:
- "intent": the primary intent category (e.g., "browser_navigation", "system_command", "email_compose", "complex_reasoning", "multi_step_planning")
- "complexity": one of "trivial", "light", "heavy", "complex"
- "confidence": 0.0 to 1.0
- "goals": list of inferred sub-goals (strings)
Respond with ONLY valid JSON, no explanation."""


class AnalysisNode:
    """Classifies intent and complexity via GPU model with pattern fallback."""

    def __init__(
        self,
        model_provider: ModelProvider,
        brain_selector: Optional[UnifiedBrainSelector] = None,
    ) -> None:
        self._provider = model_provider
        self._selector = brain_selector or UnifiedBrainSelector()

    async def process(self, state: ReasoningGraphState) -> ReasoningGraphState:
        start_ms = time.perf_counter() * 1000

        # Pick brain for analysis
        selection = self._selector.select("classification", state.command)

        # Try GPU model, fall back to patterns
        result, mode = await self._classify(state)

        # Update state
        state.phase = "analyzing"
        state.served_mode = mode
        state.intent = result.get("intent", "general_query")
        state.complexity = result.get("complexity", "light")
        state.confidence = result.get("confidence", 0.5)
        state.inferred_goals = result.get("goals", [state.command])
        state.analysis_brain_used = selection.brain_id
        state.graph_depth = _GRAPH_DEPTH.get(state.complexity, "fast")

        if mode != "LEVEL_0_PRIMARY":
            state.degraded_reason_code = result.get(
                "degraded_reason", "ANALYSIS_MODEL_UNAVAILABLE"
            )

        # Trace
        elapsed = time.perf_counter() * 1000 - start_ms
        state.reasoning_trace.append({
            "node": "analysis",
            "intent": state.intent,
            "complexity": state.complexity,
            "confidence": state.confidence,
            "brain_used": selection.brain_id,
            "mode": mode,
            "duration_ms": round(elapsed, 1),
        })

        return state

    async def _classify(
        self, state: ReasoningGraphState
    ) -> Tuple[Dict[str, Any], str]:
        """Call model or fall back to patterns."""
        try:
            response = await self._provider.infer(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": state.command},
                ],
                max_tokens=256,
                temperature=0.1,
            )
            parsed = json.loads(response["content"])
            return parsed, "LEVEL_0_PRIMARY"
        except json.JSONDecodeError:
            # Model returned non-JSON — degrade
            logger.warning("[AnalysisNode] Model returned non-JSON, using patterns")
            result = self._pattern_fallback(state.command)
            result["confidence"] = min(result.get("confidence", 0.5), 0.6)
            result["degraded_reason"] = "ANALYSIS_MODEL_INVALID_JSON"
            return result, "LEVEL_1_DEGRADED"
        except Exception as exc:
            logger.warning("[AnalysisNode] Model call failed: %s", exc)
            result = self._pattern_fallback(state.command)
            result["confidence"] = min(result.get("confidence", 0.5), 0.6)
            result["degraded_reason"] = "ANALYSIS_MODEL_UNAVAILABLE"
            return result, "LEVEL_1_DEGRADED"

    def _pattern_fallback(self, command: str) -> Dict[str, Any]:
        """Classify using keyword patterns. No model needed."""
        cmd_lower = command.lower()
        best_intent = "general_query"
        best_score = 0

        for intent, keywords in _INTENT_PATTERNS.items():
            score = sum(1 for kw in keywords if kw in cmd_lower)
            if score > best_score:
                best_score = score
                best_intent = intent

        # Derive complexity from intent
        selection = self._selector.select(best_intent, command)
        return {
            "intent": best_intent,
            "complexity": selection.complexity,
            "confidence": min(0.6, 0.3 + best_score * 0.1),
            "goals": [command],
        }
```

- [ ] **Step 5: Run tests — verify all pass**

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/graph_nodes/ tests/reasoning/test_analysis_node.py
git commit -m "feat(reasoning): add AnalysisNode with GPU model + pattern fallback

Classifies intent and complexity via ModelProvider. Falls back to
keyword patterns on model failure, capping confidence at 0.6 and
marking response LEVEL_1_DEGRADED."
```

---

## Task 5: PlanningNode (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/graph_nodes/planning_node.py`
- Test: `tests/reasoning/test_planning_node.py`

Same TDD pattern as Task 4. Key differences:
- Calls GPU model with system prompt for sub-goal decomposition
- Keyword fallback splits command on conjunctions ("and", "then", commas)
- Updates `state.sub_goals`, `state.execution_strategy`, `state.planning_brain_used`
- Brain selection: `self._selector.select("multi_step_planning", state.command)` → picks qwen_14b/32b

- [ ] **Step 1: Write failing tests** (tests for: successful decomposition, model failure degrades, keyword fallback produces sub-goals, single-goal commands produce 1 sub-goal, reasoning trace appended)

- [ ] **Step 2: Run tests — verify they fail**

- [ ] **Step 3: Implement planning_node.py** with GPU model call + keyword fallback + UnifiedBrainSelector

- [ ] **Step 4: Run tests — verify all pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(reasoning): add PlanningNode with GPU model + keyword fallback

Decomposes commands into sub-goals via ModelProvider. Keyword fallback
splits on conjunctions. Assigns execution strategy (sequential/parallel/dag)."
```

---

## Task 6: ValidationNode (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/graph_nodes/validation_node.py`
- Test: `tests/reasoning/test_validation_node.py`

- [ ] **Step 1: Write failing tests**

File: `tests/reasoning/test_validation_node.py`

```python
"""Tests for ValidationNode — fail-closed 3-gate validation."""
import pytest
from jarvis_prime.reasoning.protocol import ReasoningGraphState
from jarvis_prime.reasoning.graph_nodes.validation_node import ValidationNode


@pytest.fixture
def node():
    return ValidationNode()


class TestCostGate:
    @pytest.mark.asyncio
    async def test_under_budget_passes(self, node):
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1", command="test",
            sub_goals=[{"brain_assigned": "phi3_lightweight", "tool_required": "app_control",
                       "task_type": "system_command", "step_id": "s1", "action_id": "a1", "goal": "test"}],
        )
        result = await node.process(state)
        assert result.cost_gate_passed is True

    @pytest.mark.asyncio
    async def test_over_budget_requires_approval(self, node):
        node._daily_spend = 4.99  # near budget
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1", command="test",
            sub_goals=[
                {"brain_assigned": "qwen_coder_32b", "tool_required": "app_control",
                 "task_type": "t", "step_id": f"s{i}", "action_id": f"a{i}", "goal": "test"}
                for i in range(10)  # 10 steps × $0.003 = $0.03, pushes over $5
            ],
        )
        result = await node.process(state)
        assert result.approval_required is True
        assert "COST_EXCEEDED" in result.approval_reason_codes


class TestResourceGate:
    @pytest.mark.asyncio
    async def test_unknown_tool_requires_approval(self, node):
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1", command="test",
            sub_goals=[{"brain_assigned": "phi3", "tool_required": "nonexistent_tool",
                       "task_type": "t", "step_id": "s1", "action_id": "a1", "goal": "test"}],
        )
        result = await node.process(state)
        assert result.approval_required is True
        assert "TOOL_UNAVAILABLE" in result.approval_reason_codes


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_high_risk_requires_approval(self, node):
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1", command="test",
            sub_goals=[{"brain_assigned": "phi3", "tool_required": "app_control",
                       "task_type": "email_compose", "step_id": "s1", "action_id": "a1",
                       "goal": "send email to boss"}],
        )
        result = await node.process(state)
        assert result.approval_required is True
        assert any("COMMUNICATION" in code for code in result.approval_reason_codes)

    @pytest.mark.asyncio
    async def test_degraded_mode_stricter(self, node):
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1", command="test",
            served_mode="LEVEL_1_DEGRADED", complexity="heavy",
            sub_goals=[{"brain_assigned": "phi3", "tool_required": "app_control",
                       "task_type": "browser_navigation", "step_id": "s1", "action_id": "a1",
                       "goal": "navigate to site"}],
        )
        result = await node.process(state)
        # Heavy + degraded = always approve
        assert result.approval_required is True


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_exception_returns_needs_approval(self, node):
        # Force an error by passing malformed sub_goals
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1", command="test",
            sub_goals="not_a_list",  # type error will cause exception
        )
        result = await node.process(state)
        assert result.approval_required is True
        assert "VALIDATION_UNAVAILABLE" in result.approval_reason_codes


class TestLightValidation:
    @pytest.mark.asyncio
    async def test_trivial_command_still_validated(self, node):
        """Even trivial/light commands pass through validation."""
        state = ReasoningGraphState(
            request_id="r1", session_id="s1", trace_id="t1",
            command="open Safari", complexity="trivial",
            sub_goals=[{"brain_assigned": "phi3_lightweight", "tool_required": "app_control",
                       "task_type": "system_command", "step_id": "s1", "action_id": "a1",
                       "goal": "open Safari"}],
        )
        result = await node.process(state)
        # Should pass validation (no high-risk, under budget, known tool)
        assert result.approval_required is False
        assert result.cost_gate_passed is True
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement validation_node.py**

File: `jarvis_prime/reasoning/graph_nodes/validation_node.py`

```python
"""
ValidationNode — fail-closed 3-gate validation for ALL plan paths.

Rule-based, deterministic, no model call. Same logic serves both
FullValidation (heavy/complex plans) and LightValidation (trivial/light).

Gates:
1. Cost: estimated_cost vs daily budget
2. Resource: tool_required in KNOWN_BODY_CAPABILITIES
3. Approval: high-risk action classes, degraded mode stricter

Fail-closed: any gate error -> needs_approval + VALIDATION_UNAVAILABLE

Spec: Section 4 of unified-pipeline-step2-design.md
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Set

from jarvis_prime.reasoning.protocol import ReasoningGraphState

logger = logging.getLogger("reasoning.validation")

KNOWN_BODY_CAPABILITIES: Set[str] = {
    "app_control", "visual_browser", "screen_capture", "computer_use",
    "voice_speak", "voice_listen", "file_ops", "workspace_query", "vision_observe",
}

HIGH_RISK_TASK_TYPES: Set[str] = {
    "file_delete", "file_overwrite",
    "payment", "purchase", "subscribe",
    "email_compose", "message_send",
    "unlock", "auth", "permission_change",
    "system_shutdown", "process_kill",
}

HIGH_RISK_REASON_MAP: Dict[str, str] = {
    "file_delete": "DESTRUCTIVE_FILE_OP", "file_overwrite": "DESTRUCTIVE_FILE_OP",
    "payment": "FINANCIAL", "purchase": "FINANCIAL", "subscribe": "FINANCIAL",
    "email_compose": "COMMUNICATION", "message_send": "COMMUNICATION",
    "unlock": "SECURITY", "auth": "SECURITY", "permission_change": "SECURITY",
    "system_shutdown": "SYSTEM_DISRUPTION", "process_kill": "SYSTEM_DISRUPTION",
}

_BRAIN_COST_ESTIMATES: Dict[str, float] = {
    "phi3_lightweight": 0.0001,
    "qwen_coder": 0.0005,
    "qwen_coder_14b": 0.001,
    "qwen_coder_32b": 0.003,
    "deepseek_r1": 0.001,
    "mistral_7b_fallback": 0.0005,
}


class ValidationNode:
    """Fail-closed 3-gate validation. Runs on ALL plan paths."""

    def __init__(self) -> None:
        self._daily_spend = 0.0
        self._daily_budget = float(os.getenv("JARVIS_DAILY_BUDGET_GCP", "5.0"))

    async def process(self, state: ReasoningGraphState) -> ReasoningGraphState:
        """Validate plan through all 3 gates. Fail-closed on any error."""
        start_ms = time.perf_counter() * 1000

        try:
            reasons: List[str] = []
            sub_goals = state.sub_goals if isinstance(state.sub_goals, list) else []

            # Gate 1: Cost
            cost_passed = self._cost_gate(sub_goals, reasons)
            state.cost_gate_passed = cost_passed

            # Gate 2: Resource
            resource_passed = self._resource_gate(sub_goals, reasons)
            state.resource_gate_passed = resource_passed

            # Gate 3: Approval
            self._approval_gate(sub_goals, state, reasons)

            state.approval_reason_codes = reasons
            state.approval_required = len(reasons) > 0
            state.risk_level = "high" if reasons else "low"

        except Exception as exc:
            # FAIL-CLOSED: any error -> needs_approval
            logger.error("[ValidationNode] Gate error (fail-closed): %s", exc)
            state.approval_required = True
            state.approval_reason_codes = ["VALIDATION_UNAVAILABLE"]
            state.risk_level = "high"

        elapsed = time.perf_counter() * 1000 - start_ms
        state.reasoning_trace.append({
            "node": "validation",
            "approval_required": state.approval_required,
            "reason_codes": state.approval_reason_codes,
            "cost_gate_passed": state.cost_gate_passed,
            "resource_gate_passed": state.resource_gate_passed,
            "duration_ms": round(elapsed, 1),
        })

        return state

    def _cost_gate(self, sub_goals: list, reasons: List[str]) -> bool:
        estimated = sum(
            _BRAIN_COST_ESTIMATES.get(sg.get("brain_assigned", ""), 0.001)
            for sg in sub_goals
        )
        if self._daily_spend + estimated > self._daily_budget:
            reasons.append("COST_EXCEEDED")
            return False
        return True

    def _resource_gate(self, sub_goals: list, reasons: List[str]) -> bool:
        passed = True
        for sg in sub_goals:
            tool = sg.get("tool_required", "")
            if tool and tool not in KNOWN_BODY_CAPABILITIES:
                reasons.append("TOOL_UNAVAILABLE")
                passed = False
                break
        return passed

    def _approval_gate(
        self, sub_goals: list, state: ReasoningGraphState, reasons: List[str]
    ) -> None:
        is_degraded = state.served_mode == "LEVEL_1_DEGRADED"

        for sg in sub_goals:
            task_type = sg.get("task_type", "")
            if task_type in HIGH_RISK_TASK_TYPES:
                reason = HIGH_RISK_REASON_MAP.get(task_type, "HIGH_RISK_ACTION")
                if reason not in reasons:
                    reasons.append(reason)

        # Degraded mode: heavy tasks always require approval
        if is_degraded and state.complexity in ("heavy", "complex"):
            if "DEGRADED_HEAVY_TASK" not in reasons:
                reasons.append("DEGRADED_HEAVY_TASK")
```

- [ ] **Step 4: Run tests — verify all pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(reasoning): add ValidationNode with fail-closed 3-gate logic

Cost gate, resource gate, approval gate. Same logic for Full and Light
validation. Fail-closed: any error -> needs_approval + VALIDATION_UNAVAILABLE.
Degraded mode is stricter, never looser."
```

---

## Task 7: ExecutionPlanner (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/graph_nodes/execution_planner.py`
- Test: `tests/reasoning/test_execution_planner.py`

Same TDD pattern. Key behavior:
- Takes validated state with sub_goals
- Per sub-goal: calls `UnifiedBrainSelector.select(task_type, goal)` to assign brain + tool
- Builds `Plan` with `plan_id` (uuid) + `plan_hash` (via `compute_plan_hash`)
- Builds `SubGoal` objects from state.sub_goals
- Assembles complete `ReasonResponse`
- For trivial/light commands with no sub_goals from PlanningNode: auto-generates a single sub-goal from the command itself

- [ ] **Step 1-5: TDD cycle** (write tests, verify fail, implement, verify pass, commit)

```bash
git commit -m "feat(reasoning): add ExecutionPlanner for plan assembly

Per-step brain+tool assignment via UnifiedBrainSelector. Builds Plan with
plan_id + canonical plan_hash. Auto-generates single sub-goal for trivial commands."
```

---

## Task 8: ReasoningGraph — LangGraph Wiring (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/reasoning_graph.py`
- Test: `tests/reasoning/test_reasoning_graph.py`

- [ ] **Step 1: Write failing tests**

File: `tests/reasoning/test_reasoning_graph.py`

```python
"""Tests for the ONE thinking pipeline — LangGraph wiring."""
import pytest
from jarvis_prime.reasoning.model_provider import MockModelProvider
from jarvis_prime.reasoning.reasoning_graph import ReasoningGraph


@pytest.fixture
def graph():
    provider = MockModelProvider(
        response='{"intent": "system_command", "complexity": "trivial", "confidence": 0.95, "goals": ["open Safari"]}'
    )
    return ReasoningGraph(model_provider=provider)


@pytest.fixture
def complex_graph():
    provider = MockModelProvider(
        response='{"intent": "multi_step_planning", "complexity": "complex", "confidence": 0.85, "goals": ["research competitors", "build spreadsheet"]}'
    )
    return ReasoningGraph(model_provider=provider)


class TestTrivialPath:
    @pytest.mark.asyncio
    async def test_trivial_skips_planning(self, graph):
        result = await graph.run(
            request_id="r1", session_id="s1", trace_id="t1",
            command="open Safari",
        )
        assert result["status"] == "plan_ready"
        assert result["classification"]["complexity"] in ("trivial", "light")
        # Should have analysis + validation + planner in trace, NOT planning
        nodes_hit = [t["node"] for t in result.get("_trace", [])]
        assert "analysis" in nodes_hit
        assert "validation" in nodes_hit
        assert "execution_planner" in nodes_hit

    @pytest.mark.asyncio
    async def test_trivial_still_validated(self, graph):
        result = await graph.run(
            request_id="r1", session_id="s1", trace_id="t1",
            command="open Safari",
        )
        # Validation must have run (no VALIDATION_UNAVAILABLE)
        assert "VALIDATION_UNAVAILABLE" not in result.get("plan", {}).get("approval_reason_codes", [])


class TestComplexPath:
    @pytest.mark.asyncio
    async def test_complex_runs_full_pipeline(self, complex_graph):
        result = await complex_graph.run(
            request_id="r1", session_id="s1", trace_id="t1",
            command="research competitors and build spreadsheet",
        )
        assert result["status"] in ("plan_ready", "needs_approval")
        nodes_hit = [t["node"] for t in result.get("_trace", [])]
        assert "analysis" in nodes_hit
        assert "planning" in nodes_hit
        assert "validation" in nodes_hit
        assert "execution_planner" in nodes_hit

    @pytest.mark.asyncio
    async def test_plan_has_hash(self, complex_graph):
        result = await complex_graph.run(
            request_id="r1", session_id="s1", trace_id="t1",
            command="research competitors and build spreadsheet",
        )
        plan = result.get("plan", {})
        assert plan.get("plan_hash", "") != ""
        assert len(plan.get("plan_hash", "")) == 64  # full SHA-256


class TestDegradedMode:
    @pytest.mark.asyncio
    async def test_model_failure_produces_degraded_response(self):
        provider = MockModelProvider(should_fail=True)
        graph = ReasoningGraph(model_provider=provider)
        result = await graph.run(
            request_id="r1", session_id="s1", trace_id="t1",
            command="analyze competitor strategy",
        )
        assert result["served_mode"] == "LEVEL_1_DEGRADED"
        assert result["classification"]["confidence"] <= 0.6
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement reasoning_graph.py**

File: `jarvis_prime/reasoning/reasoning_graph.py`

```python
"""
ReasoningGraph — the ONE thinking pipeline.

LangGraph StateGraph with depth routing:
  trivial/light:  Analysis -> LightValidation -> ExecutionPlanner
  heavy/complex:  Analysis -> Planning -> FullValidation -> ExecutionPlanner

Spec: Section 2 of unified-pipeline-step2-design.md
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from jarvis_prime.reasoning.model_provider import ModelProvider
from jarvis_prime.reasoning.protocol import (
    ReasoningGraphState,
    ReasonResponse,
    Classification,
    Plan,
    SubGoal,
    RoutingTrace,
    compute_plan_hash,
)
from jarvis_prime.reasoning.unified_brain_selector import UnifiedBrainSelector
from jarvis_prime.reasoning.graph_nodes.analysis_node import AnalysisNode
from jarvis_prime.reasoning.graph_nodes.planning_node import PlanningNode
from jarvis_prime.reasoning.graph_nodes.validation_node import ValidationNode
from jarvis_prime.reasoning.graph_nodes.execution_planner import ExecutionPlanner

logger = logging.getLogger("reasoning.graph")


class ReasoningGraph:
    """The ONE thinking pipeline. Depth determined by complexity."""

    def __init__(
        self,
        model_provider: ModelProvider,
        brain_selector: Optional[UnifiedBrainSelector] = None,
    ) -> None:
        self._selector = brain_selector or UnifiedBrainSelector()
        self._analysis = AnalysisNode(model_provider=model_provider, brain_selector=self._selector)
        self._planning = PlanningNode(model_provider=model_provider, brain_selector=self._selector)
        self._validation = ValidationNode()
        self._planner = ExecutionPlanner(brain_selector=self._selector)

    async def run(
        self,
        request_id: str,
        session_id: str,
        trace_id: str,
        command: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute the reasoning pipeline. Returns ReasonResponse-shaped dict."""
        start_ms = time.perf_counter() * 1000

        # Initialize state
        state = ReasoningGraphState(
            request_id=request_id,
            session_id=session_id,
            trace_id=trace_id,
            command=command,
            context=context or {},
        )

        # Node 1: Analysis (always)
        state = await self._analysis.process(state)

        # Depth routing
        if state.complexity in ("heavy", "complex"):
            # Full path: Planning -> Validation
            state = await self._planning.process(state)
            state = await self._validation.process(state)
        else:
            # Fast path: auto-generate single sub-goal, then LightValidation
            if not state.sub_goals:
                selection = self._selector.select(state.intent or "classification", state.command)
                state.sub_goals = [{
                    "step_id": "s1",
                    "action_id": f"act-{uuid.uuid4().hex[:8]}",
                    "goal": state.command,
                    "task_type": state.intent or "system_command",
                    "brain_assigned": selection.brain_id,
                    "tool_required": self._infer_tool(state.intent),
                    "depends_on": [],
                }]
            state = await self._validation.process(state)

        # Node: ExecutionPlanner (always)
        state = await self._planner.process(state)

        # Build response
        total_ms = time.perf_counter() * 1000 - start_ms
        logger.info(
            "[ReasoningGraph] %s → %s (%s) in %.0fms",
            command[:60], state.complexity, state.served_mode, total_ms,
        )

        return self._build_response(state)

    def _infer_tool(self, intent: str) -> str:
        """Map intent to most likely Body tool."""
        _TOOL_MAP = {
            "system_command": "app_control",
            "browser_navigation": "visual_browser",
            "vision_action": "computer_use",
            "email_compose": "workspace_query",
            "calendar_query": "workspace_query",
            "screen_observation": "screen_capture",
        }
        return _TOOL_MAP.get(intent, "app_control")

    def _build_response(self, state: ReasoningGraphState) -> Dict[str, Any]:
        """Convert internal state to ReasonResponse wire format."""
        status = "needs_approval" if state.approval_required else "plan_ready"

        return {
            "protocol_version": "1.0.0",
            "request_id": state.request_id,
            "session_id": state.session_id,
            "trace_id": state.trace_id,
            "status": status,
            "served_mode": state.served_mode,
            "degraded_reason_code": state.degraded_reason_code,
            "classification": {
                "intent": state.intent,
                "complexity": state.complexity,
                "confidence": state.confidence,
                "brain_used": state.analysis_brain_used,
                "graph_depth": state.graph_depth,
            },
            "plan": {
                "plan_id": f"plan-{uuid.uuid4().hex[:8]}",
                "plan_hash": "",  # filled by ExecutionPlanner
                "sub_goals": state.sub_goals,
                "execution_strategy": state.execution_strategy,
                "approval_required": state.approval_required,
                "approval_reason_codes": state.approval_reason_codes,
                "approval_scope": "plan",
                "risk_level": state.risk_level,
            },
            "routing_trace": {
                "analysis_brain": state.analysis_brain_used,
                "planning_brain": state.planning_brain_used,
                "cost_gate_passed": state.cost_gate_passed,
                "resource_gate_passed": state.resource_gate_passed,
            },
            "_trace": state.reasoning_trace,  # internal, stripped before wire
        }
```

- [ ] **Step 4: Run tests — verify all pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(reasoning): add ReasoningGraph — the ONE thinking pipeline

LangGraph-style depth routing: trivial/light -> fast path (analysis +
light-validation + planner), heavy/complex -> full path (+ planning).
Validation runs on ALL paths. No plan escapes without gate checks."
```

---

## Task 9: handle_reason Endpoint (J-Prime)

**Files:**
- Modify: `jarvis_prime/reasoning/endpoints.py`
- Modify: `jarvis_prime/server.py`
- Test: `tests/reasoning/test_handle_reason.py`

- [ ] **Step 1: Write failing tests**

Tests for: protocol version rejection, idempotency cache hit, trivial command returns plan, complex command returns multi-step plan, model failure returns degraded response, reasoning_graph_ready becomes True in health endpoint.

- [ ] **Step 2: Implement handle_reason() in endpoints.py**

Key logic:
1. Protocol gate: reject incompatible versions
2. Idempotency check: return cached if seen
3. Create ReasoningGraph with LlamaCppModelProvider
4. Call `graph.run()`
5. Store in idempotency cache
6. Return response

```python
async def handle_reason(req: ReasonRequest) -> dict:
    # Protocol gate
    if not _is_version_compatible(req.protocol_version):
        return _protocol_mismatch_response(req)

    # Idempotency
    store = _get_idempotency_store()
    cached = store.get(req.request_id)
    if cached is not None:
        return json.loads(cached)

    # Build graph with injected model provider
    provider = _get_model_provider()
    graph = ReasoningGraph(model_provider=provider)

    # Run reasoning
    result = await graph.run(
        request_id=req.request_id,
        session_id=req.session_id,
        trace_id=req.trace_id,
        command=req.command,
        context=req.context,
    )

    # Strip internal trace before wire
    result.pop("_trace", None)

    # Cache for idempotency
    store.store(req.request_id, req.session_id, json.dumps(result))

    return result
```

- [ ] **Step 3: Register route in server.py**

```python
        @app.post("/v1/reason")
        async def reason(request: Request):
            """Full reasoning pipeline — the Mind thinks."""
            from jarvis_prime.reasoning.endpoints import handle_reason
            from jarvis_prime.reasoning.protocol import ReasonRequest
            body = await request.json()
            req = ReasonRequest.model_validate(body)
            return await handle_reason(req)
```

- [ ] **Step 4: Update health endpoint** to return `reasoning_graph_ready: True`

- [ ] **Step 5: Run all reasoning tests**

```bash
python3 -m pytest tests/reasoning/ -v
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(reasoning): add POST /v1/reason — the canonical Mind API

Full reasoning pipeline: protocol gate -> idempotency check -> graph.run()
-> cache result. Model provider injected, graph instantiated per request.
Health endpoint now reports reasoning_graph_ready=True."
```

---

## Task 10: MindClient.send_command() (JARVIS)

**Files:**
- Modify: `backend/core/mind_client.py`
- Modify: `tests/core/test_mind_client.py`

- [ ] **Step 1: Write failing test**

```python
class TestSendCommand:
    @pytest.mark.asyncio
    async def test_send_command_returns_plan(self, client):
        mock_resp = {
            "request_id": "req-001", "session_id": "sess-001",
            "status": "plan_ready", "served_mode": "LEVEL_0_PRIMARY",
            "classification": {"intent": "system_command", "complexity": "trivial",
                             "confidence": 0.95, "brain_used": "phi3", "graph_depth": "fast"},
            "plan": {"plan_id": "p1", "plan_hash": "abc", "sub_goals": [
                {"step_id": "s1", "goal": "open Safari", "tool_required": "app_control"}
            ]},
        }
        with patch.object(client, "_http_post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.send_command("open Safari")
            assert result["status"] == "plan_ready"
            assert len(result["plan"]["sub_goals"]) == 1

    @pytest.mark.asyncio
    async def test_send_command_returns_none_at_level_2(self, client):
        for _ in range(3):
            client._record_failure()
        client._record_claude_failure()
        result = await client.send_command("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_send_command_failure_degrades(self, client):
        with patch.object(client, "_http_post", new_callable=AsyncMock, side_effect=Exception("timeout")):
            result = await client.send_command("test")
            assert result is None
```

- [ ] **Step 2: Implement send_command()** — same pattern as select_brain() but calls `/v1/reason`

- [ ] **Step 3: Run tests, commit**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
git commit -m "feat(mind): add MindClient.send_command() for full reasoning

Calls POST /v1/reason on J-Prime. Returns ReasonResponse with plan,
or None on failure (triggers tier transition)."
```

---

## Task 11: Command Processor Wiring (JARVIS)

**Files:**
- Modify: `backend/api/unified_command_processor.py`

- [ ] **Step 1: Wire send_command() behind feature flag**

In `_execute_command_pipeline()`, after the existing shadow mode block and before `response = await self._call_jprime(...)`, add:

```python
        # v295.0: Full remote reasoning via MindClient (Step 2)
        _use_remote_reasoning = os.getenv("JARVIS_USE_REMOTE_REASONING", "false").lower() == "true"

        if _use_remote_reasoning:
            try:
                from backend.core.mind_client import get_mind_client
                _mind = get_mind_client()
                _reason_result = await _mind.send_command(
                    command=command_text,
                    context=_jprime_ctx,
                    deadline_ms=int((deadline - time.monotonic()) * 1000) if deadline else None,
                )
                if _reason_result is not None:
                    # Mind returned a plan — execute it
                    return await self._execute_mind_plan(
                        _reason_result, command_text, websocket, deadline=deadline,
                    )
                # Mind unavailable — fall through to existing local path
                logger.info("[v295] Mind unavailable, using local fallback")
            except Exception as exc:
                logger.warning("[v295] Remote reasoning failed: %s — using local fallback", exc)
```

- [ ] **Step 2: Add _execute_mind_plan() method**

Add to the class near the other handler methods:

```python
    async def _execute_mind_plan(
        self, reason_result: dict, command_text: str,
        websocket=None, deadline=None,
    ) -> Dict[str, Any]:
        """Execute a plan received from J-Prime Mind."""
        status = reason_result.get("status")
        plan = reason_result.get("plan", {})
        classification = reason_result.get("classification", {})

        if status == "needs_approval":
            # TODO: wire VoiceApprovalManager here in Step 3
            return {
                "success": False,
                "response": "This action requires your approval. Please confirm.",
                "command_type": "mind_plan",
                "plan": plan,
                "needs_approval": True,
            }

        if status == "error":
            error = reason_result.get("error", {})
            return {
                "success": False,
                "response": error.get("message", "Mind returned an error."),
                "command_type": "mind_error",
                "error": error,
            }

        # status == "plan_ready" — execute sub-goals
        sub_goals = plan.get("sub_goals", [])
        results = []
        for sg in sub_goals:
            # Execute via existing action execution
            step_result = await self._execute_single_step(sg, deadline=deadline)
            results.append(step_result)

        success = all(r.get("success", False) for r in results)
        response_text = results[-1].get("response", "") if results else "Done."

        return {
            "success": success,
            "response": response_text,
            "command_type": "mind_plan",
            "served_mode": reason_result.get("served_mode"),
            "complexity": classification.get("complexity"),
            "steps_executed": len(results),
            "plan_id": plan.get("plan_id"),
        }

    async def _execute_single_step(self, sub_goal: dict, deadline=None) -> dict:
        """Execute one sub-goal from a Mind plan."""
        goal = sub_goal.get("goal", "")
        tool = sub_goal.get("tool_required", "app_control")

        try:
            # Route to existing execution based on tool type
            if tool == "app_control":
                return await self._execute_system_command(goal)
            elif tool in ("visual_browser", "computer_use"):
                return await self._handle_computer_use_action(goal, deadline=deadline)
            elif tool == "workspace_query":
                return await self._try_workspace_fast_path(goal, deadline=deadline) or {
                    "success": False, "response": "Workspace action failed"
                }
            else:
                return await self._execute_system_command(goal)
        except Exception as exc:
            return {"success": False, "response": str(exc), "error": str(exc)}
```

- [ ] **Step 3: Commit**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
git commit -m "feat(command): wire Mind reasoning into command processor

Feature flag JARVIS_USE_REMOTE_REASONING=true routes commands through
J-Prime POST /v1/reason. Plans executed via existing action handlers.
Falls through to local path when Mind unavailable."
```
