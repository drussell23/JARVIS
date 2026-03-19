# Unified Thinking Pipeline — Step 0 + Step 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the Mind-Body protocol between J-Prime and JARVIS, deploy the `/v1/reason/health` + `/v1/protocol/version` endpoints, then migrate the UnifiedBrainSelector to J-Prime with a MindClient on JARVIS that calls it (with shadow mode comparison).

**Architecture:** J-Prime gains a `reasoning/` module with Pydantic protocol schemas and a unified brain selector. JARVIS gains a `MindClient` that calls J-Prime for brain selection. Feature flag `JARVIS_USE_REMOTE_BRAIN_SELECTOR` controls cutover. Shadow mode logs divergence between local and remote selections.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI (J-Prime server), aiohttp (JARVIS client), pytest, YAML (brain_selection_policy.yaml)

**Spec:** `docs/superpowers/specs/2026-03-19-unified-thinking-pipeline-design.md`

**Repos touched:**
- `jarvis-prime` (J-Prime — Mind)
- `JARVIS-AI-Agent` (JARVIS — Body)

---

## File Structure

### J-Prime (new files)

| File | Responsibility |
|------|---------------|
| `jarvis_prime/reasoning/__init__.py` | Public exports for reasoning module |
| `jarvis_prime/reasoning/protocol.py` | Pydantic v2 models: ReasonRequest, ReasonResponse, ReasonFeedback, ProtocolVersion |
| `jarvis_prime/reasoning/endpoints.py` | FastAPI route handlers for /v1/reason/health, /v1/protocol/version |
| `jarvis_prime/reasoning/unified_brain_selector.py` | Merged brain selector (4-layer gate + intelligence overlay) |
| `tests/reasoning/__init__.py` | Test package |
| `tests/reasoning/test_protocol.py` | Schema validation + serialization roundtrip tests |
| `tests/reasoning/test_endpoints.py` | Endpoint integration tests |
| `tests/reasoning/test_brain_selector.py` | Brain selector unit tests |

### J-Prime (modified files)

| File | Change |
|------|--------|
| `jarvis_prime/server.py` | Register new reasoning routes inside `create_app()` |
| `jarvis_prime/core/hybrid_router.py` | BrainPolicyReader already exists (from earlier session) — no changes |

### JARVIS (new files)

| File | Responsibility |
|------|---------------|
| `backend/core/mind_client.py` | MindClient: calls J-Prime reasoning endpoints, manages tier state |
| `tests/core/test_mind_client.py` | MindClient unit tests with mock J-Prime |

### JARVIS (modified files)

| File | Change |
|------|--------|
| `backend/core/interactive_brain_router.py` | Add shadow mode hook: compare local selection with remote |
| `backend/api/unified_command_processor.py` | Wire MindClient for brain selection (behind feature flag) |

---

## Task 1: Protocol Schemas (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/__init__.py`
- Create: `jarvis_prime/reasoning/protocol.py`
- Test: `tests/reasoning/test_protocol.py`

- [ ] **Step 1: Create reasoning package**

```bash
# In jarvis-prime repo
mkdir -p jarvis_prime/reasoning tests/reasoning
touch jarvis_prime/reasoning/__init__.py tests/reasoning/__init__.py
```

- [ ] **Step 2: Write the failing test for protocol schemas**

File: `tests/reasoning/test_protocol.py`

```python
"""Tests for Mind-Body protocol v1.0.0 schemas."""
import json
import pytest
from jarvis_prime.reasoning.protocol import (
    ReasonRequest,
    ReasonResponse,
    ReasonFeedback,
    ProtocolVersionInfo,
    AuthEnvelope,
    FallbackPolicy,
    Constraints,
    Classification,
    SubGoal,
    Plan,
    RoutingTrace,
    ErrorDetail,
    StepResult,
    PROTOCOL_VERSION,
)


class TestReasonRequest:
    def test_minimal_request(self):
        req = ReasonRequest(
            request_id="req-001",
            session_id="sess-001",
            trace_id="trace-001",
            command="open Safari",
        )
        assert req.protocol_version == PROTOCOL_VERSION
        assert req.command == "open Safari"
        assert req.parent_request_id is None
        assert req.fallback_policy is not None  # has defaults

    def test_full_request_roundtrip(self):
        req = ReasonRequest(
            request_id="req-002",
            session_id="sess-001",
            trace_id="trace-001",
            parent_request_id="req-001",
            command="research competitors",
            context={"speaker": "Derek", "active_app": "Safari"},
            constraints=Constraints(
                deadline_ms=30000,
                deadline_at_ms=1710876543210,
                hard_cost_cap_usd=0.10,
                soft_cost_target_usd=0.03,
            ),
            auth=AuthEnvelope(
                token_id="unsigned",
                signature="none",
                nonce="nonce-001",
                issued_at="2026-03-19T12:00:00Z",
            ),
        )
        data = req.model_dump(mode="json")
        restored = ReasonRequest.model_validate(data)
        assert restored.request_id == req.request_id
        assert restored.constraints.hard_cost_cap_usd == 0.10

    def test_request_json_serializable(self):
        req = ReasonRequest(
            request_id="req-003",
            session_id="sess-001",
            trace_id="trace-001",
            command="hello",
        )
        text = req.model_dump_json()
        assert isinstance(text, str)
        parsed = json.loads(text)
        assert parsed["command"] == "hello"


class TestReasonResponse:
    def test_minimal_response(self):
        resp = ReasonResponse(
            request_id="req-001",
            session_id="sess-001",
            trace_id="trace-001",
            status="plan_ready",
            served_mode="LEVEL_0_PRIMARY",
        )
        assert resp.protocol_version == PROTOCOL_VERSION
        assert resp.plan is None
        assert resp.error is None

    def test_response_with_plan(self):
        resp = ReasonResponse(
            request_id="req-001",
            session_id="sess-001",
            trace_id="trace-001",
            status="plan_ready",
            served_mode="LEVEL_0_PRIMARY",
            classification=Classification(
                intent="browser_navigation",
                complexity="light",
                confidence=0.92,
                brain_used="qwen_coder",
                graph_depth="fast",
            ),
            plan=Plan(
                plan_id="plan-001",
                plan_hash="abc123",
                sub_goals=[
                    SubGoal(
                        step_id="s1",
                        action_id="act-s1",
                        goal="open Safari",
                        task_type="system_command",
                        brain_assigned="phi3_lightweight",
                        tool_required="app_control",
                    )
                ],
            ),
        )
        assert len(resp.plan.sub_goals) == 1
        assert resp.classification.confidence == 0.92


class TestReasonFeedback:
    def test_feedback_roundtrip(self):
        fb = ReasonFeedback(
            request_id="req-001",
            session_id="sess-001",
            trace_id="trace-001",
            plan_id="plan-001",
            plan_hash="abc123",
            step_results=[
                StepResult(
                    step_id="s1",
                    action_id="act-s1",
                    success=True,
                    output="Safari opened",
                    latency_ms=1200.0,
                    tool_used="app_control",
                )
            ],
            final_outcome="success",
        )
        data = fb.model_dump(mode="json")
        restored = ReasonFeedback.model_validate(data)
        assert restored.step_results[0].success is True
        assert restored.final_outcome == "success"


class TestProtocolVersionInfo:
    def test_version_info(self):
        info = ProtocolVersionInfo(
            features=["brain_selection", "langgraph_reasoning"],
            brain_policy_hash="sha256abc",
        )
        assert info.current_version == PROTOCOL_VERSION
        assert "brain_selection" in info.features


class TestErrorDetail:
    def test_error_codes(self):
        err = ErrorDetail(
            code="MIND_TIMEOUT",
            error_class="transient",
            message="Deadline exceeded",
            retry_after_ms=5000,
            recovery_strategy="FALLBACK_TIER1",
        )
        assert err.error_class == "transient"
        assert err.retry_after_ms == 5000
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_protocol.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis_prime.reasoning.protocol'`

