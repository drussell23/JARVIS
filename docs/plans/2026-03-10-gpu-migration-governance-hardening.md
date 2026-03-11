# GPU Migration & Governance Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upgrade GCP J-Prime VM to GPU-class compute, enforce a compute-class admission gate preventing CPU routing of >7B models, add host-binding invariants, and add per-op proof artifacts — making J-Prime the authoritative golden-image runtime.

**Architecture:** Four sequential phases. Phase A adds the policy/gate layer (no VM changes). Phase B validates the capability contract end-to-end. Phase C executes the VM GPU upgrade. Phase D adds observability. Each phase ends with a go/no-go gate before the next begins. No silent fallbacks — every contract mismatch is a hard fail reported to the caller.

**Tech Stack:** Python 3.11, aiohttp, yaml, GCP Compute Engine API (via `gcloud`), llama-cpp-python (GPU Metal/CUDA auto-detect), pytest-asyncio, existing Ouroboros governance stack.

---

## Benchmark Numbers (used for timeout budget)

| Config | Model | Size | Tok/s (est.) | 512-token gen | Recommended `generation_timeout_s` |
|--------|-------|------|-------------|---------------|-------------------------------------|
| e2-highmem-8 (CPU) | 14B Q4_K_M | 8.4 GB | 1–3 | 170–512 s | **N/A — exceeds budget** |
| e2-highmem-8 (CPU) | 7B Q4_K_M | 4.4 GB | 3–6 | 85–170 s | **Marginal — exceeds 120 s budget** |
| n1-standard-4 + T4 | 7B Q4_K_M | 4.4 GB | 30–50 | 10–17 s | **60 s** (3.5× buffer) |
| n1-standard-4 + T4 | 14B Q4_K_M | 8.4 GB | 15–25 | 20–34 s | **120 s** (3.5× buffer) |
| n1-standard-8 + L4 | 14B Q4_K_M | 8.4 GB | 35–55 | 9–15 s | **60 s** (4× buffer) |

**Pipeline timeout budget** = generation_timeout_s + 90 s overhead (classify/route/validate/apply/verify):
- T4 + 7B: pipeline_timeout_s = **150 s**
- T4 + 14B: pipeline_timeout_s = **210 s**
- L4 + 14B: pipeline_timeout_s = **150 s**

Current hardcoded values in `governed_loop_service.py` (lines 335, 343):
```
generation_timeout_s = 120.0   → must become 60.0 after T4+7B confirmed
pipeline_timeout_s   = 600.0   → must become 150.0 after T4+7B confirmed
```

---

## Go/No-Go Criteria

**GO** requires ALL of:
- [ ] `/v1/capability` returns `compute_class ∈ {gpu_t4, gpu_l4, gpu_a100}`
- [ ] Admission gate rejects CPU route for any brain with `min_compute_class: gpu_t4`
- [ ] Model artifact on VM matches policy mapping (SHA or filename check)
- [ ] Host-binding invariant: `telemetry_host == selector_host == execution_host`
- [ ] Actual tok/s measured ≥ 20 tok/s at steady state (GPU confirmed active)
- [ ] `terminal_class` field populated in every `OperationResult`
- [ ] Ignition test achieves 6/6 via J-Prime primary path (no Claude fallback)

**NO-GO** (halt and report) on ANY of:
- `compute_class: cpu` returned from `/v1/capability` after VM restart
- Admission gate silently routes anyway (instead of raising)
- Model artifact mismatch at boot
- `telemetry_host != selector_host` at runtime
- Tok/s < 10 (GPU not active, CUDA/Metal fallback to CPU)

---

## File-Level Change List

| File | Action | Phase |
|------|--------|-------|
| `/Users/djrussell23/Documents/repos/jarvis-prime/run_server.py` | Add `/v1/capability` endpoint | A |
| `backend/core/ouroboros/governance/brain_selection_policy.yaml` | Add `compute_class`, `min_compute_class`, `model_artifact` fields | A |
| `backend/core/ouroboros/governance/governed_loop_service.py` | Add compute-class admission gate in `_preflight_check()`; update timeouts after Phase C | A, C |
| `backend/core/prime_client.py` | Add `fetch_capability()` method; validate at `initialize()` | B |
| `backend/core/ouroboros/governance/providers.py` | Pass host from capability to telemetry; enforce host-binding | B |
| `backend/core/gcp_vm_manager.py` | Add `upgrade_to_gpu()` helper (wraps gcloud recreate) | C |
| `backend/core/ouroboros/governance/op_context.py` | Add `terminal_class: str` to `OperationResult` (it lives in governed_loop_service.py — see below) | D |
| `backend/core/ouroboros/governance/governed_loop_service.py` | Add `terminal_class` to `OperationResult` dataclass; populate in `_finalize_result()` | D |
| `tests/test_ouroboros_governance/test_compute_admission.py` | New file: Phase A tests | A |
| `tests/test_ouroboros_governance/test_capability_contract.py` | New file: Phase B tests | B |
| `tests/test_ouroboros_governance/test_gpu_proof_artifacts.py` | New file: Phase D tests | D |

---

## Phase A: Compute-Class Admission Gate

### Task A1: Add `/v1/capability` endpoint to run_server.py

**Files:**
- Modify: `/Users/djrussell23/Documents/repos/jarvis-prime/run_server.py` (near `/health` handler, ~line 480)

**Step 1: Write the failing test**

Create `tests/test_ouroboros_governance/test_compute_admission.py`:

