# Hive SwiftUI HiveView + Portfolio — Design Spec

**Date:** 2026-04-02
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Approved
**Depends on:** Hive backend (349 tests), `backend/hive/` package complete

## Overview

Three independent sub-projects that make the Hive visible:

1. **SwiftUI HiveView** — Native macOS HUD tab showing live agent debates via IPC
2. **Portfolio Hive Page** — Dual-mode: public summary for visitors, private detail behind auth
3. **API Bridge** — jarvis-cloud endpoint serving sanitized Hive summary for the portfolio

## 1. SwiftUI HiveView (JARVIS-Apple)

### HiveStore (@Observable)

New `@Observable` class that receives Hive events from the IPC stream and maintains thread state.

**Event routing:** The existing `BrainstemLauncher` dispatches all IPC events via `NWConnection`. Currently all events flow to `AppState` (PythonBridge). Add a routing layer:
- Existing event types (`token`, `daemon`, `status`, `complete`, `action`) → `AppState` (unchanged)
- New event types (`agent_log`, `persona_reasoning`, `thread_lifecycle`, `cognitive_transition`) → `HiveStore`

**HiveStore state:**
```swift
@Observable
class HiveStore {
    var cognitiveState: String = "baseline"  // "baseline" | "rem" | "flow"
    var threads: [HiveThread] = []           // Sorted by last activity
    var activeThreadCount: Int { threads.filter { $0.isActive }.count }
}
```

**HiveThread model (Swift):**
```swift
struct HiveThread: Identifiable {
    let id: String              // thread_id
    var title: String
    var state: String           // "open" | "debating" | "consensus" | "executing" | "resolved" | "stale"
    var messages: [HiveMessage]
    var tokensConsumed: Int
    var tokenBudget: Int
    var linkedOpId: String?
    var lastActivityAt: Date
    var isActive: Bool { !["resolved", "stale"].contains(state) }
}
```

**HiveMessage model (Swift):**
```swift
enum HiveMessage: Identifiable {
    case agentLog(AgentLogData)
    case personaReasoning(PersonaReasoningData)

    var id: String { /* message_id from either case */ }
}

struct AgentLogData: Codable {
    let messageId: String
    let agentName: String
    let trinityParent: String
    let severity: String
    let category: String
    let payload: [String: AnyCodable]  // or use JSONValue enum
}

struct PersonaReasoningData: Codable {
    let messageId: String
    let persona: String        // "jarvis" | "j_prime" | "reactor"
    let role: String           // "body" | "mind" | "immune_system"
    let intent: String         // "observe" | "propose" | "validate" etc.
    let reasoning: String
    let confidence: Double
    let modelUsed: String
    let tokenCost: Int
    let validateVerdict: String?  // "approve" | "reject" | nil
    let manifestoPrinciple: String?
}
```

**Event handling:**
- `agent_log` → find or create thread by thread_id, append message
- `persona_reasoning` → find thread, append message, update tokensConsumed
- `thread_lifecycle` → update thread state (debating, consensus, executing, resolved, stale)
- `cognitive_transition` → update `cognitiveState`

### HiveView (SwiftUI)

**Layout (inside existing ClickThroughWindow):**

```
┌─────────────────────────────────────┐
│         J.A.R.V.I.S.               │
│      [ Chat | Hive ]  ← segmented  │
│                                     │
│          ⬡ Arc Reactor              │
│                                     │
│  ┌─ Cognitive State: FLOW 🔥 ────┐  │
│  │                                │  │
│  │  ▼ Memory Pressure (DEBATING)  │  │
│  │    ┊ HM: RAM 87.3% ⚠         │  │
│  │    ┊ JARVIS: Pressure rising   │  │
│  │    ┊ J-Prime: Add TTL eviction │  │
│  │    ┊ Reactor: Approved ✅      │  │
│  │                                │  │
│  │  ▶ Manifesto Review (RESOLVED) │  │
│  │                                │  │
│  └────────────────────────────────┘  │
│                                     │
│  [ Command input ]                  │
└─────────────────────────────────────┘
```

**Components:**
- `HiveView` — main container with cognitive state header + thread list
- `CognitiveStateBar` — shows current state with color + icon (BASELINE=cyan dot, REM=purple moon, FLOW=orange fire)
- `HiveThreadCard` — expandable card for a single thread (title, state badge, message count)
- `HiveMessageRow` — renders a single message (thin cyan for agent_log, colored card for persona reasoning)
- `ThreadStateBadge` — color-coded pill: OPEN=gray, DEBATING=orange, CONSENSUS=green, EXECUTING=purple, RESOLVED=blue, STALE=red

**Navigation:** `Picker` (segmented style) in the HUD header. Binding swaps between existing `TranscriptView` content and `HiveView`. Arc Reactor stays visible in both modes.

**Colors (matching the brainstorming mockup):**
- JARVIS (Body): `#22d3ee` (cyan)
- J-Prime (Mind): `#a78bfa` (purple)
- Reactor (Immune): `#ef4444` (red)
- Agent logs: `#38bdf8` (light blue), thin row
- Cognitive states: BASELINE=`#22d3ee`, REM=`#a78bfa`, FLOW=`#f97316`