- [ ] **Step 4: Implement protocol schemas**

File: `jarvis_prime/reasoning/protocol.py`

```python
"""
Mind-Body Protocol v1.0.0 — Pydantic schemas for JARVIS <-> J-Prime communication.

Spec: docs/superpowers/specs/2026-03-19-unified-thinking-pipeline-design.md
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.0.0"
MIN_SUPPORTED_VERSION = "1.0.0"
MAX_SUPPORTED_VERSION = "1.0.999"


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------

class AuthEnvelope(BaseModel):
    token_id: str = "unsigned"
    signature: str = "none"
    nonce: str = ""
    issued_at: str = ""


class FallbackPolicy(BaseModel):
    allow_tier1: bool = True
    allow_tier2: bool = True
    queue_on_fail: bool = True
    require_approval_in_degraded: bool = True


class Constraints(BaseModel):
    deadline_ms: Optional[int] = None
    deadline_at_ms: Optional[int] = None
    hard_cost_cap_usd: Optional[float] = None
    soft_cost_target_usd: Optional[float] = None
    session_budget_remaining_usd: Optional[float] = None
    allowed_task_classes: List[str] = Field(
        default_factory=lambda: ["tier0", "tier1", "tier2", "tier3"]
    )


class Classification(BaseModel):
    intent: str = ""
    complexity: str = ""  # trivial | light | heavy | complex
    confidence: float = 0.0
    brain_used: str = ""
    graph_depth: str = ""  # fast | standard | full


class SubGoal(BaseModel):
    step_id: str
    action_id: str = ""
    goal: str = ""
    task_type: str = ""
    brain_assigned: str = ""
    tool_required: str = ""
    depends_on: List[str] = Field(default_factory=list)
    estimated_ms: int = 0


class Plan(BaseModel):
    plan_id: str = ""
    plan_hash: str = ""
    sub_goals: List[SubGoal] = Field(default_factory=list)
    execution_strategy: str = "sequential"
    total_estimated_ms: int = 0
    risk_level: str = "low"
    approval_required: bool = False
    approval_reason_codes: List[str] = Field(default_factory=list)
    approval_scope: str = "plan"


class RoutingTrace(BaseModel):
    analysis_brain: str = ""
    planning_brain: str = ""
    cost_gate_passed: bool = True
    resource_gate_passed: bool = True
    sai_health: float = 1.0
    cai_confidence: float = 0.0


class ErrorDetail(BaseModel):
    code: str = "NONE"
    error_class: str = Field("transient", alias="class")  # transient | permanent | policy
    message: str = ""
    retry_after_ms: Optional[int] = None
    recovery_strategy: str = "RETRY_SHORT"

    model_config = {"populate_by_name": True}  # accept both "class" and "error_class"


class StepResult(BaseModel):
    step_id: str
    action_id: str = ""
    success: bool = False
    output: str = ""
    latency_ms: float = 0.0
    tool_used: str = ""
    artifact_refs: List[str] = Field(default_factory=list)
    side_effects_committed: bool = False


# ---------------------------------------------------------------------------
# Top-level request/response models
# ---------------------------------------------------------------------------

class ReasonRequest(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    request_id: str
    idempotency_scope: str = "session"
    session_id: str
    trace_id: str
    parent_request_id: Optional[str] = None
    auth: AuthEnvelope = Field(default_factory=AuthEnvelope)
    command: str
    context: Dict[str, Any] = Field(default_factory=dict)
    constraints: Constraints = Field(default_factory=Constraints)
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)


class ReasonResponse(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    request_id: str
    session_id: str
    trace_id: str = ""
    status: str  # plan_ready | needs_approval | queued | error
    served_mode: str  # LEVEL_0_PRIMARY | LEVEL_1_DEGRADED | LEVEL_2_REFLEX
    requested_mode: str = "LEVEL_0_PRIMARY"
    degraded_reason_code: Optional[str] = None
    classification: Optional[Classification] = None
    plan: Optional[Plan] = None
    routing_trace: Optional[RoutingTrace] = None
    error: Optional[ErrorDetail] = None


class ReasonFeedback(BaseModel):
    request_id: str
    session_id: str
    trace_id: str = ""
    plan_id: str
    plan_hash: str
    step_results: List[StepResult] = Field(default_factory=list)
    final_outcome: str = "success"  # success | partial_success | failure
    replay_token: Optional[str] = None
    original_enqueued_at: Optional[str] = None
    replay_attempt: int = 0
    max_replay_attempts: int = 3


class ProtocolVersionInfo(BaseModel):
    current_version: str = PROTOCOL_VERSION
    min_supported_version: str = MIN_SUPPORTED_VERSION
    max_supported_version: str = MAX_SUPPORTED_VERSION
    features: List[str] = Field(default_factory=list)
    brain_policy_hash: str = ""
```

- [ ] **Step 5: Export from __init__.py**

File: `jarvis_prime/reasoning/__init__.py`

```python
"""Reasoning module — the Mind of the Trinity."""
from jarvis_prime.reasoning.protocol import (  # noqa: F401
    PROTOCOL_VERSION,
    AuthEnvelope,
    Classification,
    Constraints,
    ErrorDetail,
    FallbackPolicy,
    Plan,
    ProtocolVersionInfo,
    ReasonFeedback,
    ReasonRequest,
    ReasonResponse,
    RoutingTrace,
    StepResult,
    SubGoal,
)
```

