# GCP VM Auto-Create and Shutdown Flow

## Your Questions Answered ✅

### Q1: Auto-create when memory > 85%?
**✅ YES - Fully Implemented!**

### Q2: CTRL+C shuts down VMs and shows costs?
**✅ YES - With Enhanced Cost Display!**

---

## Complete Flow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                    LOCAL MACOS - NORMAL OPERATION                 │
│                                                                   │
│  Memory Usage: 60% (9.6GB / 16GB)                                │
│  Status: All components running locally                          │
│  GCP VMs: None                                                   │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │ User opens many apps
                            │ Chrome, Cursor IDE, Docker...
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                    MEMORY PRESSURE INCREASES                      │
│                                                                   │
│  Memory Usage: 87% (13.9GB / 16GB)  ← Exceeds 85% threshold!    │
│  platform_memory_monitor detects: pressure_level = "high"        │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │ Triggers callback
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│              MAIN.PY - memory_pressure_callback()                 │
│                                                                   │
│  1. Detects pressure_level = "high"                              │
│  2. Initializes gcp_vm_manager                                   │
│  3. Gets memory snapshot from platform_memory_monitor            │
│  4. Calls intelligent_gcp_optimizer.should_create_vm()           │
│     • Checks budget: $0.00 / $5.00 ✅                            │
│     • Checks VM limit: 0 / 2 VMs ✅                              │
│     • Analyzes memory pressure: 87% > 85% ✅                     │
│     • Decision: CREATE VM (confidence: 89%)                      │
│                                                                   │
│  5. Calls gcp_vm_manager.create_vm()                             │
│     • Components: ['VISION', 'CHATBOTS']                         │
│     • Trigger: "Memory pressure: high - RAM >85%"                │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │ Creates GCP VM
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                  GCP VM CREATION (30-60 seconds)                  │
│                                                                   │
│  1. Google Compute Engine API:                                   │
│     • Machine: e2-highmem-4 (4 vCPU, 32GB RAM)                   │
│     • Type: Spot VM ($0.029/hour)                                │
│     • Zone: us-central1-a                                        │
│     • Name: jarvis-backend-20251029-143022                       │
│                                                                   │
│  2. VM Starts → gcp_vm_startup.sh runs automatically:            │
│     • apt-get install python3, git, etc.                         │
│     • Clone JARVIS repo                                          │
│     • pip install dependencies                                   │
│     • Start Cloud SQL Proxy                                      │
│     • python3 main.py --port 8010                                │
│                                                                   │
│  3. Backend Ready:                                               │
│     • External IP: http://34.10.137.70:8010                      │
│     • Health check: ✅ HEALTHY                                   │
│                                                                   │
│  4. cost_tracker records:                                        │
│     • Instance ID: 12345678901234567                             │
│     • Created at: 2025-10-29 14:30:22                            │
│     • Components: VISION, CHATBOTS                               │
│     • Cost rate: $0.029/hour                                     │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │ VM Running
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                    HYBRID OPERATION MODE                          │
│                                                                   │
│  LOCAL (macOS):                                                  │
│  • Memory: 65% (10.4GB / 16GB) ← Reduced!                       │
│  • Components: VOICE, VOICE_UNLOCK, WAKE_WORD, MONITORING       │
│                                                                   │
│  CLOUD (GCP VM - 32GB RAM):                                      │
│  • Memory: 28% (9GB / 32GB) ← Plenty of headroom!               │
│  • Components: VISION, CHATBOTS                                  │
│  • Cost: $0.029/hour = $0.00048/minute                           │
│                                                                   │
│  📊 Monitoring Loop (every 30s):                                 │
│  • Health checks VM                                              │
│  • Updates cost: uptime_hours * $0.029                           │
│  • Checks max lifetime (3 hours)                                 │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │ User presses CTRL+C or CMD+DELETE
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                 GRACEFUL SHUTDOWN (main.py lifespan)              │
│                                                                   │
│  🛑 Shutting down JARVIS backend...                              │
│                                                                   │
│  1. Broadcast shutdown to WebSocket clients                      │
│                                                                   │
│  2. Cleanup GCP VM Manager:                                      │
│     🧹 Cleaning up all VMs: Manager shutdown                     │
│                                                                   │
│     For each VM:                                                 │
│       • Update cost: uptime_hours * cost_per_hour               │
│       • 🛑 Terminating VM: jarvis-backend-20251029-143022       │
│       •    Reason: Manager shutdown                              │
│       •    Uptime: 1.47 hours                                    │
│       •    Cost: $0.0427                                         │
│       • Record termination in cost_tracker                       │
│       • Delete VM via Google Compute Engine API                  │
│       • ✅ VM terminated                                         │
│                                                                   │
│     💰 COST SUMMARY (displayed prominently):                     │
│     ============================================================  │
│     💰 GCP VM COST SUMMARY                                       │
│     ============================================================  │
│        VMs Terminated:  1                                        │
│        Total Uptime:    1.47 hours                               │
│        Session Cost:    $0.0427                                  │
│        Total Lifetime:  $0.2145 (all sessions)                   │
│     ============================================================  │
│                                                                   │
│  3. Shutdown Cost Tracking System                                │
│     ✅ Cost Tracking System shutdown complete                    │
│                                                                   │
│  4. Stop other components...                                     │
│                                                                   │
│  ✅ JARVIS stopped gracefully                                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Key Implementation Details