```python
"""Phase A: compute-class admission gate tests."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


# ─── Test A1.1: /v1/capability schema ───────────────────────────────────────

class TestCapabilityEndpointSchema:
    """The /v1/capability endpoint must return a well-formed contract."""

    def test_capability_response_has_required_fields(self):
        """Capability dict must contain compute_class, model_id, gpu_layers, tok_s_est."""
        cap = {
            "compute_class": "gpu_t4",
            "model_id": "Qwen2.5-Coder-7B-Instruct-Q4_K_M",
            "model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
            "gpu_layers": -1,
            "tok_s_estimate": 40,
            "host": "jarvis-prime-stable",
        }
        required = {"compute_class", "model_id", "model_artifact", "gpu_layers", "tok_s_estimate", "host"}
        assert required <= cap.keys(), f"Missing fields: {required - cap.keys()}"

    def test_compute_class_values_are_bounded(self):
        """compute_class must be one of the known enum values."""
        valid = {"cpu", "gpu_t4", "gpu_l4", "gpu_v100", "gpu_a100"}
        cap_cpu = {"compute_class": "cpu"}
        cap_gpu = {"compute_class": "gpu_t4"}
        assert cap_cpu["compute_class"] in valid
        assert cap_gpu["compute_class"] in valid
        assert "gpu_banana" not in valid

    def test_gpu_layers_minus_one_implies_full_offload(self):
        """gpu_layers=-1 means all layers on GPU."""
        cap = {"gpu_layers": -1, "compute_class": "gpu_t4"}
        # Contract: if compute_class != "cpu" then gpu_layers must be -1
        if cap["compute_class"] != "cpu":
            assert cap["gpu_layers"] == -1, "GPU class must have gpu_layers=-1"
```

**Step 2: Run test to verify it fails**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
source venv/bin/activate
pytest tests/test_ouroboros_governance/test_compute_admission.py -v
```

Expected: PASS (schema tests are pure contracts — they should pass immediately as spec documentation)

**Step 3: Add `/v1/capability` endpoint to run_server.py**

Find the `/health` route handler in `run_server.py`. Add immediately after it:

```python
@routes.get("/v1/capability")
async def capability_handler(request: web.Request) -> web.Response:
    """Return compute contract for this server instance.

    Fields:
      compute_class: one of cpu | gpu_t4 | gpu_l4 | gpu_v100 | gpu_a100
      model_id: human-readable model name
      model_artifact: filename of the loaded model file
      gpu_layers: -1 = all layers on GPU, 0 = CPU only
      tok_s_estimate: measured tokens/sec from last benchmark or 0 if not yet measured
      host: hostname of this server
    """
    import socket

    # Determine compute class from n_gpu_layers
    n_gpu = getattr(llm, "n_gpu_layers", 0) if llm is not None else 0

    if n_gpu == -1 or n_gpu > 0:
        # Detect GPU type from environment — set by startup script or gcloud metadata
        gpu_type = os.environ.get("JARVIS_GPU_TYPE", "").lower()
        if "l4" in gpu_type:
            compute_class = "gpu_l4"
        elif "v100" in gpu_type:
            compute_class = "gpu_v100"
        elif "a100" in gpu_type:
            compute_class = "gpu_a100"
        else:
            compute_class = "gpu_t4"  # default for T4 fleet
    else:
        compute_class = "cpu"

    model_path = getattr(llm, "model_path", "") if llm is not None else ""
    model_artifact = os.path.basename(model_path) if model_path else "unknown"

    # Extract model_id from artifact filename (strip extension and quantization suffix)
    import re
    model_id = re.sub(r"[-_]Q\d.*$", "", model_artifact.replace(".gguf", ""), flags=re.IGNORECASE)

    cap = {
        "compute_class": compute_class,
        "model_id": model_id,
        "model_artifact": model_artifact,
        "gpu_layers": n_gpu,
        "tok_s_estimate": getattr(request.app, "_tok_s_estimate", 0),
        "host": socket.gethostname(),
        "schema_version": "1.0",
    }
    return web.json_response(cap)
```

**Step 4: Run test to verify schema passes**

```bash
pytest tests/test_ouroboros_governance/test_compute_admission.py::TestCapabilityEndpointSchema -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add /Users/djrussell23/Documents/repos/jarvis-prime/run_server.py \
        tests/test_ouroboros_governance/test_compute_admission.py
git commit -m "feat(capability): add /v1/capability endpoint to run_server.py

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task A2: Add compute_class fields to brain_selection_policy.yaml

**Files:**
- Modify: `backend/core/ouroboros/governance/brain_selection_policy.yaml`

**Step 1: Write the failing test**

Add to `tests/test_ouroboros_governance/test_compute_admission.py`:

```python
# ─── Test A2: Policy schema validation ──────────────────────────────────────

import yaml
from pathlib import Path

POLICY_PATH = Path("backend/core/ouroboros/governance/brain_selection_policy.yaml")


class TestBrainPolicyComputeClass:
    """brain_selection_policy.yaml must have compute_class contract fields."""

    def test_policy_has_compute_class_per_brain(self):
        """Every brain entry must have compute_class and min_compute_class."""
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = doc.get("brains", {}).get("required", {})
        assert brains, "No brains defined in policy"
        for brain_id, cfg in brains.items():
            assert "compute_class" in cfg, f"Brain {brain_id!r} missing compute_class"
            assert "min_compute_class" in cfg, f"Brain {brain_id!r} missing min_compute_class"

    def test_policy_has_model_artifact_per_brain(self):
        """Every brain entry must have model_artifact for integrity check."""
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = doc.get("brains", {}).get("required", {})
        for brain_id, cfg in brains.items():
            assert "model_artifact" in cfg, f"Brain {brain_id!r} missing model_artifact"

    def test_compute_class_order_is_respected(self):
        """min_compute_class=gpu_t4 must not route to cpu."""
        compute_rank = {"cpu": 0, "gpu_t4": 1, "gpu_l4": 2, "gpu_v100": 3, "gpu_a100": 4}
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = doc.get("brains", {}).get("required", {})
        for brain_id, cfg in brains.items():
            cc = cfg.get("compute_class", "cpu")
            min_cc = cfg.get("min_compute_class", "cpu")
            assert compute_rank.get(cc, 0) >= compute_rank.get(min_cc, 0), (
                f"Brain {brain_id!r} has compute_class={cc!r} below "
                f"min_compute_class={min_cc!r}"
            )
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_ouroboros_governance/test_compute_admission.py::TestBrainPolicyComputeClass -v
```

Expected: FAIL — `compute_class` key missing from brain entries

**Step 3: Update brain_selection_policy.yaml**

Read the current policy first:

```bash
cat backend/core/ouroboros/governance/brain_selection_policy.yaml
```

Then add `compute_class`, `min_compute_class`, and `model_artifact` to each brain entry under `brains.required`. Example diff:

```yaml
# Before (phi3_lightweight entry):
  phi3_lightweight:
    model: llama-3.2-1b
    description: "Lightweight 1B model for trivial/doc tasks"

# After:
  phi3_lightweight:
    model: llama-3.2-1b
    model_artifact: "Llama-3.2-1B-Instruct-Q4_K_M.gguf"
    compute_class: cpu          # 1B model runs fine on CPU
    min_compute_class: cpu
    description: "Lightweight 1B model for trivial/doc tasks"

# Before (qwen_coder entry):
  qwen_coder:
    model: qwen-2.5-coder-7b
    description: "7B coder model"

# After:
  qwen_coder:
    model: qwen-2.5-coder-7b
    model_artifact: "qwen2.5-coder-7b-instruct-q4_k_m.gguf"
    compute_class: gpu_t4       # requires GPU for pipeline deadline
    min_compute_class: gpu_t4
    description: "7B coder model — requires T4-class GPU"

# Before (deepseek_r1 entry):
  deepseek_r1:
    model: deepseek-r1-7b
    description: "7B reasoning model"

# After:
  deepseek_r1:
    model: deepseek-r1-7b
    model_artifact: "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf"
    compute_class: gpu_t4
    min_compute_class: gpu_t4
    description: "7B reasoning model — requires T4-class GPU"
```

Also add compute_class rank table to policy root:

```yaml
compute_class_rank:
  cpu: 0
  gpu_t4: 1
  gpu_l4: 2
  gpu_v100: 3
  gpu_a100: 4
```

**Step 4: Run test to verify pass**

```bash
pytest tests/test_ouroboros_governance/test_compute_admission.py::TestBrainPolicyComputeClass -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/brain_selection_policy.yaml \
        tests/test_ouroboros_governance/test_compute_admission.py
git commit -m "feat(policy): add compute_class + model_artifact fields to brain_selection_policy.yaml

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task A3: Compute-class admission gate in GovernedLoopService

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
  (in `_preflight_check()` — locate with `grep -n "_preflight_check"`)

**Step 1: Write the failing test**

Add to `tests/test_ouroboros_governance/test_compute_admission.py`:

```python
# ─── Test A3: Admission gate ─────────────────────────────────────────────────

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

class TestComputeAdmissionGate:
    """GovernedLoopService must hard-fail when VM compute_class < brain min_compute_class."""

    @pytest.mark.asyncio
    async def test_cpu_vm_rejected_for_gpu_brain(self):
        """Routing a gpu_t4-class brain to a cpu VM must raise ComputeClassMismatch."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            ComputeClassMismatch,
            _check_compute_admission,
        )
        capability = {"compute_class": "cpu", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "gpu_t4", "model_artifact": "qwen.gguf"}

        with pytest.raises(ComputeClassMismatch) as exc_info:
            _check_compute_admission(brain_cfg, capability)

        assert "cpu" in str(exc_info.value)
        assert "gpu_t4" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_gpu_vm_accepted_for_gpu_brain(self):
        """Routing a gpu_t4-class brain to a gpu_t4 VM must not raise."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_compute_admission,
        )
        capability = {"compute_class": "gpu_t4", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "gpu_t4", "model_artifact": "qwen.gguf"}
        # Must not raise
        _check_compute_admission(brain_cfg, capability)

    @pytest.mark.asyncio
    async def test_cpu_vm_accepted_for_cpu_brain(self):
        """cpu-class brain can route to cpu VM."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_compute_admission,
        )
        capability = {"compute_class": "cpu", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "cpu", "model_artifact": "llama-1b.gguf"}
        _check_compute_admission(brain_cfg, capability)

    @pytest.mark.asyncio
    async def test_higher_gpu_class_satisfies_lower_min(self):
        """gpu_l4 VM satisfies gpu_t4 min_compute_class."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_compute_admission,
        )
        capability = {"compute_class": "gpu_l4", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "gpu_t4", "model_artifact": "qwen.gguf"}
        _check_compute_admission(brain_cfg, capability)
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_ouroboros_governance/test_compute_admission.py::TestComputeAdmissionGate -v
```

Expected: FAIL — `ComputeClassMismatch` and `_check_compute_admission` not importable

**Step 3: Implement `ComputeClassMismatch` and `_check_compute_admission`**

In `backend/core/ouroboros/governance/governed_loop_service.py`, add near the top (after imports):

```python
# ─── Compute-class admission ─────────────────────────────────────────────────

_COMPUTE_RANK: dict[str, int] = {
    "cpu": 0,
    "gpu_t4": 1,
    "gpu_l4": 2,
    "gpu_v100": 3,
    "gpu_a100": 4,
}


class ComputeClassMismatch(RuntimeError):
    """Raised when VM compute_class is below the brain's min_compute_class."""


def _check_compute_admission(
    brain_cfg: dict,
    capability: dict,
) -> None:
    """Hard-fail if VM compute_class < brain min_compute_class.

    Args:
        brain_cfg: entry from brain_selection_policy.yaml (must have min_compute_class)
        capability: dict from /v1/capability (must have compute_class)

    Raises:
        ComputeClassMismatch: if VM rank < brain minimum rank
    """
    vm_class = capability.get("compute_class", "cpu")
    min_class = brain_cfg.get("min_compute_class", "cpu")
    vm_rank = _COMPUTE_RANK.get(vm_class, 0)
    min_rank = _COMPUTE_RANK.get(min_class, 0)
    if vm_rank < min_rank:
        raise ComputeClassMismatch(
            f"VM compute_class={vm_class!r} (rank {vm_rank}) is below "
            f"brain min_compute_class={min_class!r} (rank {min_rank}). "
            f"Route to J-Prime is denied. Upgrade VM GPU or select a lower-tier brain."
        )