- [ ] **Step 6: Run tests — verify they pass**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_protocol.py -v
```
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/ tests/reasoning/
git commit -m "feat(reasoning): add Mind-Body protocol v1.0.0 Pydantic schemas

ReasonRequest, ReasonResponse, ReasonFeedback, ProtocolVersionInfo
with full field set from unified-thinking-pipeline spec."
```

---

## Task 2: Health + Version Endpoints (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/endpoints.py`
- Modify: `jarvis_prime/server.py` (register routes)
- Test: `tests/reasoning/test_endpoints.py`

- [ ] **Step 1: Write the failing test**

File: `tests/reasoning/test_endpoints.py`

```python
"""Tests for /v1/reason/health and /v1/protocol/version endpoints."""
import pytest
from unittest.mock import patch

from jarvis_prime.reasoning.endpoints import get_reason_health, get_protocol_version
from jarvis_prime.reasoning.protocol import PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_protocol_version_returns_current():
    result = await get_protocol_version()
    assert result["current_version"] == PROTOCOL_VERSION
    assert "min_supported_version" in result
    assert "max_supported_version" in result
    assert isinstance(result["features"], list)
    assert "brain_selection" in result["features"]


@pytest.mark.asyncio
async def test_reason_health_basic():
    result = await get_reason_health()
    assert "status" in result
    assert result["status"] in ("ready", "starting", "degraded")
    assert "protocol_version" in result
    assert "brains_loaded" in result
    assert isinstance(result["brains_loaded"], list)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_endpoints.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis_prime.reasoning.endpoints'`

- [ ] **Step 3: Implement endpoints**

File: `jarvis_prime/reasoning/endpoints.py`

```python
"""
Reasoning endpoints — /v1/reason/health, /v1/protocol/version.

These are pure async functions. The FastAPI route registration happens in
server.py to follow the existing pattern.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from jarvis_prime.reasoning.protocol import (
    PROTOCOL_VERSION,
    MIN_SUPPORTED_VERSION,
    MAX_SUPPORTED_VERSION,
)

logger = logging.getLogger("reasoning.endpoints")

_POLICY_PATH = Path(__file__).parent.parent / "core" / "hybrid_router.py"
_JARVIS_POLICY_PATHS = [
    Path(os.getenv("JARVIS_BRAIN_POLICY_PATH", "__missing__")),
    Path(os.getenv("JARVIS_REPO_PATH", ".")) / "backend" / "core" / "ouroboros" / "governance" / "brain_selection_policy.yaml",
    Path(__file__).parent.parent.parent.parent / "JARVIS-AI-Agent" / "backend" / "core" / "ouroboros" / "governance" / "brain_selection_policy.yaml",
]


def _compute_policy_hash() -> str:
    """SHA-256 of brain_selection_policy.yaml for drift detection."""
    for p in _JARVIS_POLICY_PATHS:
        if p.exists():
            try:
                return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            except Exception:
                pass
    return "unavailable"


def _get_loaded_brains() -> List[str]:
    """Query which brains are currently loaded on this J-Prime instance."""
    try:
        from jarvis_prime.core.hybrid_router import get_brain_policy_reader
        reader = get_brain_policy_reader()
        return list(reader.get_task_complexity_map().keys())[:20]
    except Exception:
        return []


async def get_protocol_version() -> Dict[str, Any]:
    """Handler for GET /v1/protocol/version."""
    return {
        "current_version": PROTOCOL_VERSION,
        "min_supported_version": MIN_SUPPORTED_VERSION,
        "max_supported_version": MAX_SUPPORTED_VERSION,
        "features": [
            "brain_selection",
            "health_check",
        ],
        "brain_policy_hash": _compute_policy_hash(),
    }


async def get_reason_health() -> Dict[str, Any]:
    """Handler for GET /v1/reason/health."""
    brains = _get_loaded_brains()
    status = "ready" if brains else "starting"

    return {
        "status": status,
        "protocol_version": PROTOCOL_VERSION,
        "brains_loaded": brains,
        "brain_policy_hash": _compute_policy_hash(),
        "reasoning_graph_ready": False,  # Step 2 will set this to True
    }
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_endpoints.py -v
```
Expected: ALL PASS

- [ ] **Step 5: Register routes in server.py**

Find the section in `jarvis_prime/server.py` where endpoints are registered (near line 1492 where `@app.get("/health")` is defined). Add after the existing `/v1/capability` endpoint block:

```python
        # v295.0: Reasoning endpoints (Mind-Body protocol)
        @app.get("/v1/reason/health")
        async def reason_health():
            """Mind health + loaded brains + graph readiness."""
            from jarvis_prime.reasoning.endpoints import get_reason_health
            return await get_reason_health()

        @app.get("/v1/protocol/version")
        async def protocol_version():
            """Protocol version negotiation + feature flags."""
            from jarvis_prime.reasoning.endpoints import get_protocol_version
            return await get_protocol_version()
```

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/endpoints.py jarvis_prime/server.py tests/reasoning/test_endpoints.py
git commit -m "feat(reasoning): add /v1/reason/health + /v1/protocol/version endpoints

Step 0 of unified thinking pipeline migration. Health endpoint reports
loaded brains and policy hash. Version endpoint enables boot-gate
compatibility checks."
```

---

## Task 3: Unified Brain Selector (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/unified_brain_selector.py`
- Test: `tests/reasoning/test_brain_selector.py`

- [ ] **Step 1: Write the failing test**

File: `tests/reasoning/test_brain_selector.py`

