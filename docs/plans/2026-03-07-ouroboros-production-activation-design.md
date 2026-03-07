# Ouroboros Governance Production Activation — Design Doc

**Date:** 2026-03-07
**Status:** Approved
**Approach:** B — Governance Integration Module + Minimal Supervisor Hooks

---

## 1. Goal

Wire the Ouroboros governance stack (Phases 0-3, 299 tests) into the running JARVIS system. Enable governed self-programming in sandbox mode, register canary slices, provide CLI break-glass commands, and establish the single write-gate authority for all autonomous operations.

## 2. Architecture

All governance logic lives in `backend/core/ouroboros/governance/integration.py`. The 100K+ line `unified_supervisor.py` gets ~15 lines of hook calls at 4 explicit points. The supervisor remains the sole lifecycle authority — the integration module owns governance mechanics.

```
unified_supervisor.py                    governance/integration.py
─────────────────────                    ─────────────────────────
__init__:                                GovernanceConfig (frozen)
  _governance_stack = None               GovernanceStack (lifecycle)
  _governance_mode = PENDING             GovernanceMode (enum)
                                         GovernanceInitError
argparse:
  register_governance_argparse(parser)   register_governance_argparse()

Zone 6.5:                                create_governance_stack()
  stack = create_governance_stack(cfg)     → factory with timeout + rollback
  stack.start()                          GovernanceStack.start()

CLI dispatch:                            handle_break_glass_command()
  handle_break_glass_command(args, stack)   → delegates to cli_commands.py
```

## 3. Non-Negotiable Constraints

1. **No side effects on import** in `governance/integration.py`.
2. **Typed integration contract** — enums for modes, dataclasses for config/results.
3. **Explicit hook points only** — 4 locations in supervisor, no hidden callbacks.
4. **Fail-closed for autonomous writes**, fail-open for interactive core paths.
5. **Single bypass policy** — no alternate path can enable autonomous loops outside supervisor.
6. **Feature flag + kill switch** — `--skip-governance` forces `READ_ONLY_PLANNING`, never full bypass.
7. **Full observability** — reason codes, op_id, policy_version, policy_hash in every decision log.
8. **Double-call safety** — `start()`/`stop()` are idempotent; re-entry is a safe no-op.

## 4. Components

### 4.1 GovernanceMode Enum

```python
class GovernanceMode(enum.Enum):
    PENDING = "pending"                    # Pre-startup
    SANDBOX = "sandbox"                    # Sandboxed execution only
    READ_ONLY_PLANNING = "read_only_planning"  # Fail-closed: no writes
    GOVERNED = "governed"                  # Full governed autonomy
    EMERGENCY_STOP = "emergency_stop"      # All autonomy halted
```

All mode fields use this enum — no string literals anywhere. Eliminates drift bugs.

### 4.2 GovernanceConfig

```python
@dataclass(frozen=True)
class GovernanceConfig:
    # Paths
    ledger_dir: Path              # ~/.jarvis/ouroboros/ledger

    # Policy (immutable during execution)
    policy_version: str           # from POLICY_VERSION constant
    policy_hash: str              # SHA-256 of policy rules for forensic replay
    contract_version: ContractVersion
    contract_hash: str            # SHA-256 of contract schema
    config_digest: str            # SHA-256 of this config for reproducibility

    # Mode
    initial_mode: GovernanceMode  # from CLI: SANDBOX | GOVERNED
    skip_governance: bool         # --skip-governance → forces READ_ONLY_PLANNING

    # Canary slices
    canary_slices: Tuple[str, ...]  # default: ("backend/core/ouroboros/",)

    # Cost guardrails
    gcp_daily_budget: float       # OUROBOROS_GCP_DAILY_BUDGET, default $10

    # Timeouts
    startup_timeout_s: float      # global factory timeout, default 30s
    component_budget_s: float     # per-component init budget, default 5s

    @classmethod
    def from_env_and_args(cls, args) -> "GovernanceConfig":
        """Build from env vars + CLI args. Validates on construction.

        Raises ValueError on invalid config.
        """
```

Frozen so policy can't drift mid-operation. Hashes included for forensic reproducibility.

### 4.3 GovernanceStack

```python
@dataclass
class GovernanceStack:
    # Core (always present)
    controller: SupervisorOuroborosController
    risk_engine: RiskEngine
    ledger: OperationLedger
    comm: CommProtocol
    lock_manager: GovernanceLockManager
    break_glass: BreakGlassManager
    change_engine: ChangeEngine
    resource_monitor: ResourceMonitor
    degradation: DegradationController
    routing: RoutingPolicy
    canary: CanaryController
    contract_checker: RuntimeContractChecker

    # Optional bridges (Protocol-typed for swappability)
    event_bridge: Optional[EventBridge]
    blast_adapter: Optional[BlastRadiusAdapter]
    learning_bridge: Optional[LearningBridge]

    # Metadata
    policy_version: str                          # immutable, pinned at creation
    capabilities: Dict[str, CapabilityStatus]    # reason map, not bool

    _started: bool = False
```

#### Capability Status (not just bool)

