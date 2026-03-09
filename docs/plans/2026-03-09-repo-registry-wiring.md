# RepoRegistry → OrchestratorConfig Wiring Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire `RepoRegistry.from_env()` into `OrchestratorConfig` so cross-repo saga operations resolve actual filesystem paths for jarvis/prime/reactor-core instead of all pointing at the same `project_root`.

**Architecture:** Three tightly-coupled changes across `orchestrator.py`, `governed_loop_service.py`, and `registry.py`. `OrchestratorConfig` gains an optional `repo_registry` field; `_build_components()` builds the registry from env and passes it in; the saga apply path resolves per-repo roots with KeyError-safe fallback. Two service-quality fixes (startup probe retention, FALLBACK_ACTIVE→ACTIVE state mapping) land in the same files. Registry gains a legacy env-var alias for reactor-core.

**Tech Stack:** Python 3.9, asyncio, pytest, pytest-asyncio (asyncio_mode=auto — never add @pytest.mark.asyncio)

---

## Task 1: Add `repo_registry` to `OrchestratorConfig` and fix saga root resolution

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py:68-94` (OrchestratorConfig)
- Modify: `backend/core/ouroboros/governance/orchestrator.py:768-769` (_execute_saga_apply)
- Test: `tests/governance/saga/test_orchestrator_repo_roots.py` (new file)

### Step 1: Write the failing tests

Create `tests/governance/saga/test_orchestrator_repo_roots.py`:

```python
"""Tests for OrchestratorConfig repo_registry wiring and saga root resolution."""
from pathlib import Path
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
from backend.core.ouroboros.governance.multi_repo.registry import (
    RepoConfig, RepoRegistry,
)


def _make_registry(tmp_path: Path) -> RepoRegistry:
    jarvis = tmp_path / "jarvis"
    prime = tmp_path / "prime"
    reactor = tmp_path / "reactor-core"
    for p in (jarvis, prime, reactor):
        p.mkdir()
    return RepoRegistry(configs=(
        RepoConfig(name="jarvis", local_path=jarvis, canary_slices=("tests/",)),
        RepoConfig(name="prime", local_path=prime, canary_slices=("tests/",)),
        RepoConfig(name="reactor-core", local_path=reactor, canary_slices=("tests/",)),
    ))


def test_orchestrator_config_accepts_repo_registry(tmp_path):
    """OrchestratorConfig can be constructed with repo_registry."""
    registry = _make_registry(tmp_path)
    cfg = OrchestratorConfig(
        project_root=tmp_path / "jarvis",
        repo_registry=registry,
    )
    assert cfg.repo_registry is registry


def test_orchestrator_config_defaults_registry_to_none(tmp_path):
    """OrchestratorConfig.repo_registry defaults to None (backward compat)."""
    cfg = OrchestratorConfig(project_root=tmp_path)
    assert cfg.repo_registry is None


def test_resolve_repo_roots_uses_registry(tmp_path):
    """When repo_registry is set, _resolve_repo_roots returns per-repo paths."""
    registry = _make_registry(tmp_path)
    cfg = OrchestratorConfig(project_root=tmp_path / "jarvis", repo_registry=registry)
    # Call the helper directly
    roots = cfg.resolve_repo_roots(
        repo_scope=("jarvis", "prime", "reactor-core"),
        op_id="op-test-001",
    )
    assert roots["jarvis"] == tmp_path / "jarvis"
    assert roots["prime"] == tmp_path / "prime"
    assert roots["reactor-core"] == tmp_path / "reactor-core"


def test_resolve_repo_roots_fallback_for_unknown_repo(tmp_path):
    """Missing repo key falls back to project_root with a warning (no KeyError)."""
    registry = _make_registry(tmp_path)
    cfg = OrchestratorConfig(project_root=tmp_path / "jarvis", repo_registry=registry)
    roots = cfg.resolve_repo_roots(
        repo_scope=("jarvis", "unknown_repo"),
        op_id="op-test-002",
    )
    assert roots["jarvis"] == tmp_path / "jarvis"
    assert roots["unknown_repo"] == tmp_path / "jarvis"  # fallback to project_root