```python
"""Tests for UnifiedBrainSelector — 4-layer gate logic."""
import pytest
from jarvis_prime.reasoning.unified_brain_selector import (
    UnifiedBrainSelector,
    UnifiedBrainSelection,
)


@pytest.fixture
def selector():
    """Create a selector. Works even without brain_selection_policy.yaml."""
    return UnifiedBrainSelector()


class TestComplexityMapping:
    def test_trivial_task(self, selector):
        sel = selector.select("system_command", command="open Safari")
        assert sel.complexity in ("trivial", "light")
        assert sel.brain_id is not None

    def test_light_task(self, selector):
        sel = selector.select("classification", command="classify this")
        assert sel.complexity == "light"

    def test_heavy_task(self, selector):
        sel = selector.select("vision_action", command="click the button")
        assert sel.complexity == "heavy"

    def test_complex_task(self, selector):
        sel = selector.select("complex_reasoning", command="analyze this pattern")
        assert sel.complexity == "complex"

    def test_unknown_task_defaults_to_light(self, selector):
        sel = selector.select("unknown_task_type", command="do something")
        assert sel.complexity == "light"


class TestBrainSelection:
    def test_returns_brain_id(self, selector):
        sel = selector.select("classification")
        assert isinstance(sel.brain_id, str)
        assert len(sel.brain_id) > 0

    def test_returns_model_name(self, selector):
        sel = selector.select("classification")
        assert isinstance(sel.model_name, str)

    def test_returns_fallback_chain(self, selector):
        sel = selector.select("classification")
        assert isinstance(sel.fallback_chain, list)

    def test_returns_routing_reason(self, selector):
        sel = selector.select("classification")
        assert isinstance(sel.routing_reason, str)
        assert len(sel.routing_reason) > 0

    def test_claude_fallback_model(self, selector):
        sel = selector.select("complex_reasoning")
        assert sel.claude_fallback is not None
        assert "claude" in sel.claude_fallback


class TestKeywordEscalation:
    def test_complex_keywords_escalate(self, selector):
        sel = selector.select("classification", command="analyze the root cause of this trend")
        assert sel.complexity == "complex"

    def test_trivial_keywords_deescalate(self, selector):
        sel = selector.select("classification", command="what time is it")
        assert sel.complexity == "trivial"


class TestCostGate:
    def test_cost_gate_default_passes(self, selector):
        sel = selector.select("classification")
        assert sel.cost_gate_passed is True

    def test_record_cost_updates_daily_spend(self, selector):
        selector.record_cost("gcp_prime", 0.01)
        assert selector.daily_spend_gcp >= 0.01


class TestGraphDepthMapping:
    def test_trivial_is_fast(self, selector):
        sel = selector.select("system_command", command="open Safari")
        assert sel.graph_depth == "fast"

    def test_heavy_is_standard(self, selector):
        sel = selector.select("browser_navigation", command="navigate to linkedin")
        assert sel.graph_depth == "standard"

    def test_complex_is_full(self, selector):
        sel = selector.select("complex_reasoning", command="analyze this architecture")
        assert sel.graph_depth == "full"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_brain_selector.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement UnifiedBrainSelector**

File: `jarvis_prime/reasoning/unified_brain_selector.py`

```python
"""
Unified Brain Selector — 4-layer gate for the ONE thinking pipeline.

Merges:
- BrainSelector (Ouroboros, code gen) — Task + Resource + Cost gates
- InteractiveBrainRouter (voice) — task_type → complexity mapping
- RouteDecisionService (CAI+SAI+UAE) — intelligence overlay

Called at every LangGraph node that needs inference.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("reasoning.brain_selector")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnifiedBrainSelection:
    brain_id: str
    model_name: str
    complexity: str            # trivial | light | heavy | complex
    graph_depth: str           # fast | standard | full
    routing_reason: str
    fallback_chain: List[str]
    claude_fallback: str
    cost_gate_passed: bool = True
    resource_gate_passed: bool = True
    vision_model: Optional[str] = None


# ---------------------------------------------------------------------------
# Task type → complexity (from InteractiveBrainRouter, moved to J-Prime)
# ---------------------------------------------------------------------------

_TASK_COMPLEXITY: Dict[str, str] = {
    # Trivial
    "workspace_fastpath": "trivial",
    "system_command": "trivial",
    "reflex_match": "trivial",
    # Light
    "classification": "light",
    "step_decomposition": "light",
    "email_triage": "light",
    "calendar_query": "light",
    "goal_chain_step": "light",
    # Heavy
    "vision_action": "heavy",
    "vision_verification": "heavy",
    "screen_observation": "heavy",
    "proactive_narration": "heavy",
    "email_compose": "heavy",
    "browser_navigation": "heavy",
    # Complex
    "multi_step_planning": "complex",
    "email_summarization": "complex",
    "complex_reasoning": "complex",
}

# Complexity → default brain
_DEFAULT_BRAIN: Dict[str, str] = {
    "trivial": "phi3_lightweight",
    "light": "qwen_coder",
    "heavy": "qwen_coder",
    "complex": "qwen_coder_32b",
}

# Complexity → default model name
_DEFAULT_MODEL: Dict[str, str] = {
    "trivial": "llama-3.2-1b",
    "light": "qwen-2.5-coder-7b",
    "heavy": "qwen-2.5-coder-7b",
    "complex": "qwen-2.5-coder-32b",
}

# Complexity → Claude fallback
_CLAUDE_FALLBACK: Dict[str, str] = {
    "trivial": "claude-haiku-4-5-20251001",
    "light": "claude-sonnet-4-20250514",
    "heavy": "claude-sonnet-4-20250514",
    "complex": "claude-sonnet-4-20250514",
}

# Complexity → graph depth
_GRAPH_DEPTH: Dict[str, str] = {
    "trivial": "fast",
    "light": "fast",
    "heavy": "standard",
    "complex": "full",
}

# Fallback chains
_FALLBACK_CHAINS: Dict[str, List[str]] = {
    "phi3_lightweight": ["qwen_coder", "mistral_7b_fallback"],
    "qwen_coder": ["qwen_coder_14b", "mistral_7b_fallback"],
    "qwen_coder_14b": ["qwen_coder", "mistral_7b_fallback"],
    "qwen_coder_32b": ["qwen_coder_14b", "qwen_coder", "mistral_7b_fallback"],
    "deepseek_r1": ["qwen_coder_32b", "qwen_coder", "mistral_7b_fallback"],
}

# Vision task types
_VISION_TASKS = {"vision_action", "vision_verification", "screen_observation", "proactive_narration"}

# Keyword escalation
_COMPLEX_RE = re.compile(
    r"\b(analyze|summarize|compare|evaluate|investigate|explain why|root cause"
    r"|trend|pattern|insight|strategic)\b",
    re.IGNORECASE,
)
_TRIVIAL_RE = re.compile(
    r"\b(open|close|lock|unlock|volume|brightness|screenshot|timer"
    r"|what time|weather)\b",
    re.IGNORECASE,
)


class UnifiedBrainSelector:
    """4-layer gate brain selector for the ONE thinking pipeline.

    Layer 1 — Intent Gate: task_type → complexity
    Layer 2 — Keyword Gate: command keywords escalate/de-escalate
    Layer 3 — Resource Gate: GPU pressure → downgrade (placeholder)
    Layer 4 — Cost Gate: daily budget → queue if exceeded
    """

    def __init__(self) -> None:
        self._daily_spend_gcp: float = 0.0
        self._daily_spend_claude: float = 0.0
        self._cost_date: str = ""
        self._daily_budget_gcp = float(os.getenv("JARVIS_DAILY_BUDGET_GCP", "5.0"))
        self._daily_budget_claude = float(os.getenv("JARVIS_DAILY_BUDGET_CLAUDE", "2.0"))
        self._vision_model = os.getenv("JARVIS_VISION_MODEL_NAME", "llava-v1.5-7b")

        # Try to load from policy YAML (hot-reload via BrainPolicyReader)
        self._policy_reader = None
        try:
            from jarvis_prime.core.hybrid_router import get_brain_policy_reader
            self._policy_reader = get_brain_policy_reader()
        except Exception:
            pass

    @property
    def daily_spend_gcp(self) -> float:
        self._maybe_reset_daily()
        return self._daily_spend_gcp

    def select(
        self,
        task_type: str,
        command: str = "",
    ) -> UnifiedBrainSelection:
        """Select the optimal brain for a task.

        Args:
            task_type: One of the keys in _TASK_COMPLEXITY.
            command: User command text for keyword escalation.
        """
        # Layer 1: Intent Gate — task_type → base complexity
        complexity = _TASK_COMPLEXITY.get(task_type, "light")

        # Layer 2: Keyword Gate — escalate/de-escalate
        if command:
            if _COMPLEX_RE.search(command):
                complexity = "complex"
            elif _TRIVIAL_RE.search(command) and complexity == "light":
                complexity = "trivial"

        # Layer 3: Resource Gate — placeholder (GPU monitoring in future step)
        resource_gate_passed = True

        # Layer 4: Cost Gate
        self._maybe_reset_daily()
        cost_gate_passed = self._daily_spend_gcp < self._daily_budget_gcp

        # Select brain
        brain_id = _DEFAULT_BRAIN.get(complexity, "qwen_coder")
        model_name = _DEFAULT_MODEL.get(complexity, "qwen-2.5-coder-7b")
        graph_depth = _GRAPH_DEPTH.get(complexity, "fast")
        claude_fallback = _CLAUDE_FALLBACK.get(complexity, "claude-sonnet-4-20250514")
        fallback_chain = _FALLBACK_CHAINS.get(brain_id, [])
        vision_model = self._vision_model if task_type in _VISION_TASKS else None

        reason = f"{task_type}→{complexity}→{brain_id}"

        return UnifiedBrainSelection(
            brain_id=brain_id,
            model_name=model_name,
            complexity=complexity,
            graph_depth=graph_depth,
            routing_reason=reason,
            fallback_chain=fallback_chain,
            claude_fallback=claude_fallback,
            cost_gate_passed=cost_gate_passed,
            resource_gate_passed=resource_gate_passed,
            vision_model=vision_model,
        )

    def record_cost(self, provider: str, usd: float) -> None:
        """Record cost for daily budget tracking."""
        self._maybe_reset_daily()
        if provider == "gcp_prime":
            self._daily_spend_gcp += usd
        elif provider == "claude_api":
            self._daily_spend_claude += usd

    def _maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._cost_date:
            self._daily_spend_gcp = 0.0
            self._daily_spend_claude = 0.0
            self._cost_date = today
```

- [ ] **Step 4: Update reasoning __init__.py exports**

Add to `jarvis_prime/reasoning/__init__.py`:

```python
from jarvis_prime.reasoning.unified_brain_selector import (  # noqa: F401
    UnifiedBrainSelection,
    UnifiedBrainSelector,
)
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_brain_selector.py -v
```
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/ tests/reasoning/test_brain_selector.py
git commit -m "feat(reasoning): add UnifiedBrainSelector with 4-layer gate

Merges InteractiveBrainRouter task_type mapping + keyword escalation
+ cost gate + fallback chains. Graph depth mapping for LangGraph
pipeline (fast/standard/full). Vision model selection for screen tasks."
```

---

## Task 4: Brain Selection Endpoint (J-Prime)

**Files:**
- Modify: `jarvis_prime/reasoning/endpoints.py`
- Modify: `jarvis_prime/server.py`
- Modify: `tests/reasoning/test_endpoints.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/reasoning/test_endpoints.py`:

```python
from jarvis_prime.reasoning.protocol import ReasonRequest, ReasonResponse


@pytest.mark.asyncio
async def test_reason_brain_select():
    """Test the /v1/reason/select endpoint (Step 1: brain selection only)."""
    from jarvis_prime.reasoning.endpoints import handle_brain_select

    req = ReasonRequest(
        request_id="req-test-001",
        session_id="sess-test",
        trace_id="trace-test",
        command="open Safari",
        context={"speaker": "Derek"},
    )
    resp = await handle_brain_select(req)
    assert resp["status"] == "plan_ready"
    assert resp["served_mode"] == "LEVEL_0_PRIMARY"
    assert resp["classification"]["brain_used"] != ""
    assert resp["classification"]["complexity"] in ("trivial", "light", "heavy", "complex")


@pytest.mark.asyncio
async def test_reason_brain_select_complex():
    from jarvis_prime.reasoning.endpoints import handle_brain_select

    req = ReasonRequest(
        request_id="req-test-002",
        session_id="sess-test",
        trace_id="trace-test",
        command="analyze the root cause of our competitor's pricing strategy",
    )
    resp = await handle_brain_select(req)
    assert resp["classification"]["complexity"] == "complex"
    assert resp["classification"]["graph_depth"] == "full"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_endpoints.py::test_reason_brain_select -v
```
Expected: FAIL with `ImportError: cannot import name 'handle_brain_select'`

- [ ] **Step 3: Implement brain select endpoint**

Add to `jarvis_prime/reasoning/endpoints.py`:

```python
from jarvis_prime.reasoning.protocol import (
    ReasonRequest,
    ReasonResponse,
    Classification,
    RoutingTrace,
)
from jarvis_prime.reasoning.unified_brain_selector import UnifiedBrainSelector

# Module-level singleton
_brain_selector: Optional[UnifiedBrainSelector] = None


def _get_brain_selector() -> UnifiedBrainSelector:
    global _brain_selector
    if _brain_selector is None:
        _brain_selector = UnifiedBrainSelector()
    return _brain_selector


async def handle_brain_select(req: ReasonRequest) -> dict:
    """Handle brain selection request (Step 1 of migration).

    Returns a ReasonResponse-shaped dict with classification filled in.
    Plan is empty — Body uses classification to select brain locally.
    """
    selector = _get_brain_selector()

    # Infer task_type from command context or default to classification
    task_type = req.context.get("task_type", "classification")
    selection = selector.select(task_type=task_type, command=req.command)

    resp = ReasonResponse(
        request_id=req.request_id,
        session_id=req.session_id,
        trace_id=req.trace_id,
        status="plan_ready",
        served_mode="LEVEL_0_PRIMARY",
        classification=Classification(
            intent=task_type,
            complexity=selection.complexity,
            confidence=0.95 if selection.cost_gate_passed else 0.5,
            brain_used=selection.brain_id,
            graph_depth=selection.graph_depth,
        ),
        routing_trace=RoutingTrace(
            analysis_brain=selection.brain_id,
            cost_gate_passed=selection.cost_gate_passed,
            resource_gate_passed=selection.resource_gate_passed,
        ),
    )
    return resp.model_dump(mode="json")
```

- [ ] **Step 4: Register route in server.py**

Add alongside the other reasoning routes in `jarvis_prime/server.py`:

```python
        # v295.0: Brain selection endpoint (Step 1 migration)
        @app.post("/v1/reason/select")
        async def reason_select(request: Request):
            """Brain selection via unified selector."""
            from jarvis_prime.reasoning.endpoints import handle_brain_select
            from jarvis_prime.reasoning.protocol import ReasonRequest
            body = await request.json()
            req = ReasonRequest.model_validate(body)
            return await handle_brain_select(req)
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd ~/Documents/repos/jarvis-prime
python3 -m pytest tests/reasoning/test_endpoints.py -v
```
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/repos/jarvis-prime
git add jarvis_prime/reasoning/ jarvis_prime/server.py tests/reasoning/
git commit -m "feat(reasoning): add /v1/reason/select brain selection endpoint

Step 1 of migration: JARVIS can call J-Prime for brain selection.
Returns ReasonResponse with classification (brain_id, complexity,
graph_depth) from UnifiedBrainSelector 4-layer gate."
```

---

## Task 5: MindClient on JARVIS

**Files:**
- Create: `backend/core/mind_client.py`
- Test: `tests/core/test_mind_client.py`

- [ ] **Step 1: Write the failing test**

File: `tests/core/test_mind_client.py`

```python
"""Tests for MindClient — JARVIS's connection to the J-Prime Mind."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from backend.core.mind_client import MindClient, OperationalLevel