```python
@dataclass(frozen=True)
class CapabilityStatus:
    enabled: bool
    reason: str   # "ok", "dep_missing", "init_timeout", "init_error"
```

Boot report shows: `event_bridge: enabled=True reason=ok` or `oracle: enabled=False reason=dep_missing`.

#### Lifecycle Methods

```python
async def start(self) -> None:
    """Start all components. Idempotent — second call is no-op."""
    if self._started:
        return
    await self.controller.start()
    # Register canary slices, wire transports
    self._started = True

async def stop(self) -> None:
    """Graceful shutdown. Idempotent."""
    if not self._started:
        return
    await self.drain()
    await self.controller.stop()
    self._started = False

def health(self) -> Dict[str, Any]:
    """Structured health report for TUI/dashboard."""
    return {
        "mode": self.controller.mode.value,
        "policy_version": self.policy_version,
        "capabilities": {k: {"enabled": v.enabled, "reason": v.reason}
                        for k, v in self.capabilities.items()},
        "degradation_mode": self.degradation.current_mode.name,
        "canary_slices": {p: s.state.value for p, s in self.canary.slices.items()},
        "pending_ops": self.ledger.pending_count if hasattr(self.ledger, 'pending_count') else 0,
        "budget_remaining": self.routing._guardrail.remaining if hasattr(self.routing, '_guardrail') else None,
        "last_contract_check": "ok",
    }

async def drain(self) -> None:
    """Drain in-flight operations before shutdown."""
    # Flush ledger, wait for pending lock releases
```

#### Write Gate — Single Source of Truth

```python
def can_write(self, op_context: Dict[str, Any]) -> Tuple[bool, str]:
    """Single authority for all autonomous write decisions.

    Returns (allowed, reason_code). ALL write paths must call this.
    No alternate path can enable writes outside this gate.
    """
    if not self._started:
        return False, "governance_not_started"
    if not self.controller.writes_allowed:
        return False, f"mode_{self.controller.mode.value}"
    if self.degradation.current_mode.value > 1:  # REDUCED or worse
        return False, f"degradation_{self.degradation.current_mode.name}"
    # Check canary slice
    files = op_context.get("files", [])
    for f in files:
        if not self.canary.is_file_allowed(str(f)):
            return False, f"canary_not_promoted:{f}"
    # Check runtime contract
    proposed_version = op_context.get("proposed_contract_version")
    if proposed_version and not self.contract_checker.check_before_write(proposed_version):
        return False, "contract_incompatible"
    return True, "ok"
```

#### Deterministic Replay

```python
def replay_decision(self, op_id: str) -> Optional[Dict[str, Any]]:
    """Reconstruct classification from persisted inputs + policy_version.

    Returns the exact prior decision for forensic audit.
    """
    entry = self.ledger.get_entry(op_id)
    if entry is None:
        return None
    # Re-classify with frozen policy
    profile = OperationProfile(**entry.data.get("profile", {}))
    classification = self.risk_engine.classify(profile)
    return {
        "op_id": op_id,
        "policy_version": self.policy_version,
        "original_state": entry.state.value,
        "replayed_tier": classification.tier.name,
        "replayed_reason": classification.reason_code,
        "match": classification.tier.name == entry.data.get("risk_tier"),
    }
```

### 4.4 Factory — create_governance_stack()

```python
class GovernanceInitError(Exception):
    """Raised when governance stack creation fails."""
    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}")

async def create_governance_stack(
    config: GovernanceConfig,
    event_bus: Optional[Any] = None,
    oracle: Optional[Any] = None,
    learning_memory: Optional[Any] = None,
) -> GovernanceStack:
    """Factory with global timeout + per-component budgets.

    On failure:
    - Cleans up any partially-created resources
    - Raises GovernanceInitError with reason_code

    Reason codes:
    - governance_init_timeout
    - governance_init_contract_error
    - governance_init_ledger_error
    - governance_init_dep_missing
    """
```

Per-component init has its own budget (`component_budget_s`, default 5s). One slow dependency can't silently consume the global timeout. Partial-init rollback ensures no half-wired stack leaks.

### 4.5 Argparse Registration

```python
def register_governance_argparse(security_group) -> None:
    """Add governance flags to existing security argument group.

    Flags:
    --skip-governance    Force READ_ONLY_PLANNING (never full bypass)
    --governance-mode    {sandbox, governed, safe} (default: sandbox)
    --break-glass        {issue, list, revoke, audit} subcommand
    --break-glass-op-id  Operation ID for issue/revoke
    --break-glass-reason Reason string for issue/revoke
    --break-glass-ttl    TTL in seconds (default 300)
    """
```

`--skip-governance` forces `READ_ONLY_PLANNING`, not disabled. This is the kill switch — it disables autonomous writes while keeping governance observable.

### 4.6 Break-Glass CLI Handler

```python
async def handle_break_glass_command(
    args,
    stack: Optional[GovernanceStack],
) -> int:
    """Dispatch break-glass CLI operations.

    Works even when stack is None (degraded mode):
    - list/audit: return empty with warning
    - issue/revoke: return error with reason

    Returns exit code (0 success, 1 error).
    """
```