def test_resolve_repo_roots_no_registry_uses_project_root(tmp_path):
    """When repo_registry is None, all repos resolve to project_root."""
    cfg = OrchestratorConfig(project_root=tmp_path)
    roots = cfg.resolve_repo_roots(
        repo_scope=("jarvis", "prime"),
        op_id="op-test-003",
    )
    assert roots["jarvis"] == tmp_path
    assert roots["prime"] == tmp_path
```

### Step 2: Run to verify tests fail

```bash
python3 -m pytest tests/governance/saga/test_orchestrator_repo_roots.py -v --tb=short
```
Expected: ImportError or AttributeError — `repo_registry` not on `OrchestratorConfig` yet.

### Step 3: Implement

In `backend/core/ouroboros/governance/orchestrator.py`:

**3a. Add import at top of file (after existing imports, before logger):**
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry
```

**3b. Add `repo_registry` field and `resolve_repo_roots()` method to `OrchestratorConfig` (lines 68–94):**

Replace the existing `OrchestratorConfig` class with:
```python
@dataclass(frozen=True)
class OrchestratorConfig:
    """Frozen configuration for the governed pipeline orchestrator.

    Parameters
    ----------
    project_root:
        Root directory of the project being modified (jarvis repo).
    repo_registry:
        Optional multi-repo registry. When set, cross-repo saga applies
        resolve each repo's local_path from the registry instead of using
        project_root for all repos. Defaults to None (single-repo mode).
    generation_timeout_s:
        Maximum seconds for candidate generation (per attempt).
    validation_timeout_s:
        Maximum seconds for candidate validation (per attempt).
    approval_timeout_s:
        Maximum seconds to wait for human approval.
    max_generate_retries:
        Number of additional generation attempts after the first failure.
    max_validate_retries:
        Number of additional validation attempts after the first failure.
    """

    project_root: Path
    repo_registry: Optional[Any] = None  # RepoRegistry at runtime; Any avoids circular import
    generation_timeout_s: float = 120.0
    validation_timeout_s: float = 60.0
    approval_timeout_s: float = 600.0
    max_generate_retries: int = 1
    max_validate_retries: int = 2

    def resolve_repo_roots(
        self,
        repo_scope: Tuple[str, ...],
        op_id: str,
    ) -> Dict[str, Path]:
        """Resolve per-repo filesystem roots from registry; fallback to project_root.

        Parameters
        ----------
        repo_scope:
            Tuple of repo names from OperationContext.
        op_id:
            Operation ID for structured warning on missing registry keys.

        Returns
        -------
        Dict mapping repo name -> absolute Path.
        Missing keys fall back to project_root with a warning (never raise).
        """
        roots: Dict[str, Path] = {}
        for repo in repo_scope:
            if self.repo_registry is not None:
                try:
                    roots[repo] = Path(self.repo_registry.get(repo).local_path)
                except KeyError:
                    logger.warning(
                        "[OrchestratorConfig] repo=%s not in registry for op_id=%s; "
                        "falling back to project_root=%s",
                        repo, op_id, self.project_root,
                    )
                    roots[repo] = self.project_root
            else:
                roots[repo] = self.project_root
        return roots
```

**3c. Update `_execute_saga_apply()` to call `resolve_repo_roots()` (line 769):**

Replace:
```python
        # Resolve repo roots: all repos map to project_root for now
        repo_roots = {repo: self._config.project_root for repo in ctx.repo_scope}
```
With:
```python
        # Resolve per-repo filesystem roots from registry (fallback to project_root)
        repo_roots = self._config.resolve_repo_roots(
            repo_scope=ctx.repo_scope,
            op_id=ctx.op_id,
        )
```

### Step 4: Run tests

```bash
python3 -m pytest tests/governance/saga/test_orchestrator_repo_roots.py -v --tb=short
```
Expected: 5 PASSED.