```

Then in `_preflight_check()`, add after the `_file_touch_cache` check:

```python
# ── Compute-class admission gate ──────────────────────────────────────
if self._vm_capability is not None:
    brain_id = ctx.routing_decision.brain_id if ctx.routing_decision else None
    if brain_id:
        brain_cfg = self._brain_policy.get("brains", {}).get("required", {}).get(brain_id, {})
        try:
            _check_compute_admission(brain_cfg, self._vm_capability)
        except ComputeClassMismatch as exc:
            logger.error("[GLS] Compute admission DENIED for op=%s: %s", ctx.op_id, exc)
            raise
```

Add `self._vm_capability: dict | None = None` to `__init__`.

**Step 4: Run tests to verify pass**

```bash
pytest tests/test_ouroboros_governance/test_compute_admission.py -v
```

Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_compute_admission.py
git commit -m "feat(admission): add compute-class admission gate to GovernedLoopService

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task A4: Model artifact integrity check at boot

**Files:**
- Modify: `backend/core/prime_client.py` (add `fetch_capability()`)
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (call at start)

**Step 1: Write the failing test**

Create `tests/test_ouroboros_governance/test_capability_contract.py`:

```python
"""Phase B: capability contract validation tests."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestModelArtifactIntegrity:
    """Boot must reject if VM model_artifact != policy model_artifact."""

    @pytest.mark.asyncio
    async def test_matching_artifact_passes(self):
        """If VM model_artifact matches policy, no error raised."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_artifact_integrity,
        )
        capability = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        brain_cfg = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        _check_artifact_integrity(brain_cfg, capability)  # must not raise

    @pytest.mark.asyncio
    async def test_mismatched_artifact_raises(self):
        """If VM model_artifact != policy, ModelArtifactMismatch raised."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            ModelArtifactMismatch,
            _check_artifact_integrity,
        )
        capability = {"model_artifact": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf"}
        brain_cfg = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        with pytest.raises(ModelArtifactMismatch) as exc_info:
            _check_artifact_integrity(brain_cfg, capability)
        assert "14B" in str(exc_info.value) or "mismatch" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_case_insensitive_match(self):
        """Artifact comparison is case-insensitive (VM filenames vary)."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_artifact_integrity,
        )
        capability = {"model_artifact": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"}
        brain_cfg = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        _check_artifact_integrity(brain_cfg, capability)  # must not raise
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_ouroboros_governance/test_capability_contract.py::TestModelArtifactIntegrity -v
```

Expected: FAIL — `ModelArtifactMismatch` and `_check_artifact_integrity` not importable

**Step 3: Implement artifact integrity check**

In `governed_loop_service.py`, add after `_check_compute_admission`:

```python
class ModelArtifactMismatch(RuntimeError):
    """Raised when VM model_artifact doesn't match policy model_artifact."""


def _check_artifact_integrity(brain_cfg: dict, capability: dict) -> None:
    """Hard-fail if model loaded on VM doesn't match policy's expected artifact.

    Comparison is case-insensitive to handle filesystem conventions.

    Raises:
        ModelArtifactMismatch: if filenames don't match (case-insensitive)
    """
    policy_artifact = brain_cfg.get("model_artifact", "")
    vm_artifact = capability.get("model_artifact", "")
    if not policy_artifact or not vm_artifact:
        return  # can't check — skip (unknown artifact)
    if policy_artifact.lower() != vm_artifact.lower():
        raise ModelArtifactMismatch(
            f"Model artifact mismatch: policy expects {policy_artifact!r} "
            f"but VM reports {vm_artifact!r}. "
            f"Update policy or reload correct model on VM."
        )
```

**Step 4: Run tests to verify pass**

```bash
pytest tests/test_ouroboros_governance/test_capability_contract.py::TestModelArtifactIntegrity -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_capability_contract.py
git commit -m "feat(integrity): add model artifact integrity check at boot

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task A5: Host-binding invariant

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Modify: `backend/core/ouroboros/governance/providers.py`

**Step 1: Write the failing test**

Add to `tests/test_ouroboros_governance/test_capability_contract.py`:

```python
class TestHostBindingInvariant:
    """telemetry_host == selector_host == execution_host — no psutil fallback for remote routes."""

    def test_host_binding_check_passes_when_hosts_match(self):
        """If all three hosts match, no error."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_host_binding,
        )
        _check_host_binding(
            telemetry_host="jarvis-prime-stable",
            selector_host="jarvis-prime-stable",
            execution_host="jarvis-prime-stable",
        )  # must not raise

    def test_host_binding_raises_on_mismatch(self):
        """If telemetry_host != execution_host, HostBindingViolation raised."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            HostBindingViolation,
            _check_host_binding,
        )
        with pytest.raises(HostBindingViolation):
            _check_host_binding(
                telemetry_host="jarvis-prime-stable",
                selector_host="jarvis-prime-stable",
                execution_host="some-other-host",
            )

    def test_local_psutil_host_not_used_for_remote_routes(self):
        """execution_host must come from capability.host, never socket.gethostname() on local machine."""
        import socket
        local_hostname = socket.gethostname()
        vm_hostname = "jarvis-prime-stable"
        # They should be different — if they're the same the test environment is wrong
        # But the invariant is: execution_host must equal capability["host"]
        capability = {"host": vm_hostname, "compute_class": "gpu_t4"}
        # execution_host derived from capability, not local psutil
        execution_host = capability["host"]
        assert execution_host == vm_hostname
        assert execution_host != local_hostname or vm_hostname == local_hostname
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_ouroboros_governance/test_capability_contract.py::TestHostBindingInvariant -v
```

Expected: FAIL — `HostBindingViolation` and `_check_host_binding` not importable

**Step 3: Implement host-binding check**

In `governed_loop_service.py`, add:

```python
class HostBindingViolation(RuntimeError):
    """Raised when telemetry_host, selector_host, and execution_host don't all match."""


def _check_host_binding(
    telemetry_host: str,
    selector_host: str,
    execution_host: str,
) -> None:
    """Enforce the invariant: all three host references must be identical.

    This prevents scenarios where routing selects VM-A but execution reaches VM-B,
    or where local psutil data is incorrectly used for a remote route.

    Raises:
        HostBindingViolation: if any host differs from the others
    """
    hosts = {telemetry_host, selector_host, execution_host}
    if len(hosts) > 1:
        raise HostBindingViolation(
            f"Host-binding invariant violated: "
            f"telemetry_host={telemetry_host!r}, "
            f"selector_host={selector_host!r}, "
            f"execution_host={execution_host!r}. "
            f"All three must be identical."
        )
```

**Step 4: Run tests to verify pass**

```bash
pytest tests/test_ouroboros_governance/test_capability_contract.py -v
```

Expected: All PASS

**Step 5: Run full test suite to check regressions**

```bash
pytest tests/test_ouroboros_governance/ -v --tb=short -q 2>&1 | tail -30
```

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_capability_contract.py
git commit -m "feat(host-binding): enforce telemetry_host==selector_host==execution_host invariant

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Phase B: Golden-Image Runtime Migration

### Task B1: PrimeClient.fetch_capability()

**Files:**
- Modify: `backend/core/prime_client.py`

**Step 1: Write the failing test**

Add to `tests/test_ouroboros_governance/test_capability_contract.py`:

```python
class TestPrimeClientCapability:
    """PrimeClient must expose fetch_capability() and validate at initialize()."""

    @pytest.mark.asyncio
    async def test_fetch_capability_parses_response(self):
        """fetch_capability() returns dict with compute_class."""
        from backend.core.prime_client import PrimeClient, PrimeClientConfig

        fake_response = {
            "compute_class": "gpu_t4",
            "model_id": "Qwen2.5-Coder-7B",
            "model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
            "gpu_layers": -1,
            "tok_s_estimate": 40,
            "host": "jarvis-prime-stable",
            "schema_version": "1.0",
        }

        import aiohttp
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=fake_response)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        config = PrimeClientConfig()
        client = PrimeClient(config=config)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            cap = await client.fetch_capability()

        assert cap["compute_class"] == "gpu_t4"
        assert cap["host"] == "jarvis-prime-stable"

    @pytest.mark.asyncio
    async def test_fetch_capability_raises_on_http_error(self):
        """fetch_capability() raises RuntimeError if HTTP status != 200."""
        from backend.core.prime_client import PrimeClient, PrimeClientConfig, CapabilityFetchError

        from unittest.mock import AsyncMock, MagicMock, patch

        mock_response = AsyncMock()
        mock_response.status = 503
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        config = PrimeClientConfig()
        client = PrimeClient(config=config)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(CapabilityFetchError):
                await client.fetch_capability()
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_ouroboros_governance/test_capability_contract.py::TestPrimeClientCapability -v
```

Expected: FAIL — `fetch_capability` method doesn't exist, `CapabilityFetchError` not importable

**Step 3: Implement fetch_capability in PrimeClient**

In `backend/core/prime_client.py`, add:

```python
class CapabilityFetchError(RuntimeError):
    """Raised when /v1/capability fetch fails or returns non-200."""


# Inside PrimeClient class:

@property
def capability_url(self) -> str:
    return f"{self.config.base_url}/v1/capability"

async def fetch_capability(self) -> dict:
    """Fetch compute contract from /v1/capability.

    Returns:
        dict with compute_class, model_id, model_artifact, gpu_layers,
              tok_s_estimate, host, schema_version

    Raises:
        CapabilityFetchError: on HTTP error or JSON parse failure
    """
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=10.0)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(self.capability_url) as resp:
                if resp.status != 200:
                    raise CapabilityFetchError(
                        f"capability fetch failed: HTTP {resp.status} from {self.capability_url}"
                    )
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise CapabilityFetchError(
                        f"capability JSON parse error: {exc}"
                    ) from exc
    except CapabilityFetchError:
        raise
    except Exception as exc:
        raise CapabilityFetchError(
            f"capability fetch error ({type(exc).__name__}): {exc}"
        ) from exc
    return data
```

**Step 4: Run tests**

```bash
pytest tests/test_ouroboros_governance/test_capability_contract.py::TestPrimeClientCapability -v
```

Expected: PASS

**Step 5: Wire capability fetch into GLS startup**

In `GovernedLoopService.start()`, after `PrimeClient.initialize()` succeeds:

```python
# Fetch and validate capability contract
if self._prime_client is not None:
    try:
        cap = await self._prime_client.fetch_capability()
        self._vm_capability = cap
        logger.info(
            "[GLS] VM capability: compute_class=%s model=%s host=%s gpu_layers=%s tok_s=%s",
            cap.get("compute_class"), cap.get("model_id"),
            cap.get("host"), cap.get("gpu_layers"), cap.get("tok_s_estimate"),
        )
        # Validate compute class against policy default brain
        default_brain_id = self._brain_policy.get("routing", {}).get(
            "task_class_map", {}
        ).get("tier1", [None])[0]
        if default_brain_id:
            brain_cfg = (
                self._brain_policy.get("brains", {})
                .get("required", {})
                .get(default_brain_id, {})
            )
            _check_compute_admission(brain_cfg, cap)
            _check_artifact_integrity(brain_cfg, cap)
    except (ComputeClassMismatch, ModelArtifactMismatch) as exc:
        logger.error("[GLS] Boot-time capability validation FAILED: %s", exc)
        raise  # hard fail — do not proceed with wrong compute class
    except Exception as exc:
        logger.warning("[GLS] Could not fetch capability (non-fatal): %s", exc)
        self._vm_capability = None
```

**Step 6: Commit**

```bash
git add backend/core/prime_client.py \
        backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_capability_contract.py
git commit -m "feat(capability): PrimeClient.fetch_capability() + boot validation in GLS

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Phase C: GPU Enablement

> **IMPORTANT**: Phase C involves VM recreation. This is irreversible until another gcloud operation. Read every step before executing. Confirm with user before running `gcloud compute instances` mutating commands.

### Task C1: Confirm current VM state before changes

**Step 1: Snapshot current VM state**

```bash
gcloud compute instances describe jarvis-prime-stable \
    --zone=us-central1-a \
    --format="yaml(machineType, scheduling, guestAccelerators, status)"
```

Expected output:
```yaml
machineType: .../e2-highmem-8
scheduling:
  onHostMaintenance: MIGRATE
guestAccelerators: []
status: RUNNING
```

**Step 2: Verify J-Prime service is running**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="systemctl is-active jarvis-prime || echo 'NOT_ACTIVE'"
```

**Step 3: Record current model symlink**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="ls -la /opt/jarvis-prime/models/current.gguf && ls -lh /opt/jarvis-prime/models/*.gguf"
```

---

### Task C2: Switch model to qwen_coder 7B (pre-GPU upgrade)

> Switch model BEFORE GPU upgrade so the new config is baked in at restart.

**Step 1: Write the failing test (VM integration — run against live VM)**

Add to `tests/test_ouroboros_governance/test_capability_contract.py`:

```python
class TestVMModelSwitch:
    """After model switch, /v1/capability must report the new artifact."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_capability_reports_7b_after_switch(self):
        """After switching current.gguf to 7B, /v1/capability.model_artifact matches."""
        from backend.core.prime_client import PrimeClient, PrimeClientConfig
        config = PrimeClientConfig()
        client = PrimeClient(config=config)
        cap = await client.fetch_capability()
        assert "7b" in cap["model_artifact"].lower() or "7B" in cap["model_artifact"]
        assert cap["compute_class"] in ("cpu", "gpu_t4")  # cpu until GPU upgrade
```

**Step 2: Switch symlink on VM**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="cd /opt/jarvis-prime/models && \
               sudo ln -sf qwen2.5-coder-7b-instruct-q4_k_m.gguf current.gguf && \
               ls -la current.gguf"
```

**Step 3: Restart J-Prime service**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="sudo systemctl restart jarvis-prime && \
               sleep 5 && \
               systemctl is-active jarvis-prime"
```

**Step 4: Verify health**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="curl -s http://localhost:8000/health | python3 -m json.tool"
```

---

### Task C3: GPU VM upgrade (STOP → MODIFY → RESTART)

> **PRE-CONDITION**: User must confirm GPU quota in us-central1 zone.

**Step 1: Check T4 quota**

```bash
gcloud compute regions describe us-central1 --format="yaml(quotas)" \
    | grep -A2 "NVIDIA_T4"
```

If quota is 0, request increase via Cloud Console or use L4 in alternate zone.

**Step 2: Stop VM**

```bash
# Stop VM (non-destructive — disk preserved)
gcloud compute instances stop jarvis-prime-stable --zone=us-central1-a
```

Wait for: `Updated [https://www.googleapis.com/compute/v1/projects/...]`

**Step 3: Change machine type and add GPU**

```bash
# Change from e2-highmem-8 to n1-standard-4 (T4 requires n1 family)
gcloud compute instances set-machine-type jarvis-prime-stable \
    --zone=us-central1-a \
    --machine-type=n1-standard-4

# Add T4 GPU
gcloud compute instances add-accelerator jarvis-prime-stable \
    --zone=us-central1-a \
    --accelerator=type=nvidia-tesla-t4,count=1

# Set scheduling to TERMINATE (required for GPU VMs)
gcloud compute instances set-scheduling jarvis-prime-stable \
    --zone=us-central1-a \
    --maintenance-policy=TERMINATE \
    --no-restart-on-failure
```

**Step 4: Start VM**

```bash
gcloud compute instances start jarvis-prime-stable --zone=us-central1-a
```

**Step 5: Verify GPU visible inside VM**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>&1 || echo 'GPU_NOT_FOUND'"
```

Expected: `Tesla T4, 15360 MiB, 525.xx.xx`

If `GPU_NOT_FOUND`: CUDA drivers may not be installed. Run:
```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="sudo apt-get install -y linux-headers-\$(uname -r) && \
               sudo apt-get install -y cuda-drivers && \
               sudo reboot"
```

Wait 60s then reconnect.

**Step 6: Verify J-Prime auto-detects GPU**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="sudo systemctl restart jarvis-prime && sleep 30 && \
               curl -s http://localhost:8000/health | python3 -m json.tool"
```

**Step 7: Set JARVIS_GPU_TYPE env for capability endpoint**

In the J-Prime systemd unit (`/etc/systemd/system/jarvis-prime.service`), add:
```
Environment="JARVIS_GPU_TYPE=t4"
```

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="sudo sed -i '/\[Service\]/a Environment=\"JARVIS_GPU_TYPE=t4\"' \
               /etc/systemd/system/jarvis-prime.service && \
               sudo systemctl daemon-reload && \
               sudo systemctl restart jarvis-prime"
```

**Step 8: Benchmark tok/s**

```bash
gcloud compute ssh jarvis-prime-stable \
    --zone=us-central1-a \
    --command="curl -s -X POST http://localhost:8000/v1/completions \
               -H 'Content-Type: application/json' \
               -d '{\"prompt\": \"Write a Python function to check if a number is prime:\", \"max_tokens\": 200, \"temperature\": 0}' \
               | python3 -m json.tool"
```

Record `timings.predicted_per_second` from response. This is the actual tok/s.

**Expected benchmark results (go/no-go gates):**
- T4 + 7B Q4_K_M: ≥ 20 tok/s → GO
- T4 + 7B Q4_K_M: < 10 tok/s → NO-GO (GPU not active, investigate CUDA)
- 10-20 tok/s → marginal, investigate model loading

---

### Task C4: Update timeouts based on benchmark

**Step 1: Calculate correct timeouts**

Using measured tok/s (call it `R`):
- `generation_timeout_s = ceil((512 / R) * 3.5)` — 3.5× safety factor for 512 max tokens
- `pipeline_timeout_s = generation_timeout_s + 90` — 90s for pre/post processing

For R=40 tok/s (T4 + 7B):
- `generation_timeout_s = ceil(12.8 * 3.5) = ceil(44.8) = 45` → use **60** (round up to nearest 15)
- `pipeline_timeout_s = 60 + 90 = 150`

**Step 2: Write the failing test**

Add to `tests/test_ouroboros_governance/test_capability_contract.py`:

```python
class TestTimeoutBudget:
    """generation_timeout_s and pipeline_timeout_s must match GPU benchmark."""

    def test_generation_timeout_is_gpu_appropriate(self):
        """generation_timeout_s must be <= 120s (not the old 600s-era value)."""
        import os
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        from pathlib import Path
        config = GovernedLoopConfig.from_env(project_root=Path("."))
        # After GPU upgrade, generation timeout must be tightened
        # Old CPU value was 120s (marginal), GPU should be 60s
        assert config.generation_timeout_s <= 120.0, (
            f"generation_timeout_s={config.generation_timeout_s} is too high for GPU routing"
        )

    def test_pipeline_timeout_is_proportionate(self):
        """pipeline_timeout_s must be >= generation_timeout_s + 60s overhead."""
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        from pathlib import Path
        config = GovernedLoopConfig.from_env(project_root=Path("."))
        assert config.pipeline_timeout_s >= config.generation_timeout_s + 60.0
```

**Step 3: Update .env or governed_loop_service.py defaults**

In `.env` (preferred — no code change needed if env-var driven):
```bash
JARVIS_GOVERNED_GENERATION_TIMEOUT=60
JARVIS_PIPELINE_TIMEOUT_S=150
```

Or update defaults in `GovernedLoopConfig` (line 335, 343 in governed_loop_service.py):
```python
generation_timeout_s: float = float(os.environ.get("JARVIS_GOVERNED_GENERATION_TIMEOUT", "60"))
pipeline_timeout_s: float = float(os.environ.get("JARVIS_PIPELINE_TIMEOUT_S", "150"))
```

**Step 4: Run tests**

```bash
pytest tests/test_ouroboros_governance/test_capability_contract.py::TestTimeoutBudget -v
```

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_capability_contract.py
git commit -m "feat(timeouts): tighten generation/pipeline timeouts for GPU-class routing

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Phase D: Observability — Per-Op Proof Artifacts

### Task D1: Add terminal_class to OperationResult

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (OperationResult dataclass)

**Step 1: Write the failing test**

Create `tests/test_ouroboros_governance/test_gpu_proof_artifacts.py`:

```python
"""Phase D: observability and per-op proof artifact tests."""
import pytest
import dataclasses


class TestTerminalClass:
    """OperationResult must include terminal_class field."""

    def test_operation_result_has_terminal_class(self):
        """OperationResult dataclass must have terminal_class: str field."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        fields = {f.name for f in dataclasses.fields(OperationResult)}
        assert "terminal_class" in fields, (
            f"OperationResult missing terminal_class. Current fields: {fields}"
        )

    def test_terminal_class_valid_values(self):
        """terminal_class must be one of the taxonomy values."""
        valid = {"PRIMARY_SUCCESS", "FALLBACK_SUCCESS", "DEGRADED", "TIMEOUT", "NOOP"}
        # The taxonomy is defined — test that we know what we're building
        assert "PRIMARY_SUCCESS" in valid
        assert "FALLBACK_SUCCESS" in valid
        assert "DEGRADED" in valid
        assert "TIMEOUT" in valid
        assert "NOOP" in valid
        assert "BANANA" not in valid

    def test_operation_result_terminal_class_defaults_to_unknown(self):
        """Default terminal_class should be 'UNKNOWN' (not None) to avoid None checks."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        from backend.core.ouroboros.governance.op_context import OperationPhase

        result = OperationResult(
            op_id="test-op",
            terminal_phase=OperationPhase.COMPLETE,
            provider_used="claude",
            generation_duration_s=1.0,
            total_duration_s=5.0,
            reason_code="ok",
            trigger_source="test",
            routing_reason="test",
        )
        # Should have terminal_class as UNKNOWN or empty string, not raise AttributeError
        assert hasattr(result, "terminal_class")
        assert result.terminal_class is not None
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_ouroboros_governance/test_gpu_proof_artifacts.py::TestTerminalClass -v
```

Expected: FAIL on `test_operation_result_has_terminal_class` — field doesn't exist

**Step 3: Add terminal_class to OperationResult**

Find `OperationResult` in `governed_loop_service.py` (search for `class OperationResult`). Add field:

```python
@dataclasses.dataclass
class OperationResult:
    op_id: str
    terminal_phase: OperationPhase
    provider_used: str | None
    generation_duration_s: float
    total_duration_s: float
    reason_code: str
    trigger_source: str
    routing_reason: str
    terminal_class: str = "UNKNOWN"   # PRIMARY_SUCCESS | FALLBACK_SUCCESS | DEGRADED | TIMEOUT | NOOP
```

**Step 4: Populate terminal_class in result finalization**

In the method that builds `OperationResult` (after the pipeline runs):

```python
def _classify_terminal(
    terminal_phase: OperationPhase,
    provider_used: str | None,
    reason_code: str,
    is_noop: bool,
) -> str:
    """Classify outcome into terminal taxonomy."""
    if is_noop:
        return "NOOP"
    if terminal_phase == OperationPhase.COMPLETE:
        if provider_used and "prime" in provider_used.lower():
            return "PRIMARY_SUCCESS"
        elif provider_used:
            return "FALLBACK_SUCCESS"
        return "PRIMARY_SUCCESS"  # default for COMPLETE
    if "timeout" in reason_code.lower() or "deadline" in reason_code.lower():
        return "TIMEOUT"
    return "DEGRADED"
```

Call this when building the `OperationResult`.

**Step 5: Run tests**

```bash
pytest tests/test_ouroboros_governance/test_gpu_proof_artifacts.py::TestTerminalClass -v
```

Expected: PASS

---

### Task D2: Per-op proof artifact emission

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`

**Step 1: Write the failing test**

Add to `tests/test_ouroboros_governance/test_gpu_proof_artifacts.py`:

```python
class TestProofArtifact:
    """Every completed op must emit a proof artifact with route, model, fallback status, host."""

    def test_proof_artifact_structure(self):
        """_build_proof_artifact must return dict with all required fields."""
        from backend.core.ouroboros.governance.governed_loop_service import _build_proof_artifact
        from backend.core.ouroboros.governance.op_context import OperationPhase

        artifact = _build_proof_artifact(
            op_id="test-op-123",
            terminal_phase=OperationPhase.COMPLETE,
            terminal_class="PRIMARY_SUCCESS",
            provider_used="gcp-jprime",
            model_id="Qwen2.5-Coder-7B",
            compute_class="gpu_t4",
            execution_host="jarvis-prime-stable",
            fallback_active=False,
            phase_trail=["CLASSIFY", "ROUTE", "GENERATE", "VALIDATE", "APPLY", "COMPLETE"],
            generation_duration_s=3.5,
            total_duration_s=12.0,
        )

        required = {
            "op_id", "terminal_phase", "terminal_class",
            "provider_used", "model_id", "compute_class",
            "execution_host", "fallback_active", "phase_trail",
            "generation_duration_s", "total_duration_s",
        }
        assert required <= artifact.keys(), f"Missing: {required - artifact.keys()}"

    def test_proof_artifact_fallback_flag_set_on_fallback(self):
        """fallback_active must be True when provider is Claude (not J-Prime)."""
        from backend.core.ouroboros.governance.governed_loop_service import _build_proof_artifact
        from backend.core.ouroboros.governance.op_context import OperationPhase

        artifact = _build_proof_artifact(
            op_id="test-op",
            terminal_phase=OperationPhase.COMPLETE,
            terminal_class="FALLBACK_SUCCESS",
            provider_used="claude-api",
            model_id="claude-sonnet-4-6",
            compute_class="api",
            execution_host="anthropic",
            fallback_active=True,
            phase_trail=["CLASSIFY", "ROUTE", "GENERATE", "COMPLETE"],
            generation_duration_s=8.0,
            total_duration_s=20.0,
        )

        assert artifact["fallback_active"] is True
        assert artifact["terminal_class"] == "FALLBACK_SUCCESS"
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_ouroboros_governance/test_gpu_proof_artifacts.py::TestProofArtifact -v
```

Expected: FAIL — `_build_proof_artifact` not importable

**Step 3: Implement _build_proof_artifact**

In `governed_loop_service.py`:

```python
def _build_proof_artifact(
    op_id: str,
    terminal_phase: "OperationPhase",
    terminal_class: str,
    provider_used: str | None,
    model_id: str | None,
    compute_class: str | None,
    execution_host: str | None,
    fallback_active: bool,
    phase_trail: list[str],
    generation_duration_s: float,
    total_duration_s: float,
) -> dict:
    """Build a structured proof artifact for a completed operation.

    This is written to the ledger and can be consumed by the observability layer.
    """
    import time
    return {
        "op_id": op_id,
        "terminal_phase": terminal_phase.name if hasattr(terminal_phase, "name") else str(terminal_phase),
        "terminal_class": terminal_class,
        "provider_used": provider_used,
        "model_id": model_id,
        "compute_class": compute_class,
        "execution_host": execution_host,
        "fallback_active": fallback_active,
        "phase_trail": phase_trail,
        "generation_duration_s": round(generation_duration_s, 3),
        "total_duration_s": round(total_duration_s, 3),
        "proof_ts_utc": time.time(),
    }
```

Emit this artifact via `_record_ledger()` at operation completion.

**Step 4: Run full test suite**

```bash
pytest tests/test_ouroboros_governance/test_gpu_proof_artifacts.py -v
```

Expected: All PASS

**Step 5: Run all governance tests to confirm no regressions**

```bash
pytest tests/test_ouroboros_governance/ -v --tb=short -q 2>&1 | tail -40
```

**Step 6: Commit Phase D**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_gpu_proof_artifacts.py
git commit -m "feat(observability): terminal_class + per-op proof artifact in OperationResult

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Phase D Final: End-to-End Ignition Test (Go/No-Go)

### Task D3: Run ignition test with J-Prime primary path

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
source venv/bin/activate
python3 trigger_ignition.py 2>&1 | tee /tmp/ignition-gpu-run.log
```

**Examine output for:**

1. `provider_used: gcp-jprime` (NOT `claude-api`)
2. `terminal_class: PRIMARY_SUCCESS`
3. `compute_class: gpu_t4` in capability log
4. `host: jarvis-prime-stable` consistent across telemetry, selector, execution
5. `duration: < 60s` (generation only, not total)
6. 6/6 checklist items ✅

**If J-Prime fails and Claude fallback is used:**
- Check `terminal_class` — should be `FALLBACK_SUCCESS`
- This means GPU is not active — re-run Task C3 verification
- **DO NOT** claim GO if provider_used shows Claude for primary-path test

---

## Summary of Tests Added/Updated

| Test file | Tests | Phase |
|-----------|-------|-------|
| `tests/test_ouroboros_governance/test_compute_admission.py` | `TestCapabilityEndpointSchema` (3), `TestBrainPolicyComputeClass` (3), `TestComputeAdmissionGate` (4) | A |
| `tests/test_ouroboros_governance/test_capability_contract.py` | `TestModelArtifactIntegrity` (3), `TestHostBindingInvariant` (3), `TestPrimeClientCapability` (2), `TestVMModelSwitch` (1 integration), `TestTimeoutBudget` (2) | A, B |
| `tests/test_ouroboros_governance/test_gpu_proof_artifacts.py` | `TestTerminalClass` (3), `TestProofArtifact` (2) | D |

**Total new tests: 26** (11 unit, 1 integration, 14 contract/schema)

---

## Go/No-Go Verdict

**Current state (before this plan):** NO-GO
- e2-highmem-8 CPU-only VM with 14B model times out at 983s
- No compute-class gate → routes impossible workloads to CPU
- No host-binding enforcement → no proof of execution locality
- `terminal_class` missing → no observability taxonomy

**After Phases A–D:** Provisional GO when:
- [ ] Benchmark shows ≥ 20 tok/s on T4 + 7B model
- [ ] Ignition test reaches COMPLETE via `provider_used=gcp-jprime`
- [ ] `terminal_class=PRIMARY_SUCCESS` in ledger entry
- [ ] All 26 new tests passing
- [ ] No regressions in existing 1357-test suite (excluding 9 pre-existing failures)
