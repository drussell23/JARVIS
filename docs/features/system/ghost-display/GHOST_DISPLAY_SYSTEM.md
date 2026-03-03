# Ghost Display System - Deep Architecture Documentation

**Version**: v283.1
**Last Updated**: 2026-03-03
**Status**: Production
**Owner**: Unified Supervisor (Lifecycle) / PhantomHardwareManager (Infrastructure) / GhostDisplayManager (Operations)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Problem Statement](#problem-statement)
3. [Architecture Overview](#architecture-overview)
4. [Component Deep Dive](#component-deep-dive)
   - [PhantomHardwareManager](#phantomhardwaremanager)
   - [GhostDisplayManager](#ghostdisplaymanager)
   - [GhostPersistenceManager](#ghostpersistencemanager)
5. [Lifecycle Integration](#lifecycle-integration)
6. [Data Flow](#data-flow)
7. [Cross-Repository Contract](#cross-repository-contract)
8. [Configuration Reference](#configuration-reference)
9. [Failure Modes & Recovery](#failure-modes--recovery)
10. [Root Causes Fixed](#root-causes-fixed)
11. [Roadmap](#roadmap)

---

## Executive Summary

The Ghost Display System provides JARVIS with an **invisible virtual monitor** — a software-defined display that exists at the macOS kernel level but is not visible to the user. This enables JARVIS to:

- **See** windows via screen capture without cluttering the user's workspace
- **Teleport** windows to an invisible workspace for background processing
- **Monitor** multiple applications simultaneously via mosaic capture
- **Recover** window positions automatically after crashes

The system eliminates the need for physical HDMI dummy plugs by leveraging BetterDisplay's virtual screen technology, managed entirely through CLI automation.

---

## Problem Statement

JARVIS needs to interact with windows that the user doesn't want visible:

| Without Ghost Display | With Ghost Display |
|---|---|
| Windows must be on user's visible screen | Windows live on invisible Display 2 |
| Vision pipeline can only see user's desktop | Vision captures ghost display independently |
| Background tasks clutter the workspace | Tasks run on hidden display silently |
| HDMI dummy plug required ($15 hardware) | Software-defined, zero hardware needed |
| Plug can be unplugged accidentally | Virtual display survives restarts |

---

## Architecture Overview

The system has three layers:

```
                    UNIFIED SUPERVISOR
                    (Lifecycle Owner)
                         │
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
   PHANTOM HARDWARE   GHOST DISPLAY   GHOST PERSISTENCE
      MANAGER           MANAGER         MANAGER
   (Infrastructure)   (Operations)    (Crash Recovery)
          │              │              │
          ▼              ▼              ▼
   BetterDisplay CLI   yabai WM     ~/.jarvis/ghost_state.json
          │              │
          ▼              ▼
   macOS Kernel      Window Teleportation
   (GPU Framebuffer)  (Space Management)
          │
          ▼
   ┌─────────────────────────────────┐
   │  CONSUMERS                      │
   │  ├─ Vision Pipeline (ScreenCap) │
   │  ├─ Trinity Commands (J-Prime)  │
   │  └─ Visual Monitor Agent        │
   └─────────────────────────────────┘
```

### Ownership Boundaries

| Component | Responsibility | Source File |
|---|---|---|
| **PhantomHardwareManager** | Create, connect, destroy virtual displays via BetterDisplay | `backend/system/phantom_hardware_manager.py` |
| **GhostDisplayManager** | Window teleportation, space management via yabai | `backend/vision/yabai_space_detector.py` |
| **GhostPersistenceManager** | Crash-safe window state persistence & recovery | `backend/vision/ghost_persistence_manager.py` |
| **Unified Supervisor** | Lifecycle orchestration, health monitoring, state publication | `unified_supervisor.py` (Phase 6.5) |
| **Trinity Handlers** | Command interface for J-Prime to control ghost display | `backend/system/trinity_handlers.py` |
| **Visual Monitor Agent** | Screen capture targeting ghost display | `backend/neural_mesh/agents/visual_monitor_agent.py` |

---

## Component Deep Dive

### PhantomHardwareManager

**File**: `backend/system/phantom_hardware_manager.py`
**Pattern**: Singleton via `get_phantom_manager()`
**Protocol Version**: v68.0 (with v283.1 connect fix)

#### Purpose

PhantomHardwareManager is the **infrastructure layer**. It manages the BetterDisplay CLI to create and maintain the virtual display hardware. It does NOT manage windows — it creates the display that other components use.

#### BetterDisplay CLI Discovery

The manager uses a multi-strategy discovery approach with zero hardcoded assumptions:

```
Priority 1: Cached path (if still valid)
Priority 2: `which betterdisplaycli` (PATH lookup)
Priority 3: Known path scan:
            ├─ /usr/local/bin/betterdisplaycli
            ├─ /opt/homebrew/bin/betterdisplaycli
            ├─ ~/.local/bin/betterdisplaycli
            ├─ ~/bin/betterdisplaycli
            └─ /Applications/BetterDisplay.app/Contents/MacOS/betterdisplaycli
Priority 4: Spotlight search via `mdfind`
```

CLI verification uses `help` command (NOT `--version` which launches a new app instance).

#### Display Creation Flow

```
ensure_ghost_display_exists_async()
│
├─ STEP 0: Quick check via system_profiler
│   └─ If display already connected → skip to yabai verification
│
├─ STEP 1: Discover BetterDisplay CLI
│   └─ Multi-path discovery (see above)
│
├─ STEP 2: Verify BetterDisplay.app is running
│   └─ pgrep → auto-launch via `open -a` if needed
│
├─ STEP 3: Check if ghost display exists in BetterDisplay config
│   └─ `get -nameLike="JARVIS GHOST" -list`
│   └─ Fallback: system_profiler parse
│
├─ STEP 4: Create virtual display
│   └─ `create -type=VirtualScreen -virtualScreenName="JARVIS GHOST" -aspectWidth=16 -aspectHeight=9`
│
├─ STEP 5: Connect display to GPU framebuffer (v283.1)
│   └─ `set -virtualScreenName="JARVIS GHOST" -connected=on`
│   └─ WITHOUT THIS, the display exists in config but is invisible to macOS
│
└─ STEP 6: Wait for kernel registration
    └─ Poll yabai every 0.5s→2.0s (exponential backoff)
    └─ Adaptive timeout via EMA of observed registration latency
```

#### Key Insight: Create vs Connect (v283.1 Fix)

BetterDisplay's `create` command only **defines** a virtual screen in BetterDisplay's internal configuration. It does NOT connect it to the GPU framebuffer. The `set -connected=on` command is what makes the display actually appear as a real display in macOS.

**Before v283.1**: `create` returned success → JARVIS thought display was ready → but system_profiler, yabai, and all consumers saw nothing. ~150 orphaned display definitions accumulated in BetterDisplay config.

**After v283.1**: `_connect_virtual_display_async()` runs after every `create` and every "already exists" detection, ensuring the display is always connected to the GPU.

#### Adaptive Timeout System

Registration wait time adapts to observed system behavior:

```python
# Exponential Moving Average of registration latency
new_ema = alpha * observed_latency + (1 - alpha) * previous_ema

# Adaptive target = 2x the observed EMA
# Capped at JARVIS_GHOST_REGISTRATION_WAIT_CAP_SECONDS (45s)
```

This means:
- First boot: uses default timeout
- Subsequent boots: timeout adapts to actual registration speed
- Fast systems get shorter waits; slow systems get longer waits automatically

---

### GhostDisplayManager

**File**: `backend/vision/yabai_space_detector.py`
**Pattern**: Part of YabaiSpaceDetector singleton

#### Purpose

GhostDisplayManager is the **operations layer**. It manages windows ON the ghost display — teleporting windows to it, laying them out, and querying their state.

#### Window Teleportation

```
teleport_window_to_ghost_async(window_id)
│
├─ Get ghost display space from yabai
│   └─ If no ghost space: auto-create via PhantomHardwareManager
│
├─ Record window state (GhostPersistenceManager)
│   └─ Original space, geometry, z-order → atomic JSON write
│
├─ Move window to ghost space
│   └─ yabai -m window {id} --space {ghost_space}
│
└─ Return (success, ghost_space_id)
```

#### Shadow Realm Protocol (v53.0)

For windows stuck in phantom fullscreen states, the standard space-move fails. The Shadow Realm protocol uses display-level targeting:

```
_exile_to_shadow_realm_async(window_id)
│
├─ Ensure window is actionable (not zombie/dehydrated)
├─ Move to Display 2: yabai -m window {id} --display 2
├─ Maximize on ghost display
└─ Return (success, "shadow_realm")
```

#### Ghost Display Status Reporting

```python
get_ghost_display_status() → {
    "available": bool,
    "status": "available" | "unavailable" | "fallback" | "reconnecting" | "disconnected",
    "window_count": int,
    "apps": ["Chrome", "Terminal", ...],
    "resolution": "2560x1440",
    "scale": 2.0,
    "space_index": 6,
    "frozen_apps": [...],
    "frozen_count": int
}
```

---

### GhostPersistenceManager

**File**: `backend/vision/ghost_persistence_manager.py`

#### Purpose

Ensures JARVIS never **loses** windows. If JARVIS crashes while windows are teleported to the ghost display, this manager detects and repatriates them on the next startup.

#### State File Schema

**Location**: `~/.jarvis/ghost_state.json`

```json
{
  "version": "1.0",
  "last_updated": "2026-03-03T14:00:00",
  "session_id": "abc123",
  "windows": {
    "12345": {
      "window_id": 12345,
      "app_name": "Chrome",
      "original_space": 4,
      "original_x": 100,
      "original_y": 200,
      "original_width": 800,
      "original_height": 600,
      "ghost_space": 6,
      "teleported_at": "2026-03-03T14:00:00",
      "z_order": 5
    }
  }
}
```

#### Crash Recovery Flow

```
On Startup:
│
├─ Load ghost_state.json
├─ Query yabai for windows on ghost space
├─ Compare against persistence file
├─ For each stranded window:
│   ├─ Move back to original_space
│   ├─ Restore geometry (x, y, width, height)
│   └─ Remove from persistence
└─ Start auto-save loop (5s interval)
```

#### Atomic Write Pattern

All writes use temp-file-then-rename to prevent corruption:

```python
# Write to temp file first
with open(temp_path, 'w') as f:
    json.dump(state, f)
# Atomic rename (single filesystem operation)
os.rename(temp_path, final_path)
```

---

## Lifecycle Integration

### Startup Phase

The ghost display initializes during **Phase 6.5** (between Permissions and AGI-OS):

```
Phase Map:
  0-5%    clean_slate
  5-15%   loading_server
  15-25%  preflight
  25-45%  resources
  45-55%  backend
  55-65%  intelligence
  65-85%  trinity
  80-85%  enterprise
  85%     permissions
  85-86%  ghost_display  ← HERE
  86-90%  agi_os
  90-93%  visual_pipeline
  93-100% frontend
```

### Non-Blocking Initialization

Ghost display initialization is **shielded and non-blocking**:

```python
# Creates a background task that won't block startup
task = create_safe_task(_run_ghost_display_initialization(phantom_mgr))

# If it completes within 30s budget: great
# If not: startup continues, ghost display finishes in background
# Callback handles deferred completion
```

This means:
- Startup is NEVER blocked by BetterDisplay being slow
- Ghost display can appear 5-60 seconds after startup completes
- All consumers handle the "not ready yet" state gracefully

### Background Health Loop

After initialization, a continuous health monitor runs:

```
Every 30s (JARVIS_GHOST_HEALTH_INTERVAL):
  ├─ Adapt timeout based on CPU load (up to 2x if >90%)
  ├─ Query PhantomHardwareManager.get_display_status_async()
  ├─ Success → publish state, reset failure counter
  └─ Failure → increment counter
      └─ After 3 consecutive failures → auto-recovery
          └─ Re-run ensure_ghost_display_exists_async()
```

---

## Data Flow

### Complete End-to-End Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. INITIALIZATION (Startup Phase 6.5)                                    │
│                                                                          │
│   unified_supervisor._initialize_ghost_display()                         │
│     → PhantomHardwareManager.ensure_ghost_display_exists_async()         │
│       → betterdisplaycli create -type=VirtualScreen ...                  │
│       → betterdisplaycli set -connected=on          (v283.1)            │
│       → yabai polling loop (0.5s→2.0s backoff)                          │
│     → Publish ghost_display_state.json                                   │
│     → Start health loop                                                  │
│     → Start crash recovery                                               │
└─────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 2. WINDOW TELEPORTATION (Event-driven)                                  │
│                                                                         │
│   J-Prime → "exile Chrome to ghost display"                             │
│     → trinity_handlers.handle_exile_window()                            │
│       → GhostDisplayManager.teleport_window_to_ghost_async()            │
│         → GhostPersistenceManager.record_teleportation()                │
│         → yabai -m window {id} --space {ghost_space}                    │
│         → ACK → J-Prime                                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 3. VISION CONSUMPTION (Continuous)                                      │
│                                                                         │
│   VisualMonitorAgent                                                    │
│     → Resolve ghost display CGDirectDisplayID                           │
│       ├─ Tier 1: GhostDisplayManager.ghost_display_id                   │
│       ├─ Tier 2: JARVIS_GHOST_CG_DISPLAY_ID env var                     │
│       └─ Tier 3: yabai index → CGDirectDisplayID                        │
│     → ScreenCap.capture(display_id=ghost_cg_id)                         │
│     → Mosaic mode: multi-window 60 FPS capture                          │
│     → Feed to reasoning pipeline                                        │
└─────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 4. HEALTH MONITORING (Background, every 30s)                            │
│                                                                         │
│   _ghost_display_health_loop()                                          │
│     → PhantomHardwareManager.get_display_status_async()                 │
│       → system_profiler SPDisplaysDataType                              │
│     → Success: publish state, reset failures                            │
│     → 3 consecutive failures: auto-recovery                             │
│       → ensure_ghost_display_exists_async()                             │
└─────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 5. CRASH RECOVERY (On startup)                                          │
│                                                                         │
│   GhostPersistenceManager.startup()                                     │
│     → Load ghost_state.json                                             │
│     → Audit stranded windows on ghost space                             │
│     → Repatriate each to original space + geometry                      │
│     → Start auto-save loop (5s)                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Cross-Repository Contract

### State File Protocol

**File**: `~/.jarvis/trinity/state/ghost_display_state.json`

```json
{
  "schema_version": 1,
  "timestamp": 1772575907.214726,
  "is_ready": true,
  "status": {
    "ghost_display_active": true,
    "cli_available": true,
    "app_running": true
  },
  "component_status": "complete"
}
```

### Consumer Contract

| Repository | Reads | Purpose |
|---|---|---|
| **JARVIS** (supervisor) | Writes | Authoritative source of truth |
| **J-Prime** | `is_ready` | Gate ghost display commands |
| **Reactor Core** | `ghost_display_active` | Display routing decisions |
| **Loading Server** | `status` | Health endpoint for frontend |

### Contract Rules

1. Only the Unified Supervisor writes to the state file
2. Consumers must tolerate stale data (file may be up to 30s old)
3. `schema_version` must be checked before parsing
4. Missing file = ghost display unavailable (graceful degradation)
5. `component_status` values: `"pending"`, `"running"`, `"complete"`, `"error"`, `"skipped"`

---

## Configuration Reference

### Core Display Settings

| Environment Variable | Default | Description |
|---|---|---|
| `JARVIS_GHOST_DISPLAY_ENABLED` | `true` | Enable/disable entire ghost display system |
| `JARVIS_GHOST_DISPLAY_NAME` | `JARVIS_GHOST` | Virtual display name in BetterDisplay |
| `JARVIS_GHOST_RESOLUTION` | `1920x1080` | Preferred resolution |
| `JARVIS_GHOST_ASPECT` | `16:9` | Aspect ratio (W:H) |

### Initialization Timeouts

| Environment Variable | Default | Description |
|---|---|---|
| `JARVIS_GHOST_DISPLAY_TIMEOUT` | `30.0` | Phase timeout (seconds) |
| `JARVIS_GHOST_REGISTRATION_WAIT_SECONDS` | 60% of phase timeout | Max wait for yabai recognition |
| `JARVIS_GHOST_REGISTRATION_STABILIZATION_SECONDS` | `4.0` | Wait after yabai sees display topology |

### Adaptive Registration

| Environment Variable | Default | Description |
|---|---|---|
| `JARVIS_GHOST_REGISTRATION_EMA_ALPHA` | `0.35` | EMA smoothing factor for latency tracking |
| `JARVIS_GHOST_REGISTRATION_WAIT_CAP_SECONDS` | `45.0` | Maximum adaptive registration wait |

### Health Monitoring

| Environment Variable | Default | Description |
|---|---|---|
| `JARVIS_GHOST_HEALTH_INTERVAL` | `30.0` | Health check frequency (seconds) |
| `JARVIS_GHOST_MAX_HEALTH_FAILURES` | `3` | Consecutive failures before recovery |
| `JARVIS_GHOST_HEALTH_CHECK_TIMEOUT` | `10.0` | Individual health check timeout |

### Crash Recovery

| Environment Variable | Default | Description |
|---|---|---|
| `JARVIS_GHOST_CRASH_RECOVERY` | `true` | Enable crash recovery on startup |
| `JARVIS_GHOST_PERSIST_STARTUP_TIMEOUT` | `10.0` | Recovery startup wait |
| `JARVIS_GHOST_REPATRIATION_TIMEOUT` | `15.0` | Window repatriation timeout |

---

## Failure Modes & Recovery

### Failure: BetterDisplay Not Installed

```
Detection: CLI discovery returns None
Recovery:  Graceful skip (logged as INFO, not ERROR)
Impact:    Ghost display unavailable; vision works on primary display only
```

### Failure: CLI Integration Disabled

```
Detection: CLI commands return "Failed." with exit code 1
Recovery:  Log warning with instructions to enable in BetterDisplay > Application > Integration
Impact:    Cannot create or query virtual displays
```

### Failure: Display Created but Not Connected (v283.1 Root Cause)

```
Detection: system_profiler doesn't show JARVIS GHOST after create succeeds
Root Cause: BetterDisplay `create` only defines config; `set -connected=on` required
Fix:       _connect_virtual_display_async() runs after every create
```

### Failure: yabai Doesn't Recognize Display

```
Detection: Polling loop exhausts max_wait_seconds
Recovery:  Two-tier:
           1. If display_count >= 2 but no ghost space → continue (stabilizing)
           2. If display_count == 1 → display not connected, log warning
Impact:    Window teleportation unavailable until next health check
```

### Failure: BetterDisplay.app Crashes

```
Detection: Health loop finds ghost_display_active=false for 3 consecutive checks
Recovery:  Auto-recovery:
           1. Re-launch BetterDisplay.app
           2. Re-create virtual display
           3. Re-connect to GPU
           4. Wait for yabai recognition
Impact:    30-90s of ghost display unavailability
```

### Failure: JARVIS Crashes with Windows on Ghost Display

```
Detection: GhostPersistenceManager finds stranded windows on startup
Recovery:  Automatic repatriation:
           1. Load persisted window state
           2. Move each window back to original space
           3. Restore geometry (position + size)
Impact:    User may briefly see windows flash during repatriation
```

### Failure: Duplicate Virtual Screens Accumulated

```
Detection: get -virtualScreenName -list returns many entries
Root Cause: Repeated create without connect → orphaned definitions
Prevention: v283.1 connect fix eliminates this; STEP 0 detects existing connected display
Recovery:   `betterdisplaycli discard -virtualScreenName="JARVIS GHOST"` cleans all
```

---

## Root Causes Fixed

### v283.1: Display Never Connected to GPU Framebuffer

**Disease**: BetterDisplay's `create` command only saves a virtual screen definition to config. Without `set -connected=on`, the display never appears at the macOS kernel level. PhantomHardwareManager returned success after `create` without connecting.

**Symptom**: Ghost display state showed `error`, ~150 orphaned definitions accumulated, yabai never saw a second display.

**Cure**: Added `_connect_virtual_display_async()` which runs `set -virtualScreenName="JARVIS GHOST" -connected=on` after every successful create AND after detecting an "already exists" condition.

### v251.2: CLI `list` Command Spawning App Instances

**Disease**: BetterDisplay does not have a `list` operation. Unrecognized commands cause it to launch a new app instance, spawning zombie processes and menu bar icons.

**Cure**: Changed CLI verification from `--version` to `help`. Changed display query from `list` to `get -nameLike=... -list`. Added `system_profiler` fallback that works without CLI integration.

### v68.0: Physical HDMI Dummy Plug Dependency

**Disease**: JARVIS required a physical HDMI dummy plug for virtual display functionality. Plug could be unplugged, lost, or unavailable.

**Cure**: Software-defined virtual displays via BetterDisplay, managed entirely through CLI automation.

---

## Roadmap

### Completed (Current State)

- [x] Virtual display creation via BetterDisplay CLI
- [x] GPU framebuffer connection (v283.1)
- [x] yabai space registration with adaptive timeouts
- [x] Window teleportation (space-move + shadow realm protocols)
- [x] Crash-safe window persistence
- [x] Automatic crash recovery on startup
- [x] Background health monitoring with auto-recovery
- [x] Cross-repo state publication
- [x] Non-blocking startup integration
- [x] Multi-path CLI discovery
- [x] BetterDisplay auto-launch

### Phase 1: Hardening (Near-term)

- [ ] **Duplicate prevention gate**: Check for existing JARVIS GHOST before create (prevent accumulation if connect fails)
- [ ] **Health check via CLI**: Use `betterdisplaycli get -nameLike=... -connected` instead of system_profiler (faster, more reliable)
- [ ] **Disconnect on shutdown**: `set -connected=off` during graceful shutdown to clean up macOS display topology
- [ ] **Resolution negotiation**: Detect primary display resolution and set ghost display to match for consistent capture quality
- [ ] **Display layout management**: Position ghost display adjacent to primary via yabai (prevent user accidentally focusing it)

### Phase 2: Intelligence (Medium-term)

- [ ] **Window auto-triage**: Automatically detect windows that should be on ghost display (e.g., background processes, hidden browsers)
- [ ] **Multi-window layout engine**: Tile windows on ghost display for optimal mosaic capture
- [ ] **Capture-aware resolution**: Dynamically adjust ghost display resolution based on vision pipeline needs
- [ ] **Memory-aware degradation**: Reduce ghost display resolution under memory pressure to free GPU VRAM
- [ ] **Display group management**: Use BetterDisplay display groups for coordinated multi-display behavior

### Phase 3: Cross-Repo Integration (Long-term)

- [ ] **J-Prime native ghost awareness**: J-Prime queries ghost display state directly instead of via state file
- [ ] **Reactor Core capture routing**: Reactor Core can request specific windows be captured on ghost display
- [ ] **Event-driven state sync**: Replace polling-based state file with WebSocket/event-driven state propagation
- [ ] **Multi-ghost-display support**: Multiple virtual displays for different purposes (vision, browser automation, monitoring)
- [ ] **Remote ghost display streaming**: Stream ghost display to remote monitoring dashboard

### Phase 4: Advanced Capabilities (Future)

- [ ] **PIP monitoring window**: Small live preview of ghost display on primary screen (requires BetterDisplay Pro)
- [ ] **OCR-optimized display mode**: Configure ghost display settings optimized for text recognition
- [ ] **Automated display recovery testing**: Chaos testing that randomly disconnects ghost display and verifies recovery
- [ ] **Display performance metrics**: Track capture latency, frame drops, GPU utilization on ghost display
- [ ] **Hot-swap display backend**: Support alternative virtual display backends (e.g., native CGVirtualDisplay API on macOS 14+)

---

## Prerequisites

### Required

- **macOS** (any Apple Silicon or Intel Mac)
- **yabai** window manager (for space/display management)
- **BetterDisplay** (free version, v4.0+)
  - CLI integration enabled: BetterDisplay > Application > Integration > Enable CLI access

### Optional

- **BetterDisplay Pro** ($21.99) — only needed for PIP monitoring window feature (Phase 4 roadmap)

### Verification

```bash
# Check BetterDisplay CLI
betterdisplaycli help | head -1
# Expected: "BetterDisplay Version X.X.X Build NNNNN ..."

# Check CLI integration works
betterdisplaycli get -brightness
# Expected: a number like "0.773" (NOT "Failed.")

# Check ghost display exists
system_profiler SPDisplaysDataType | grep -A5 "JARVIS"
# Expected: JARVIS GHOST display entry

# Check yabai sees it
yabai -m query --displays | python3 -c "import sys,json; print(len(json.load(sys.stdin)),'displays')"
# Expected: "2 displays"
```

---

## File Reference

| File | Lines | Purpose |
|---|---|---|
| `backend/system/phantom_hardware_manager.py` | ~1050 | BetterDisplay CLI integration, display creation/connection |
| `backend/vision/yabai_space_detector.py` | ~13000 | Window teleportation, ghost space management |
| `backend/vision/ghost_persistence_manager.py` | ~400 | Crash-safe window state persistence |
| `unified_supervisor.py` (lines 75570-76060) | ~490 | Lifecycle integration, health loop, state publication |
| `backend/system/trinity_handlers.py` (lines 267-388) | ~120 | Trinity command handlers for ghost display |
| `backend/neural_mesh/agents/visual_monitor_agent.py` | ~4000 | Vision pipeline ghost display consumer |
| `backend/loading_server.py` (lines 1370-1393) | ~24 | Ghost display status reporting to frontend |