### Files (JARVIS-Apple)

| File | Responsibility |
|------|----------------|
| `JARVISHUD/Services/HiveStore.swift` | @Observable store, event handling, thread state |
| `JARVISHUD/Models/HiveModels.swift` | HiveThread, HiveMessage, AgentLogData, PersonaReasoningData |
| `JARVISHUD/Views/HiveView.swift` | Main Hive tab container |
| `JARVISHUD/Views/CognitiveStateBar.swift` | FSM state indicator |
| `JARVISHUD/Views/HiveThreadCard.swift` | Expandable thread card |
| `JARVISHUD/Views/HiveMessageRow.swift` | Individual message renderer |
| `JARVISHUD/Views/ThreadStateBadge.swift` | Color-coded state pill |

### Modified Files (JARVIS-Apple)

| File | Change |
|------|--------|
| `JARVISHUD/JARVISHUDApp.swift` | Add HiveStore to environment, wire IPC event routing |
| `JARVISHUD/Views/HUDView.swift` | Add segmented control, swap content between transcript and HiveView |
| `JARVISHUD/Services/BrainstemLauncher.swift` | Route `hive_*` event types to HiveStore |

---

## 2. Portfolio Hive Page (jarvis-portfolio)

### Public Page `/hive`

Accessible without auth. Shows sanitized summary — no persona reasoning text, no telemetry details.

**Content:**
- Cognitive state indicator (animated: BASELINE=pulsing cyan, REM=slow purple glow, FLOW=orange fire animation)
- Active thread count + titles
- Recent resolved threads (last 5): title + outcome (pr_opened / stale) + timestamp
- Today's stats: total threads, tokens consumed, debates resolved
- "Powered by the Symbiotic AI-Native Manifesto" footer link

**Data source:** Polls `GET /api/hive/summary` from jarvis-cloud every 30 seconds. No SSE (simpler, no auth needed, cheaper).

### Private Detail (behind auth)

Accessible via device token cookie. Full thread view matching the native HiveView — persona reasoning, agent logs, token costs, Manifesto citations.

**Auth:** Check for `jarvis_device_id` cookie. If absent, show "Authenticate to view full Hive feed" with redirect to jarvis-cloud auth flow.

**Data source:** SSE via jarvis-cloud `/api/stream/{deviceId}` (same as native HUD).

### Files (jarvis-portfolio)

| File | Responsibility |
|------|----------------|
| `app/hive/page.tsx` | Public summary page |
| `app/hive/detail/page.tsx` | Private full-detail page (auth-gated) |
| `components/hive/CognitiveStateIndicator.tsx` | Animated state display |
| `components/hive/ThreadSummaryCard.tsx` | Public thread card (title + outcome only) |
| `components/hive/HiveStats.tsx` | Today's stats bar |
| `lib/hive-api.ts` | Fetch wrapper for `/api/hive/summary` |

---

## 3. API Bridge (jarvis-cloud)

### `GET /api/hive/summary` (public, no auth)

Returns sanitized Hive state for the portfolio public page.

**Response:**
```json
{
  "cognitive_state": "flow",
  "active_threads": [
    {"title": "Memory Pressure in Vision Loop", "state": "debating"}
  ],
  "recent_resolved": [
    {"title": "Stale SHM Cleanup", "outcome": "pr_opened", "resolved_at": "2026-04-02T14:30:00Z"}
  ],
  "stats": {
    "total_threads_today": 3,
    "tokens_consumed_today": 12847,
    "debates_resolved_today": 2
  }
}
```

**Data source:** The backend Python Hive already persists threads to `~/.jarvis/hive/threads/`. The brainstem can expose a local HTTP endpoint that jarvis-cloud proxies, OR jarvis-cloud reads from Redis (if Hive events are already in the SSE stream).

**Simplest v1 path:** The HUD Relay Agent already projects all events to IPC. The brainstem's `command_sender.py` already forwards events to Vercel via `/api/command`. Add a small Redis-backed accumulator in jarvis-cloud that tracks the latest Hive state from the SSE stream, and serve it from `/api/hive/summary`.

### Files (jarvis-cloud)

| File | Responsibility |
|------|----------------|
| `app/api/hive/summary/route.ts` | Public summary endpoint |
| `lib/hive/hive-state.ts` | Redis-backed Hive state accumulator |

### Modified Files (jarvis-cloud)

| File | Change |
|------|--------|
| `app/api/stream/[deviceId]/route.ts` | Accumulate Hive events into Redis state alongside SSE delivery |

---

## 4. Build Order

These three sub-projects are independent:

1. **SwiftUI HiveView** — can be built and tested with mock IPC events
2. **API Bridge** — can be built and tested with mock Redis data
3. **Portfolio Page** — can be built once API bridge is ready (or with mock data)

Recommended order: **SwiftUI first** (you'll see live data immediately from the running Hive), then **API bridge + portfolio** in parallel.

---

## 5. Out of Scope

- Portfolio auth flow (reuse existing jarvis-cloud device auth)
- Push notifications for Hive events
- Historical thread analytics
- Public persona reasoning (always private — security boundary)
- Portfolio SSE (public uses polling only)