@pytest.fixture
def client():
    return MindClient(
        mind_host="127.0.0.1",
        mind_port=8000,
    )


class TestOperationalLevels:
    def test_starts_at_level_0(self, client):
        assert client.current_level == OperationalLevel.LEVEL_0

    def test_degrade_to_level_1(self, client):
        for _ in range(3):  # 3 consecutive failures
            client._record_failure()
        assert client.current_level == OperationalLevel.LEVEL_1

    def test_degrade_to_level_2(self, client):
        for _ in range(3):
            client._record_failure()
        assert client.current_level == OperationalLevel.LEVEL_1
        client._record_claude_failure()
        assert client.current_level == OperationalLevel.LEVEL_2

    def test_recovery_requires_hysteresis(self, client):
        # Degrade
        for _ in range(3):
            client._record_failure()
        assert client.current_level == OperationalLevel.LEVEL_1
        # One success is not enough
        client._record_success()
        assert client.current_level == OperationalLevel.LEVEL_1
        # Three consecutive successes required
        client._record_success()
        client._record_success()
        assert client.current_level == OperationalLevel.LEVEL_0


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_check_health_success(self, client):
        mock_response = {
            "status": "ready",
            "protocol_version": "1.0.0",
            "brains_loaded": ["qwen_coder"],
        }
        with patch.object(client, "_http_get", new_callable=AsyncMock, return_value=mock_response):
            result = await client.check_health()
            assert result["status"] == "ready"

    @pytest.mark.asyncio
    async def test_check_health_failure_degrades(self, client):
        with patch.object(client, "_http_get", new_callable=AsyncMock, side_effect=Exception("unreachable")):
            for _ in range(3):
                try:
                    await client.check_health()
                except Exception:
                    pass
        assert client.current_level == OperationalLevel.LEVEL_1


