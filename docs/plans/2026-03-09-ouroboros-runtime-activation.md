# Ouroboros Runtime Activation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Four targeted fixes that complete Ouroboros runtime activation: env-var documentation, Zone 6.8 cancellation safety, comm transport observability with configurable debounce, and autonomy policy seeding at startup.

**Architecture:** Four files change (`unified_supervisor.py`, `backend/core/ouroboros/governance/integration.py`, `backend/core/ouroboros/governance/governed_loop_service.py`, `.env.example`). Two test files gain new cases (`test_governed_loop_service.py`, `test_integration.py`). No new modules. No new dependencies. All changes are strictly additive or targeted substitutions.

**Tech Stack:** Python 3.9+, `asyncio`, `os.environ`, `pytest` with `asyncio_mode=auto`

**Invariants:**
- `asyncio_mode=auto` is active — NEVER use `@pytest.mark.asyncio`. Always run with `python3 -m pytest`.
- `unified_supervisor.py` is 100 000+ lines — always grep before editing, never rely on remembered line numbers.
- The `.env.example` insert goes BEFORE the final `# END OF CONFIGURATION` comment.

---

## Task 1: .env.example — Add Ouroboros Governance Section

**Files:**
- Modify: `.env.example` (insert before the final `# END OF CONFIGURATION` block)

**No test required** — this is documentation only.

**Step 1: Find exact insertion point.**

```bash
grep -n "END OF CONFIGURATION" .env.example
```

The last two lines of the file are:
```
# ============================================================================
# END OF CONFIGURATION
# ============================================================================
```

Insert the new section immediately before the `# ====...END OF CONFIGURATION` separator.

**Step 2: Add the section.**

The block to insert:

```
# ============================================================================
# OUROBOROS SELF-PROGRAMMING GOVERNANCE
# ============================================================================

# --- Repo Paths (critical for multi-repo patching) ---
# Absolute path to the JARVIS repo (required; defaults to "." if unset)
JARVIS_REPO_PATH=/Users/yourname/repos/JARVIS-AI-Agent

# Absolute path to the J-Prime repo (optional; omit to disable Prime patching)
JARVIS_PRIME_REPO_PATH=/Users/yourname/repos/J-Prime

# Absolute path to the Reactor-Core repo (optional; omit to disable Reactor patching)
JARVIS_REACTOR_REPO_PATH=/Users/yourname/repos/reactor-core

# --- Governed Loop Core ---
OUROBOROS_LEDGER_DIR=~/.jarvis/ouroboros/ledger
OUROBOROS_STARTUP_TIMEOUT=30
OUROBOROS_COMPONENT_BUDGET=5
OUROBOROS_GCP_DAILY_BUDGET=10.0
OUROBOROS_CANARY_SLICES=backend/core/ouroboros/

# --- Provider config ---
JARVIS_GOVERNED_CLAUDE_MODEL=claude-sonnet-4-20250514
JARVIS_GOVERNED_CLAUDE_MAX_COST_PER_OP=0.50
JARVIS_GOVERNED_CLAUDE_DAILY_BUDGET=10.00
JARVIS_GOVERNED_GENERATION_TIMEOUT=120.0
JARVIS_GOVERNED_APPROVAL_TIMEOUT=600.0
JARVIS_GOVERNED_HEALTH_PROBE_INTERVAL=30.0
JARVIS_GOVERNED_MAX_CONCURRENT_OPS=2
JARVIS_COLD_START_GRACE_S=300
JARVIS_PIPELINE_TIMEOUT_S=600.0
JARVIS_APPROVAL_TTL_S=1800

# --- ResourceMonitor thresholds ---
OUROBOROS_RAM_ELEVATED_PCT=80
OUROBOROS_RAM_CRITICAL_PCT=85
OUROBOROS_RAM_EMERGENCY_PCT=90
OUROBOROS_CPU_ELEVATED_PCT=70
OUROBOROS_CPU_CRITICAL_PCT=80
OUROBOROS_CPU_EMERGENCY_PCT=95
OUROBOROS_LATENCY_ELEVATED_MS=40
OUROBOROS_LATENCY_CRITICAL_MS=100

# --- Voice narration debounce (seconds between narrations) ---
OUROBOROS_VOICE_DEBOUNCE_S=60

```

**Step 3: Verify.**

```bash
grep -c "OUROBOROS_VOICE_DEBOUNCE_S" .env.example   # must return 1
tail -5 .env.example                                  # must end with END OF CONFIGURATION
grep -c "JARVIS_REPO_PATH" .env.example              # must return 1
```