### Step 5: Run existing orchestrator tests to confirm no regressions

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py tests/governance/self_dev/ -q --tb=short
```
Expected: 0 failures.

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/governance/saga/test_orchestrator_repo_roots.py
git commit -m "feat(orchestrator): add repo_registry to OrchestratorConfig, resolve per-repo roots in saga apply

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Wire `RepoRegistry.from_env()` into `_build_components()` + add legacy reactor alias

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py:636-648`
- Modify: `backend/core/ouroboros/governance/multi_repo/registry.py:72-79`
- Test: add to `tests/test_ouroboros_governance/test_governed_loop_service.py`

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_governed_loop_service.py` (append at end of file):

```python
class TestGovernedLoopRegistryWiring:
    async def test_build_components_wires_registry(self, tmp_path, monkeypatch):
        """_build_components() passes RepoRegistry to OrchestratorConfig."""
        prime_path = tmp_path / "prime"
        prime_path.mkdir()
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("JARVIS_PRIME_REPO_PATH", str(prime_path))

        config = GovernedLoopConfig(project_root=tmp_path)
        svc = GovernedLoopService(config=config)

        # Patch providers to avoid real network calls
        with patch(
            "backend.core.ouroboros.governance.governed_loop_service.PrimeProvider",
            side_effect=ImportError,
        ):
            await svc.start()

        assert svc._orchestrator is not None
        registry = svc._orchestrator._config.repo_registry
        assert registry is not None
        names = {r.name for r in registry.list_enabled()}
        assert "jarvis" in names
        assert "prime" in names

        await svc.stop()

    def test_reactor_legacy_env_var_wired(self, tmp_path, monkeypatch):
        """REACTOR_CORE_REPO_PATH is accepted as legacy alias for reactor-core."""
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry
        reactor_path = tmp_path / "reactor-core"
        reactor_path.mkdir()
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
        monkeypatch.delenv("JARVIS_REACTOR_REPO_PATH", raising=False)
        monkeypatch.setenv("REACTOR_CORE_REPO_PATH", str(reactor_path))

        registry = RepoRegistry.from_env()
        names = {r.name for r in registry.list_all()}
        assert "reactor-core" in names
        rc = registry.get("reactor-core")
        assert rc.local_path == reactor_path
```

### Step 2: Run to verify they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestGovernedLoopRegistryWiring -v --tb=short
```
Expected: FAIL — registry not wired yet.

### Step 3a: Add legacy `REACTOR_CORE_REPO_PATH` alias to `registry.py`

In `backend/core/ouroboros/governance/multi_repo/registry.py`, replace the reactor-core section (lines 72–79):

```python
        # Optional: reactor-core (canonical var + legacy alias)
        reactor_path = os.environ.get("JARVIS_REACTOR_REPO_PATH") or \
                       os.environ.get("REACTOR_CORE_REPO_PATH")
        if reactor_path:
            configs.append(RepoConfig(
                name="reactor-core",
                local_path=Path(reactor_path),
                canary_slices=("tests/",),
            ))
```

### Step 3b: Wire registry into `_build_components()` in `governed_loop_service.py`

In `_build_components()`, replace the `# Build orchestrator` block (lines 636–648):

```python
        # Build RepoRegistry from environment (always; empty if env vars not set)
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry
        repo_registry = RepoRegistry.from_env()
        logger.info(
            "[GovernedLoop] RepoRegistry enabled repos: %s",
            [r.name for r in repo_registry.list_enabled()],
        )

        # Build orchestrator
        orch_config = OrchestratorConfig(
            project_root=self._config.project_root,
            repo_registry=repo_registry,
            generation_timeout_s=self._config.generation_timeout_s,
            approval_timeout_s=self._config.approval_timeout_s,
        )
        self._orchestrator = GovernedOrchestrator(
            stack=self._stack,
            generator=self._generator,
            approval_provider=self._approval_provider,
            config=orch_config,
            validation_runner=validation_runner,
        )
```

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestGovernedLoopRegistryWiring -v --tb=short
```
Expected: 2 PASSED.

### Step 5: Run full governed_loop_service tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py -q --tb=short
```
Expected: 0 failures.

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        backend/core/ouroboros/governance/multi_repo/registry.py \
        tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "feat(governance): wire RepoRegistry.from_env() into _build_components(), add REACTOR_CORE_REPO_PATH alias

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Retain PrimeProvider on failed startup probe + fix FALLBACK_ACTIVE state

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py:562-578` (startup probe)
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py:279-280` (state mapping)
- Test: `tests/governance/integration/test_governed_loop_startup.py` (new file)