class TestBrainSelect:
    @pytest.mark.asyncio
    async def test_select_brain_returns_classification(self, client):
        mock_resp = {
            "request_id": "req-001",
            "session_id": "sess-001",
            "trace_id": "trace-001",
            "status": "plan_ready",
            "served_mode": "LEVEL_0_PRIMARY",
            "classification": {
                "intent": "classification",
                "complexity": "light",
                "confidence": 0.95,
                "brain_used": "qwen_coder",
                "graph_depth": "fast",
            },
        }
        with patch.object(client, "_http_post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.select_brain(
                command="check email",
                task_type="classification",
                context={"speaker": "Derek"},
            )
            assert result["classification"]["brain_used"] == "qwen_coder"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/core/test_mind_client.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement MindClient**

File: `backend/core/mind_client.py`

```python
"""
MindClient — JARVIS's connection to the J-Prime Mind.

Manages:
- HTTP calls to /v1/reason/* endpoints
- Operational level state machine (Level 0/1/2)
- Hysteresis for level transitions
- Circuit breaker for the Mind-Body link
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("MindClient")


class OperationalLevel(Enum):
    LEVEL_0 = "LEVEL_0_PRIMARY"       # J-Prime full pipeline
    LEVEL_1 = "LEVEL_1_DEGRADED"      # Claude API emergency planner
    LEVEL_2 = "LEVEL_2_REFLEX"        # Reflex only + queue


class MindClient:
    """JARVIS Body's connection to the J-Prime Mind.

    Manages operational level transitions with hysteresis, circuit breaker,
    and HTTP communication with /v1/reason/* endpoints.
    """

    FAILURE_THRESHOLD = 3        # consecutive failures before degrading
    RECOVERY_THRESHOLD = 3       # consecutive successes before recovering
    CIRCUIT_COOLDOWN_S = 30.0    # seconds before retrying after circuit open

    def __init__(
        self,
        mind_host: Optional[str] = None,
        mind_port: Optional[int] = None,
    ) -> None:
        self._host = mind_host or os.getenv("JARVIS_PRIME_HOST", "136.113.252.164")
        self._port = mind_port or int(os.getenv("JARVIS_PRIME_PORT", "8000"))
        self._base_url = f"http://{self._host}:{self._port}"

        # Level state machine
        self._level = OperationalLevel.LEVEL_0
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_failure_time = 0.0

        # Session tracking
        self._session_id = str(uuid.uuid4())[:12]

        # HTTP session (lazy)
        self._session = None

    @property
    def current_level(self) -> OperationalLevel:
        return self._level

    # ----- Level transitions -----

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        self._consecutive_successes = 0
        self._last_failure_time = time.monotonic()

        if self._consecutive_failures >= self.FAILURE_THRESHOLD:
            if self._level == OperationalLevel.LEVEL_0:
                self._level = OperationalLevel.LEVEL_1
                logger.warning("[MindClient] Degraded to LEVEL_1 after %d failures", self._consecutive_failures)

    def _record_claude_failure(self) -> None:
        """Claude API also failed — degrade to Level 2."""
        if self._level == OperationalLevel.LEVEL_1:
            self._level = OperationalLevel.LEVEL_2
            logger.warning("[MindClient] Degraded to LEVEL_2 — Claude API also unavailable")

    def _record_success(self) -> None:
        self._consecutive_successes += 1
        self._consecutive_failures = 0

        if self._consecutive_successes >= self.RECOVERY_THRESHOLD:
            if self._level != OperationalLevel.LEVEL_0:
                old = self._level
                self._level = OperationalLevel.LEVEL_0
                self._consecutive_successes = 0
                logger.info("[MindClient] Recovered to LEVEL_0 from %s", old.value)

    # ----- HTTP helpers -----

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _http_get(self, path: str, timeout: float = 10.0) -> Dict[str, Any]:
        import aiohttp
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Mind returned {resp.status}: {await resp.text()}")
            return await resp.json()

    async def _http_post(self, path: str, data: Dict, timeout: float = 30.0) -> Dict[str, Any]:
        import aiohttp
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Mind returned {resp.status}: {await resp.text()}")
            return await resp.json()

    # ----- Public API -----

    async def check_health(self) -> Dict[str, Any]:
        """Check Mind health. Updates level state on failure."""
        try:
            result = await self._http_get("/v1/reason/health", timeout=5.0)
            self._record_success()
            return result
        except Exception as exc:
            self._record_failure()
            raise

    async def check_protocol_version(self) -> Dict[str, Any]:
        """Check protocol version compatibility (boot gate)."""
        return await self._http_get("/v1/protocol/version", timeout=5.0)

    async def select_brain(
        self,
        command: str,
        task_type: str = "classification",
        context: Optional[Dict[str, Any]] = None,
        deadline_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Call J-Prime for brain selection (Step 1 migration).

        Returns ReasonResponse dict with classification.
        On failure, degrades level and returns None.
        """
        if self._level == OperationalLevel.LEVEL_2:
            return None  # Level 2: no thinking

        request_id = str(uuid.uuid4())[:12]
        trace_id = str(uuid.uuid4())[:12]

        payload = {
            "protocol_version": "1.0.0",
            "request_id": request_id,
            "session_id": self._session_id,
            "trace_id": trace_id,
            "command": command,
            "context": {**(context or {}), "task_type": task_type},
        }

        if deadline_ms:
            payload["constraints"] = {"deadline_ms": deadline_ms}

        try:
            result = await self._http_post(
                "/v1/reason/select",
                data=payload,
                timeout=float(os.getenv("JARVIS_MIND_TIMEOUT_S", "10")),
            )
            self._record_success()
            return result
        except Exception as exc:
            logger.warning("[MindClient] Brain selection failed: %s", exc)
            self._record_failure()
            return None

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_mind_client: Optional[MindClient] = None


def get_mind_client() -> MindClient:
    """Get the global MindClient singleton."""
    global _mind_client
    if _mind_client is None:
        _mind_client = MindClient()
    return _mind_client
```