**Step 4: Commit.**

```bash
git add .env.example
git commit -m "docs(config): add Ouroboros governance env-var section to .env.example"
```

---

## Task 2: Zone 6.8 Error Swallow Fix in `unified_supervisor.py`

**Files:**
- Modify: `unified_supervisor.py` (targeted substitution — ~7 lines)

**Step 1: Verify exact lines before editing.**

```bash
grep -n "BaseException as exc\|Zone 6.8 governed loop failed\|governed loop FAILED" unified_supervisor.py
```

Expected (current broken state):
```
86012:                                except BaseException as exc:
86016:                                    self.logger.warning(
86017:                                        "[Kernel] Zone 6.8 governed loop failed: %s -- skipped",
```

Note exact line numbers — they may have shifted if earlier edits changed the file. Always use the grep result, not this plan's line numbers.

**Step 2: Replace the except block.**

Current (remove):
```python
                                except BaseException as exc:
                                    # BaseException catches CancelledError (Python 3.9+)
                                    # and TimeoutError from wait_for
                                    self._governed_loop = None
                                    self.logger.warning(
                                        "[Kernel] Zone 6.8 governed loop failed: %s -- skipped",
                                        exc,
                                    )
```

Replace with:
```python
                                except (asyncio.CancelledError, KeyboardInterrupt):
                                    raise
                                except BaseException as exc:
                                    self._governed_loop = None
                                    self.logger.critical(
                                        "[Kernel] Zone 6.8 governed loop FAILED to start: %r — "
                                        "Ouroboros is OFFLINE. Check traceback below for root cause.",
                                        exc,
                                        exc_info=True,
                                    )
```

**Why this is correct:** `asyncio.wait_for()` on timeout raises `asyncio.TimeoutError` (subclass of `Exception`), not `CancelledError`. So a startup timeout falls through to `except BaseException` and logs CRITICAL. A supervisor-level cancellation (CancelledError) propagates out, preventing ghost-ACTIVE state.

**Step 3: Verify after edit.**

```bash
grep -n "CancelledError, KeyboardInterrupt\|Ouroboros is OFFLINE\|exc_info=True" unified_supervisor.py
```

All three patterns must appear in consecutive lines near the original location.

**Step 4: Run tests.**

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```

Expected: 791 passed (no new tests for this task — the monolith is not unit-testable in isolation; behavioral correctness is verified by existing GovernedLoopService tests).

**Step 5: Commit.**

```bash
git add unified_supervisor.py
git commit -m "fix(supervisor): re-raise CancelledError from Zone 6.8, log CRITICAL on GLS start failure"
```

---

## Task 3: Comm Transport Visibility + Configurable VoiceNarrator Debounce

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `tests/test_ouroboros_governance/test_integration.py` (append new class)

### Step 1: Read current `_build_comm_protocol` in integration.py.

```bash
grep -n "def _build_comm_protocol\|logger.debug.*Transport\|logger.debug.*VoiceNarrator\|logger.debug.*OpsLogger\|debounce_s=60" \
    backend/core/ouroboros/governance/integration.py
```

Confirm `import os` is already present:
```bash
grep -n "^import os" backend/core/ouroboros/governance/integration.py
```

### Step 2: Write the failing tests first.

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
# ---------------------------------------------------------------------------
# TestBuildCommProtocolTransportWiring
# ---------------------------------------------------------------------------


class TestBuildCommProtocolTransportWiring:
    """Tests for _build_comm_protocol() transport stack wiring."""

    def test_voice_narrator_debounce_from_env(self, monkeypatch):
        """OUROBOROS_VOICE_DEBOUNCE_S env var controls VoiceNarrator debounce."""
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("OUROBOROS_VOICE_DEBOUNCE_S", "5")
        mock_voice_narrator_cls = MagicMock()
        mock_safe_say = MagicMock()

        with patch.dict("sys.modules", {
            "backend.core.ouroboros.governance.comms.voice_narrator": MagicMock(
                VoiceNarrator=mock_voice_narrator_cls
            ),
            "backend.core.supervisor.unified_voice_orchestrator": MagicMock(
                safe_say=mock_safe_say
            ),
            "backend.core.ouroboros.governance.tui_transport": MagicMock(
                TUITransport=MagicMock
            ),
            "backend.core.ouroboros.governance.comms.ops_logger": MagicMock(
                OpsLogger=MagicMock
            ),
        }):
            import importlib
            import backend.core.ouroboros.governance.integration as _int_mod
            importlib.reload(_int_mod)
            _int_mod._build_comm_protocol()

        mock_voice_narrator_cls.assert_called_once_with(
            say_fn=mock_safe_say, debounce_s=5.0, source="ouroboros"
        )

    def test_log_transport_always_first(self, monkeypatch):
        """LogTransport is always the first transport regardless of others."""
        from unittest.mock import MagicMock, patch
        from backend.core.ouroboros.governance.comm_protocol import LogTransport

        with patch.dict("sys.modules", {
            "backend.core.ouroboros.governance.tui_transport": MagicMock(
                TUITransport=MagicMock
            ),
            "backend.core.ouroboros.governance.comms.ops_logger": MagicMock(
                OpsLogger=MagicMock
            ),
            "backend.core.ouroboros.governance.comms.voice_narrator": MagicMock(
                VoiceNarrator=MagicMock
            ),
            "backend.core.supervisor.unified_voice_orchestrator": MagicMock(
                safe_say=MagicMock()
            ),
        }):
            import importlib
            import backend.core.ouroboros.governance.integration as _int_mod
            importlib.reload(_int_mod)
            protocol = _int_mod._build_comm_protocol()

        assert isinstance(protocol._transports[0], LogTransport)
```