### Step 1: Write the failing tests

Create `tests/governance/integration/test_governed_loop_startup.py`:

```python
"""Acceptance tests for GovernedLoopService startup behavior.

AC1: PrimeProvider retained on failed startup health probe (not dropped)
AC2: FALLBACK_ACTIVE FSM state maps to ServiceState.ACTIVE (not DEGRADED)
AC3: QUEUE_ONLY FSM state still maps to ServiceState.DEGRADED (intentional)
"""
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    ServiceState,
)
from backend.core.ouroboros.governance.candidate_generator import FailbackState


async def test_ac1_prime_provider_retained_on_startup_probe_failure(tmp_path):
    """PrimeProvider is kept even when health_probe() returns False at startup."""
    config = GovernedLoopConfig(project_root=tmp_path)

    mock_prime_client = MagicMock()
    svc = GovernedLoopService(config=config, prime_client=mock_prime_client)

    with patch(
        "backend.core.ouroboros.governance.governed_loop_service.PrimeProvider"
    ) as MockProvider:
        mock_provider_instance = MagicMock()
        mock_provider_instance.health_probe = AsyncMock(return_value=False)
        MockProvider.return_value = mock_provider_instance

        await svc.start()

    # Generator must exist — primary provider was retained, not dropped
    assert svc._generator is not None
    await svc.stop()


async def test_ac2_fallback_active_maps_to_active_state(tmp_path):
    """FALLBACK_ACTIVE FSM state → ServiceState.ACTIVE (GCP-first intentional fallback)."""
    config = GovernedLoopConfig(project_root=tmp_path)
    svc = GovernedLoopService(config=config)

    mock_generator = MagicMock()
    mock_fsm = MagicMock()
    mock_fsm.state = FailbackState.FALLBACK_ACTIVE
    mock_generator.fsm = mock_fsm

    with patch.object(svc, "_build_components", new=AsyncMock()), \
         patch.object(svc, "_reconcile_on_boot", new=AsyncMock()), \
         patch.object(svc, "_register_canary_slices"), \
         patch.object(svc, "_attach_to_stack"):
        svc._generator = mock_generator
        svc._orchestrator = MagicMock()
        svc._approval_provider = MagicMock()
        # Simulate the state determination block
        from backend.core.ouroboros.governance.governed_loop_service import ServiceState
        import time
        svc._state = ServiceState.STARTING
        # Invoke start() and let it run state determination
        await svc.start()

    assert svc.state == ServiceState.ACTIVE, (
        f"Expected ACTIVE for FALLBACK_ACTIVE FSM, got {svc.state}"
    )
    await svc.stop()


async def test_ac3_queue_only_still_maps_to_degraded(tmp_path):
    """QUEUE_ONLY FSM state → ServiceState.DEGRADED (no providers = genuinely degraded)."""
    config = GovernedLoopConfig(project_root=tmp_path)
    svc = GovernedLoopService(config=config)

    mock_generator = MagicMock()
    mock_fsm = MagicMock()
    mock_fsm.state = FailbackState.QUEUE_ONLY
    mock_generator.fsm = mock_fsm

    with patch.object(svc, "_build_components", new=AsyncMock()), \
         patch.object(svc, "_reconcile_on_boot", new=AsyncMock()), \
         patch.object(svc, "_register_canary_slices"), \
         patch.object(svc, "_attach_to_stack"):
        svc._generator = mock_generator
        svc._orchestrator = MagicMock()
        svc._approval_provider = MagicMock()
        svc._state = ServiceState.STARTING
        await svc.start()

    assert svc.state == ServiceState.DEGRADED
    await svc.stop()
```