### 1. Auto-Create Trigger (Memory > 85%)

**File:** `backend/core/platform_memory_monitor.py`

```python
# Lines 193-203
if pressure_level == "critical" or snapshot.macos_is_swapping:
    snapshot.pressure_level = "critical"
    snapshot.gcp_shift_recommended = True
    snapshot.gcp_shift_urgent = True

elif pressure_level == "warn":  # This is the 85% threshold!
    snapshot.pressure_level = "high"
    snapshot.gcp_shift_recommended = True
```

**File:** `backend/main.py`

```python
# Lines 850-923
async def memory_pressure_callback(pressure_level: str):
    """Triggers GCP VM creation when memory pressure is high/critical"""

    # Only create VM on high or critical pressure
    if pressure_level not in ['high', 'critical']:
        return

    # Check if VM needed
    should_create, reason, confidence = await gcp_vm_manager.should_create_vm(
        snapshot, trigger_reason=f"Memory pressure: {pressure_level}"
    )

    if should_create:
        # Create VM with components to offload
        vm_instance = await gcp_vm_manager.create_vm(
            components=['VISION', 'CHATBOTS'],  # Or more if critical
            trigger_reason=f"Memory pressure: {pressure_level}",
            metadata={"pressure_level": pressure_level}
        )
```

### 2. CTRL+C Shutdown with Cost Display

**File:** `backend/main.py`

```python
# Lines 1894-1902 - Called when CTRL+C is pressed
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... startup code ...

    yield  # Application runs

    # CTRL+C triggers this cleanup code:

    # Cleanup GCP VM Manager (before cost tracker to finalize costs)
    if gcp_vm_manager:
        logger.info("🧹 Cleaning up GCP VMs...")
        await gcp_vm_manager.cleanup()  # ← Terminates all VMs + shows costs
        logger.info("✅ GCP VM Manager cleanup complete")
```

**File:** `backend/core/gcp_vm_manager.py`

```python
# Lines 602-637 - Enhanced with cost summary
async def cleanup_all_vms(self, reason: str = "System shutdown"):
    """Terminate all managed VMs with cost summary"""

    # Calculate total costs before terminating
    total_session_cost = 0.0
    total_uptime_hours = 0.0
    vm_count = len(self.managed_vms)

    for vm in self.managed_vms.values():
        vm.update_cost()  # Calculate final cost
        total_session_cost += vm.total_cost
        total_uptime_hours += vm.uptime_hours

    # Terminate all VMs (calls terminate_vm for each)
    # ... termination code ...

    # Display prominent cost summary
    logger.info("="*60)
    logger.info("💰 GCP VM COST SUMMARY")
    logger.info("="*60)
    logger.info(f"   VMs Terminated:  {vm_count}")
    logger.info(f"   Total Uptime:    {total_uptime_hours:.2f} hours")
    logger.info(f"   Session Cost:    ${total_session_cost:.4f}")
    logger.info(f"   Total Lifetime:  ${self.stats['total_cost']:.4f}")
    logger.info("="*60)
```