### Step 3: Run tests to verify they fail.

```bash
python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestBuildCommProtocolTransportWiring -q
```

Expected: FAIL (debounce test fails because `_build_comm_protocol` still uses hardcoded `60.0`).

### Step 4: Edit `_build_comm_protocol` in `integration.py`.

Make these targeted changes:

1. Change `logger.debug("[Integration] TUITransport added to CommProtocol")` → `logger.info(...)`
2. Change `logger.debug("[Integration] VoiceNarrator added to CommProtocol")` → `logger.info(...)`
3. Change `logger.debug("[Integration] OpsLogger added to CommProtocol")` → `logger.info(...)`
4. Replace `VoiceNarrator(say_fn=safe_say, debounce_s=60.0, source="ouroboros")` with:
   ```python
   _debounce = float(os.environ.get("OUROBOROS_VOICE_DEBOUNCE_S", "60.0"))
   transports.append(VoiceNarrator(say_fn=safe_say, debounce_s=_debounce, source="ouroboros"))
   ```
   (This is a two-line replace: remove the single `transports.append(VoiceNarrator(...))` line, add the two-line version)

5. Add a summary log after the `if extra_transports:` block, immediately before `return CommProtocol(...)`:
   ```python
   logger.info(
       "[Integration] CommProtocol transport stack: %s",
       [type(t).__name__ for t in transports],
   )
   ```

### Step 5: Run tests to verify they pass.

```bash
python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestBuildCommProtocolTransportWiring -q
```

Expected: 2 passed.

Then run full suite:
```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```

Expected: 793+ passed (791 baseline + 2 new).

### Step 6: Commit.

```bash
git add backend/core/ouroboros/governance/integration.py \
        tests/test_ouroboros_governance/test_integration.py
git commit -m "fix(governance): elevate comm transport logs to INFO, make VoiceNarrator debounce configurable via env"
```

---

## Task 4: Autonomy Policy Seeding in `governed_loop_service.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Modify: `tests/test_ouroboros_governance/test_governed_loop_service.py` (append new class)

### Step 1: Write failing tests first.

Find current line count of test file:
```bash
wc -l tests/test_ouroboros_governance/test_governed_loop_service.py
```

Append at end of `tests/test_ouroboros_governance/test_governed_loop_service.py`:

```python
# ---------------------------------------------------------------------------
# TestSeedAutonomyPolicies
# ---------------------------------------------------------------------------


class TestSeedAutonomyPolicies:
    """Unit tests for GovernedLoopService._seed_autonomy_policies()."""

    def _make_service_with_registry(self, repos: list):
        """Build a GovernedLoopService with a mock registry containing named repos."""
        from unittest.mock import MagicMock
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        mock_registry = MagicMock()
        mock_repos = []
        for name in repos:
            r = MagicMock()
            r.name = name
            mock_repos.append(r)
        mock_registry.list_enabled.return_value = mock_repos
        service._repo_registry = mock_registry
        return service

    def test_seeds_governed_for_tests_slice(self):
        """tests/ canary slice seeds GOVERNED tier for all trigger sources."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        assert service._trust_graduator is not None
        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger, repo="jarvis", canary_slice="tests/"
            )
            assert cfg is not None, f"No config for trigger={trigger}, slice=tests/"
            assert cfg.current_tier is AutonomyTier.GOVERNED

    def test_seeds_governed_for_docs_slice(self):
        """docs/ canary slice seeds GOVERNED tier for all trigger sources."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger, repo="jarvis", canary_slice="docs/"
            )
            assert cfg is not None
            assert cfg.current_tier is AutonomyTier.GOVERNED

    def test_seeds_observe_for_core_slice(self):
        """backend/core/ seeds OBSERVE tier for all trigger sources."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger,
                repo="jarvis",
                canary_slice="backend/core/",
            )
            assert cfg is not None
            assert cfg.current_tier is AutonomyTier.OBSERVE

    def test_seeds_observe_for_unclassified_root(self):
        """Empty canary_slice (root default) seeds OBSERVE tier."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger, repo="jarvis", canary_slice=""
            )
            assert cfg is not None
            assert cfg.current_tier is AutonomyTier.OBSERVE

    def test_seeds_all_registered_repos(self):
        """All repos in registry get policies seeded."""
        repos = ["jarvis", "prime", "reactor-core"]
        service = self._make_service_with_registry(repos)
        service._seed_autonomy_policies()

        all_configs = service._trust_graduator.all_configs()
        repos_covered = {cfg.repo for cfg in all_configs}
        assert repos_covered == set(repos)

    def test_seeds_all_trigger_sources(self):
        """All four trigger sources are seeded per slice per repo."""
        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        triggers_covered = {cfg.trigger_source for cfg in service._trust_graduator.all_configs()}
        assert triggers_covered == {"voice_command", "backlog", "test_failure", "opportunity_miner"}

    def test_seed_count_is_deterministic(self):
        """4 repos * 4 triggers * 4 slices = 64 configs."""
        repos = ["jarvis", "prime", "reactor-core", "extra"]
        service = self._make_service_with_registry(repos)
        service._seed_autonomy_policies()

        # 4 slices: "tests/", "docs/", "backend/core/", ""
        # 4 trigger sources, 4 repos
        assert len(service._trust_graduator.all_configs()) == 64

    def test_fallback_to_jarvis_when_no_registry(self):
        """When _repo_registry is None, seeds policies for 'jarvis' only."""
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        service._repo_registry = None

        service._seed_autonomy_policies()

        repos_covered = {cfg.repo for cfg in service._trust_graduator.all_configs()}
        assert repos_covered == {"jarvis"}

    async def test_seed_called_during_start(self):
        """_seed_autonomy_policies() is called during start(), populating _trust_graduator."""
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        assert service._trust_graduator is None

        await service.start()
        assert service._trust_graduator is not None
        assert len(service._trust_graduator.all_configs()) > 0
        await service.stop()

    def test_seeding_is_idempotent(self):
        """Calling _seed_autonomy_policies() twice produces the same count (fresh graduator each time)."""
        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()
        count_first = len(service._trust_graduator.all_configs())

        service._seed_autonomy_policies()
        count_second = len(service._trust_graduator.all_configs())

        assert count_first == count_second
```

### Step 2: Run tests to verify they fail.

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestSeedAutonomyPolicies -q
```

Expected: FAIL with `AttributeError: 'GovernedLoopService' object has no attribute '_trust_graduator'`.

### Step 3: Add `self._trust_graduator` to `__init__` in `governed_loop_service.py`.

Find the `__init__` attribute block:
```bash
grep -n "self._repo_registry\|self._active_ops\|self._trust_graduator" \
    backend/core/ouroboros/governance/governed_loop_service.py
```

After the line `self._repo_registry: Optional[Any] = None`, add:
```python
        self._trust_graduator: Optional[Any] = None
```

### Step 4: Add `_seed_autonomy_policies` method.

Find insertion point:
```bash
grep -n "def _register_canary_slices\|def _attach_to_stack" \
    backend/core/ouroboros/governance/governed_loop_service.py