### Step 2: Run to verify they fail

```bash
python3 -m pytest tests/governance/integration/test_governed_loop_startup.py -v --tb=short
```
Expected: AC1 FAIL (primary dropped on probe failure), AC2 FAIL (FALLBACK_ACTIVE → DEGRADED currently).

### Step 3a: Fix startup probe retention

In `governed_loop_service.py`, replace lines 562–578 (the PrimeProvider build block):

```python
        # Build PrimeProvider if PrimeClient available
        if self._prime_client is not None:
            try:
                from backend.core.ouroboros.governance.providers import (
                    PrimeProvider,
                )

                primary = PrimeProvider(self._prime_client, repo_root=self._config.project_root)
                if await primary.health_probe():
                    logger.info("[GovernedLoop] PrimeProvider: healthy at startup")
                else:
                    logger.warning(
                        "[GovernedLoop] PrimeProvider: unhealthy at startup; "
                        "retained for probe-based recovery"
                    )
                    # Do NOT set primary = None — circuit breaker handles retry
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] PrimeProvider build failed: %s", exc
                )
                primary = None
```

### Step 3b: Fix FALLBACK_ACTIVE state mapping

In `governed_loop_service.py`, replace the state determination block (lines 274–284):

```python
            # Determine state based on provider FSM
            if self._generator is not None:
                fsm_state = self._generator.fsm.state
                if fsm_state is FailbackState.QUEUE_ONLY:
                    self._state = ServiceState.DEGRADED
                elif fsm_state is FailbackState.FALLBACK_ACTIVE:
                    # FALLBACK_ACTIVE = intentional cloud/fallback mode, not degraded
                    self._state = ServiceState.ACTIVE
                    logger.info(
                        "[GovernedLoop] Started: state=ACTIVE (fallback_active_intentional)"
                    )
                else:
                    self._state = ServiceState.ACTIVE
            else:
                self._state = ServiceState.DEGRADED
```

Note: remove the `logger.info` at line 286 (the original one) since we now log inside each branch. Or keep it — check what the surrounding code looks like and make the minimal change to avoid duplicate logs.

### Step 4: Run startup tests

```bash
python3 -m pytest tests/governance/integration/test_governed_loop_startup.py -v --tb=short
```
Expected: 3 PASSED.

### Step 5: Run full governance tests to confirm no regressions

```bash
python3 -m pytest tests/governance/ tests/test_ouroboros_governance/ -q --tb=short 2>&1 | tail -15
```
Expected: 0 new failures (the pre-existing `test_ac2_miner_always_requires_human_ack` failure is acceptable).

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/integration/test_governed_loop_startup.py
git commit -m "fix(governance): retain PrimeProvider on startup probe failure; FALLBACK_ACTIVE→ACTIVE

GCP-first intent: provider retained for circuit-breaker recovery on probe fail.
FALLBACK_ACTIVE mapped to ACTIVE (intentional cloud fallback, not degradation).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Summary

| Task | Files Changed | New Tests | Effect |
|------|--------------|-----------|--------|
| 1 | `orchestrator.py` | 5 | Cross-repo saga resolves actual per-repo paths |
| 2 | `governed_loop_service.py`, `registry.py` | 2 | Registry built from env and wired to orchestrator; legacy `REACTOR_CORE_REPO_PATH` alias added |
| 3 | `governed_loop_service.py` | 3 | J-Prime retained on startup; no false DEGRADED for GCP-first mode |

**After these 3 tasks, set in your shell profile:**
```bash
export JARVIS_REPO_PATH=/Users/djrussell23/Documents/repos/JARVIS-AI-Agent
export JARVIS_PRIME_REPO_PATH=/Users/djrussell23/Documents/repos/jarvis-prime
export JARVIS_REACTOR_REPO_PATH=/Users/djrussell23/Documents/repos/reactor-core
# legacy alias (some modules still read this):
export REACTOR_CORE_REPO_PATH=/Users/djrussell23/Documents/repos/reactor-core
```