### 3. Per-VM Termination Cost Display

**File:** `backend/core/gcp_vm_manager.py`

```python
# Lines 490-538
async def terminate_vm(self, vm_name: str, reason: str = "Manual termination") -> bool:
    """Terminate a VM instance"""

    # Update cost before termination
    vm.update_cost()

    # Record in cost tracker
    if self.cost_tracker:
        await self.cost_tracker.record_vm_termination(
            instance_id=vm.instance_id,
            reason=reason,
            total_cost=vm.total_cost
        )

    # Delete the VM via Google Compute Engine API
    operation = await asyncio.to_thread(
        self.instances_client.delete,
        project=self.config.project_id,
        zone=self.config.zone,
        instance=vm_name
    )

    await self._wait_for_operation(operation)

    # Display individual VM cost
    logger.info(f"✅ VM terminated: {vm_name}")
    logger.info(f"   Uptime: {vm.uptime_hours:.2f}h")
    logger.info(f"   Cost: ${vm.total_cost:.4f}")  # ← Shows individual cost

    return True
```

---

## Safety Features

### 1. Budget Protection
```python
# Won't create VM if daily budget exceeded
daily_cost = await cost_tracker.get_daily_cost()
if daily_cost >= daily_budget_usd:
    return False, f"Daily budget exceeded: ${daily_cost:.2f}"
```

### 2. VM Count Limits
```python
# Won't create more than max_concurrent_vms (default: 2)
active_vms = len([vm for vm in managed_vms if vm.state == RUNNING])
if active_vms >= max_concurrent_vms:
    return False, f"Max concurrent VMs reached: {active_vms} / {max_concurrent_vms}"
```

### 3. Auto-Termination
```python
# VMs auto-terminate after max_vm_lifetime_hours (default: 3 hours)
if vm.uptime_hours >= max_vm_lifetime_hours:
    await terminate_vm(vm_name, reason="Max lifetime exceeded")
```

### 4. Graceful Shutdown on CTRL+C
- All VMs terminated before exit
- Costs calculated and displayed
- No orphaned VMs running
- Cost tracker records everything

---

## Example Session Output

```bash
$ python main.py

🚀 Starting optimized JARVIS backend...
☁️  GCP VM auto-creation enabled
✅ Memory pressure callback registered
✅ Dynamic component loading enabled

# ... normal operation ...

📊 Memory pressure changed: high
🚀 Creating GCP Spot VM: RAM >85% (confidence: 89%)
⏳ VM creation operation started: operation-123456
✅ VM created successfully: jarvis-backend-20251029-143022
   External IP: 34.10.137.70
   Internal IP: 10.128.0.42
   Cost: $0.029/hour

# ... VM running for 1.47 hours ...

^C  # User presses CTRL+C

🛑 Shutting down JARVIS backend...
🧹 Cleaning up GCP VMs...
🧹 Cleaning up all VMs: Manager shutdown

🛑 Terminating VM: jarvis-backend-20251029-143022 (Reason: Manager shutdown)
✅ VM terminated: jarvis-backend-20251029-143022
   Uptime: 1.47h
   Cost: $0.0427

✅ All VMs cleaned up
============================================================
💰 GCP VM COST SUMMARY
============================================================
   VMs Terminated:  1
   Total Uptime:    1.47 hours
   Session Cost:    $0.0427
   Total Lifetime:  $0.2145
============================================================

✅ GCP VM Manager cleanup complete
✅ Cost Tracking System shutdown complete
✅ JARVIS stopped gracefully
```

---

## Cost Tracking Files

All costs are tracked in these files:
1. `cost_tracker.py` - Persistent database of all VM sessions
2. `gcp_vm_manager.py` - In-memory tracking of current session
3. Log files - Full audit trail

You can also check costs anytime:
```bash
cd backend
python3 core/gcp_vm_status.py --costs
```

---

## Summary

✅ **Auto-create when memory > 85%**: YES - Fully implemented
✅ **CTRL+C shuts down VMs**: YES - Graceful cleanup
✅ **Shows costs on shutdown**: YES - Prominent display
✅ **Prevents runaway costs**: YES - Budget limits, auto-termination, concurrent limits

**You're protected!** 🛡️💰