```

Insert the following method BETWEEN `_register_canary_slices` and `_attach_to_stack`:

```python
    def _seed_autonomy_policies(self) -> None:
        """Seed baseline SignalAutonomyConfig per repo x trigger_source x canary_slice.

        Default tiers:
          tests/            -> GOVERNED  (test-only changes run without human approval)
          docs/             -> GOVERNED  (doc patches run without human approval)
          backend/core/     -> OBSERVE   (infrastructure changes require voice confirmation)
          "" (root default) -> OBSERVE   (unclassified root-level changes default to safe)

        Tiers are seeded conservatively; TrustGraduator.promote() advances them
        automatically as operational track record accumulates.
        """
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        from backend.core.ouroboros.governance.autonomy.tiers import (
            AutonomyTier,
            GraduationMetrics,
            SignalAutonomyConfig,
            WorkContext,
            CognitiveLoad,
        )

        _TRIGGER_SOURCES = (
            "voice_command",
            "backlog",
            "test_failure",
            "opportunity_miner",
        )
        # canary_slice -> (tier, defer_during_work_context)
        _SLICE_POLICIES = {
            "tests/":        (AutonomyTier.GOVERNED, (WorkContext.MEETINGS,)),
            "docs/":         (AutonomyTier.GOVERNED, (WorkContext.MEETINGS,)),
            "backend/core/": (AutonomyTier.OBSERVE,  (WorkContext.MEETINGS, WorkContext.CODING)),
            "":              (AutonomyTier.OBSERVE,   (WorkContext.MEETINGS, WorkContext.CODING)),
        }

        graduator = TrustGraduator()
        repos = (
            [r.name for r in self._repo_registry.list_enabled()]
            if self._repo_registry is not None
            else ["jarvis"]
        )

        for repo in repos:
            for trigger_source in _TRIGGER_SOURCES:
                for canary_slice, (tier, defer_ctxs) in _SLICE_POLICIES.items():
                    config = SignalAutonomyConfig(
                        trigger_source=trigger_source,
                        repo=repo,
                        canary_slice=canary_slice,
                        current_tier=tier,
                        graduation_metrics=GraduationMetrics(),
                        defer_during_cognitive_load=CognitiveLoad.HIGH,
                        defer_during_work_context=tuple(defer_ctxs),
                        require_user_active=False,
                    )
                    graduator.register(config)

        self._trust_graduator = graduator
        logger.info(
            "[GovernedLoop] Autonomy policies seeded: %d configs across %d repos",
            len(graduator.all_configs()),
            len(repos),
        )
```

### Step 5: Wire `_seed_autonomy_policies()` into `start()`.

Find the exact call sequence:
```bash
grep -n "_register_canary_slices\|_attach_to_stack\|_seed_autonomy_policies" \
    backend/core/ouroboros/governance/governed_loop_service.py
```

Current:
```python
            self._register_canary_slices()
            self._attach_to_stack()
```

Replace with:
```python
            self._register_canary_slices()
            self._seed_autonomy_policies()
            self._attach_to_stack()
```

### Step 6: Verify.

```bash
grep -n "_seed_autonomy_policies\|_trust_graduator" \
    backend/core/ouroboros/governance/governed_loop_service.py
```

Must return at least three matches: `__init__` attribute, `start()` call, and `def _seed_autonomy_policies`.

### Step 7: Run tests to verify they pass.

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestSeedAutonomyPolicies -q
```

Expected: 10 passed.

Then full suite:
```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```

Expected: 801+ passed (791 baseline + 10 new).

### Step 8: Commit.

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "feat(governance): seed baseline autonomy policies at GovernedLoopService startup"
```

---

## Final Verification

After all four tasks complete:

```bash
python3 -m pytest tests/test_ouroboros_governance/ -q
```

801+ tests must pass.

```bash
grep -c "OUROBOROS_VOICE_DEBOUNCE_S" .env.example       # 1
grep -c "JARVIS_REPO_PATH" .env.example                 # 1
grep -n "Ouroboros is OFFLINE" unified_supervisor.py    # 1 (Zone 6.8)
grep -n "CommProtocol transport stack" backend/core/ouroboros/governance/integration.py  # 1
grep -n "_trust_graduator" backend/core/ouroboros/governance/governed_loop_service.py    # 3+
```

---

## Pitfalls

**Task 2 — TimeoutError vs CancelledError:** `asyncio.wait_for()` timeout raises `asyncio.TimeoutError` (subclass of `Exception`). It is NOT caught by `except (asyncio.CancelledError, KeyboardInterrupt): raise` — it correctly falls through to `except BaseException`. This is the intended behavior.

**Task 3 — `importlib.reload` isolation:** The `patch.dict("sys.modules", ...)` approach avoids cross-test leakage. Do not use `importlib.reload` alone without the sys.modules patch — prior import state leaks between tests in the same process.

**Task 4 — `Optional[Any]` type for `_trust_graduator`:** The import of `TrustGraduator` is deferred inside `_seed_autonomy_policies()` to avoid a top-level circular import. Therefore, the attribute type annotation in `__init__` uses `Optional[Any]`. This is intentional.

**Task 4 — `test_seed_count_is_deterministic` arithmetic:** 4 slices × 4 trigger sources × 4 repos = 64. If the slice map in `_seed_autonomy_policies` ever changes, this test catches the regression.