Guards against absent stack — prints explicit reason instead of crashing.

## 5. Supervisor Hook Points

### Hook 1: `__init__` (~line 66615)

```python
# After existing autonomy mode flags
self._governance_stack: Optional["GovernanceStack"] = None
self._governance_mode: GovernanceMode = GovernanceMode.PENDING
self._governance_init_reason: str = "pending_startup"
```

3 lines. No imports needed (forward reference string).

### Hook 2: Argparse (~line 97303)

```python
from backend.core.ouroboros.governance.integration import register_governance_argparse
register_governance_argparse(security)
```

2 lines. Adds flags to existing security group.

### Hook 3: Zone 6.5 (~after line 85840)

```python
# ── Governance Gate ──────────────────────────────────
from backend.core.ouroboros.governance.integration import (
    GovernanceConfig, GovernanceMode, create_governance_stack, GovernanceInitError,
)
if not getattr(self._args, "skip_governance", False):
    try:
        _gov_config = GovernanceConfig.from_env_and_args(self._args)
        self._governance_stack = await asyncio.wait_for(
            create_governance_stack(
                _gov_config,
                event_bus=getattr(self, "_cross_repo_event_bus", None),
                oracle=getattr(self, "_codebase_knowledge_graph", None),
                learning_memory=getattr(self, "_learning_memory", None),
            ),
            timeout=_gov_config.startup_timeout_s,
        )
        await self._governance_stack.start()
        self._governance_mode = GovernanceMode(self._governance_stack.controller.mode.value)
        self._governance_init_reason = "ok"
        self.logger.info("[Kernel] Governance gate: %s", self._governance_stack.health())
    except (GovernanceInitError, asyncio.TimeoutError) as exc:
        self._governance_mode = GovernanceMode.READ_ONLY_PLANNING
        self._governance_init_reason = str(exc)
        self.logger.warning("[Kernel] Governance gate failed: %s — READ_ONLY_PLANNING", exc)
else:
    self._governance_mode = GovernanceMode.READ_ONLY_PLANNING
    self._governance_init_reason = "skip_governance_flag"
    self.logger.info("[Kernel] Governance skipped — READ_ONLY_PLANNING")
```

~15 lines. Follows existing autonomy gate pattern exactly.

### Hook 4: CLI Dispatch (in async_main command handling)

```python
if getattr(args, "break_glass_action", None):
    from backend.core.ouroboros.governance.integration import handle_break_glass_command
    _stack = getattr(kernel, "_governance_stack", None) if kernel else None
    sys.exit(await handle_break_glass_command(args, _stack))
```

4 lines. Handles degraded/no-stack case internally.

## 6. Edge Cases

### Supervisor restart during in-flight init
Factory is idempotent — creates fresh components each time. No stale state from prior run. Ledger is append-only and survives restarts.

### Double-start / double-stop
Both are idempotent no-ops (guarded by `_started` flag).

### Contract flapping at boot boundary
RuntimeContractChecker uses the frozen `contract_version` from GovernanceConfig. No re-evaluation during a single boot cycle. Hysteresis is inherent — version is checked once at factory creation.

### Break-glass token replay across restarts
BreakGlassManager stores tokens in memory only (by design). Tokens don't survive restarts — this is a feature, not a bug. Each restart requires fresh issuance with new justification.

## 7. Boot Report

On successful startup, emitted as structured log + TUI message:

```
[Governance] Boot complete:
  mode=SANDBOX  policy_version=2.1.0  policy_hash=a3f8...
  contract=2.1.0  contract_hash=7b2c...  config_digest=e91d...
  capabilities:
    event_bridge: enabled=true reason=ok
    oracle: enabled=false reason=dep_missing
    learning: enabled=true reason=ok
  canary_slices: backend/core/ouroboros/ [PENDING, 0 ops]
  degradation: FULL_AUTONOMY  gcp_budget=$10.00/day remaining=$10.00
  pending_ops=0  transport_queue=0
```

## 8. Files Changed

| File | Change | Lines |
|------|--------|-------|
| `backend/core/ouroboros/governance/integration.py` | **Create** | ~400 |
| `tests/test_ouroboros_governance/test_integration.py` | **Create** | ~300 |
| `unified_supervisor.py` ~line 66615 | **Add** 3 state vars | 3 |
| `unified_supervisor.py` ~line 97303 | **Add** argparse registration | 2 |
| `unified_supervisor.py` ~line 85840 | **Add** governance gate | ~15 |
| `unified_supervisor.py` async_main | **Add** CLI dispatch | 4 |

**Total supervisor changes: ~24 lines across 4 locations.**

## 9. What This Enables

After this wiring is live:
1. Governance starts in SANDBOX mode on every boot
2. Canary slice `backend/core/ouroboros/` begins tracking operations
3. CLI break-glass commands are available (`jarvis --break-glass list`)
4. All autonomous write attempts go through `can_write()` gate
5. After 50+ ops, <5% rollback, <120s p95, 72h stability → canary promotes to ACTIVE
6. Derek signs off → governance mode promoted to GOVERNED
7. Ouroboros self-programming operates under full governance