- [ ] **Step 4: Create test directory if needed**

```bash
mkdir -p tests/core && touch tests/__init__.py tests/core/__init__.py
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/core/test_mind_client.py -v
```
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
git add backend/core/mind_client.py tests/core/test_mind_client.py tests/__init__.py tests/core/__init__.py
git commit -m "feat(mind): add MindClient with operational level state machine

Connects JARVIS Body to J-Prime Mind via /v1/reason/* endpoints.
Level 0/1/2 with hysteresis (3 failures to degrade, 3 successes to
recover). Singleton via get_mind_client()."
```

---

## Task 6: Shadow Mode Wiring (JARVIS)

**Files:**
- Modify: `backend/core/interactive_brain_router.py`
- Modify: `backend/api/unified_command_processor.py`

- [ ] **Step 1: Add shadow comparison to InteractiveBrainRouter**

Add a `compare_with_remote` method to `InteractiveBrainRouter` in `backend/core/interactive_brain_router.py`, after the `get_claude_model` method:

```python
    async def compare_with_remote(
        self,
        task_type: str,
        command: str,
        remote_classification: dict,
    ) -> Optional[dict]:
        """Shadow mode: compare local selection with remote J-Prime selection.

        Returns divergence dict if selections differ, None if they match.
        Logs divergence for shadow mode metrics.
        """
        local = self.select_for_task(task_type, command)
        remote_brain = remote_classification.get("brain_used", "")
        remote_complexity = remote_classification.get("complexity", "")

        divergences = {}
        if local.brain_id != remote_brain:
            divergences["brain_id"] = {
                "local": local.brain_id,
                "remote": remote_brain,
                "severity": "WARN",
            }
        if local.complexity != remote_complexity:
            divergences["complexity"] = {
                "local": local.complexity,
                "remote": remote_complexity,
                "severity": "WARN",
            }

        if divergences:
            logger.warning(
                "[Shadow] Divergence: task=%s command='%s' divergences=%s",
                task_type, command[:60], divergences,
            )
            return divergences
        return None
```

- [ ] **Step 2: Wire MindClient into command processor**

Add the following to `backend/api/unified_command_processor.py`, in the `_execute_command_pipeline` method, **right before** the existing `response = await self._call_jprime(...)` call. Use the feature flag `JARVIS_USE_REMOTE_BRAIN_SELECTOR`:

```python
        # v295.0: Remote brain selection via MindClient (Step 1 migration)
        # Feature flag: JARVIS_USE_REMOTE_BRAIN_SELECTOR=true enables remote selection
        # Shadow mode: JARVIS_BRAIN_SELECTOR_SHADOW=true logs divergence without switching
        _use_remote_brain = os.getenv("JARVIS_USE_REMOTE_BRAIN_SELECTOR", "false").lower() == "true"
        _shadow_mode = os.getenv("JARVIS_BRAIN_SELECTOR_SHADOW", "false").lower() == "true"

        if _use_remote_brain or _shadow_mode:
            try:
                from backend.core.mind_client import get_mind_client
                _mind = get_mind_client()
                _remote_result = await _mind.select_brain(
                    command=command_text,
                    task_type="classification",
                    context=_jprime_ctx,
                )
                if _remote_result and _shadow_mode:
                    # Shadow: compare but don't use
                    try:
                        from backend.core.interactive_brain_router import get_interactive_brain_router
                        _local_router = get_interactive_brain_router()
                        _classification = _remote_result.get("classification", {})
                        await _local_router.compare_with_remote(
                            "classification", command_text, _classification,
                        )
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("[v295] Remote brain selection unavailable: %s", exc)
```

- [ ] **Step 3: Commit**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
git add backend/core/interactive_brain_router.py backend/api/unified_command_processor.py
git commit -m "feat(shadow): wire MindClient brain selection with shadow mode

Feature flags:
- JARVIS_USE_REMOTE_BRAIN_SELECTOR=true: use J-Prime brain selection
- JARVIS_BRAIN_SELECTOR_SHADOW=true: compare local vs remote, log divergence

Shadow mode logs WARN-level divergences for brain_id and complexity
mismatches without changing behavior."
```

---

## Task 7: End-to-End Smoke Test

- [ ] **Step 1: Verify J-Prime endpoints respond**

With J-Prime running on GCP (136.113.252.164:8000):

```bash
# Health check
curl -s http://136.113.252.164:8000/v1/reason/health | python3 -m json.tool

# Protocol version
curl -s http://136.113.252.164:8000/v1/protocol/version | python3 -m json.tool

# Brain selection
curl -s -X POST http://136.113.252.164:8000/v1/reason/select \
  -H "Content-Type: application/json" \
  -d '{"request_id":"test-001","session_id":"smoke","trace_id":"smoke","command":"open Safari"}' \
  | python3 -m json.tool
```

Expected: all return valid JSON with correct schema fields.

- [ ] **Step 2: Verify shadow mode on JARVIS**

```bash
# Enable shadow mode
export JARVIS_BRAIN_SELECTOR_SHADOW=true

# Run a voice command and check logs for [Shadow] entries
# Look for: "[Shadow] Divergence: ..." or absence (match)
```

- [ ] **Step 3: Commit any fixes from smoke test**

```bash
git add -A && git commit -m "fix: address issues found in Step 0+1 smoke test"
```

---

## Task 8: Review Fixes — Circuit Breaker, Health Task, Shadow Tests

Addresses plan review issues. Must be completed before Step 1 cutover.

**Files:**
- Modify: `backend/core/mind_client.py`
- Create: `tests/core/test_shadow_mode.py`

- [ ] **Step 1: Add 3-state circuit breaker to MindClient**

Replace the simple consecutive-failure counter in `mind_client.py` with a proper CLOSED/OPEN/HALF_OPEN circuit breaker. Follow the pattern in `backend/core/prime_client.py`:

```python
class _CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class _CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 30.0):
        self.state = _CircuitState.CLOSED
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()

    async def can_execute(self) -> bool:
        async with self._lock:
            if self.state == _CircuitState.CLOSED:
                return True
            if self.state == _CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self._cooldown_s:
                    self.state = _CircuitState.HALF_OPEN
                    return True  # allow one test request
                return False
            if self.state == _CircuitState.HALF_OPEN:
                return True  # already testing
            return False

    async def record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            self.state = _CircuitState.CLOSED

    async def record_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self._failure_threshold:
                self.state = _CircuitState.OPEN
```

Wire into MindClient: check `_circuit.can_execute()` before HTTP calls, call `record_success/failure` after.

- [ ] **Step 2: Add background health check task**

Add to `MindClient.__init__`:

```python
        self._health_task: Optional[asyncio.Task] = None
        self._health_interval_s = float(os.getenv("JARVIS_MIND_HEALTH_INTERVAL_S", "30"))
```

Add method:

```python
    async def start_health_monitor(self) -> None:
        """Start background health check task (30s interval)."""
        if self._health_task is not None:
            return
        self._health_task = asyncio.create_task(
            self._health_loop(), name="mind_health_monitor"
        )

    async def _health_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._health_interval_s)
                await self.check_health()
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # check_health already handles level transitions

    async def stop_health_monitor(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
```

- [ ] **Step 3: Write shadow mode tests**

File: `tests/core/test_shadow_mode.py`

```python
"""Tests for shadow mode divergence comparison."""
import pytest
from backend.core.interactive_brain_router import InteractiveBrainRouter


class TestShadowComparison:
    @pytest.fixture
    def router(self):
        return InteractiveBrainRouter()

    @pytest.mark.asyncio
    async def test_no_divergence_when_matching(self, router):
        remote = {"brain_used": "qwen_coder", "complexity": "light"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        assert result is None  # no divergence

    @pytest.mark.asyncio
    async def test_divergence_on_brain_mismatch(self, router):
        remote = {"brain_used": "phi3_lightweight", "complexity": "light"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        assert result is not None
        assert "brain_id" in result
        assert result["brain_id"]["severity"] == "WARN"

    @pytest.mark.asyncio
    async def test_divergence_on_complexity_mismatch(self, router):
        remote = {"brain_used": "qwen_coder", "complexity": "heavy"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        assert result is not None
        assert "complexity" in result

    @pytest.mark.asyncio
    async def test_keyword_escalation_matches_remote(self, router):
        # "analyze" keyword escalates to complex locally
        remote = {"brain_used": "qwen_coder_32b", "complexity": "complex"}
        result = await router.compare_with_remote(
            "classification", "analyze the root cause", remote
        )
        assert result is None  # both should agree on complex
```

- [ ] **Step 4: Run all tests**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/core/test_shadow_mode.py tests/core/test_mind_client.py -v
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
git add backend/core/mind_client.py tests/core/test_shadow_mode.py
git commit -m "feat(mind): add circuit breaker, health monitor, shadow mode tests

3-state circuit breaker (CLOSED/OPEN/HALF_OPEN) with 30s cooldown.
Background health check task (30s interval). Shadow mode comparison
tests for brain_id and complexity divergence."
```

---

## Implementation Notes (from plan review)

These items are deferred to future migration steps but documented here for traceability:

1. **PersistentDeque for Level 2 queue**: Required for Level 2 operation. Implement in Step 2 when full reasoning pipeline moves to J-Prime.
2. **Policy YAML as source of truth**: Task-complexity mappings are hardcoded as Python dicts in `unified_brain_selector.py` for Step 1 speed. Step 2 should read from `brain_selection_policy.yaml` `interactive_task_complexity` section.
3. **Boot gate wiring**: `MindClient.check_protocol_version()` exists but is not called at JARVIS startup. Wire into `unified_supervisor.py` Zone 5 during Step 2.
4. **Level 1 degraded cost cap ($0.50/session)**: Track Claude API cost per session in MindClient. Implement when Level 1 emergency planner is built (Step 2).
5. **Shadow mode confidence comparison**: Add INFO-level comparison for confidence differences >10% in Step 2 when planning node produces confidence scores.
6. **HEAVY_CODE normalization**: Add `{"HEAVY_CODE": "heavy", "heavy_code": "heavy"}` mapping when Ouroboros BrainSelector calls through the unified selector (Step 3).
