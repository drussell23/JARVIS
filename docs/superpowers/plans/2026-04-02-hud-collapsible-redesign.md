# HUD Collapsible Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reclaim 60% of screen real estate by adding a collapsible toggle to the JARVISHUD — hero mode for boot/demos, compact mode for working.

**Architecture:** Add `isCollapsed` state with `@AppStorage` persistence. Expanded mode keeps current layout unchanged. Collapsed mode replaces title+reactor+spacers with a compact header bar containing mini reactor dot + text + segmented control. Spring animation between states. Auto-collapse after 5s on first launch.

**Tech Stack:** Swift 6.0 / SwiftUI / macOS 14+

**Spec:** `docs/superpowers/specs/2026-04-02-hud-collapsible-redesign.md`

---

## File Structure

| File | Change |
|------|--------|
| Create: `JARVISHUD/Views/CompactHeaderBar.swift` | New: mini reactor dot + JARVIS text + status + segmented control |
| Modify: `JARVISHUD/Views/HUDView.swift` | Add isCollapsed state, conditional layout, boot auto-collapse, keyboard shortcut |

---

## Task 1: CompactHeaderBar Component

**Files:**
- Create: `JARVIS-Apple/JARVISHUD/Views/CompactHeaderBar.swift`

- [ ] **1.1: Create CompactHeaderBar.swift**

```swift
// JARVISHUD/Views/CompactHeaderBar.swift
// Compact header bar shown when HUD is collapsed.
// Contains: mini reactor dot, JARVIS text, status, segmented tab control.

import SwiftUI

struct CompactHeaderBar: View {
    let hudState: HUDState
    let statusText: String
    let serverVersion: String
    let isConnected: Bool
    @Binding var hudTab: HUDTab
    var onExpandReactor: () -> Void
    
    var body: some View {
        HStack(spacing: 0) {
            // Left: Mini reactor + JARVIS name
            HStack(spacing: 10) {
                // Mini reactor dot (20px) — double-tap to expand
                Circle()
                    .fill(
                        RadialGradient(
                            colors: [reactorCenterColor, reactorEdgeColor, reactorEdgeColor.opacity(0)],
                            center: .center,
                            startRadius: 0,
                            endRadius: 10
                        )
                    )
                    .frame(width: 20, height: 20)
                    .shadow(color: reactorGlowColor.opacity(0.5), radius: 6)
                    .overlay(
                        Circle()
                            .fill(reactorCenterColor.opacity(0.8))
                            .frame(width: 6, height: 6)
                            .opacity(pulseOpacity)
                            .animation(.easeInOut(duration: 1.5).repeatForever(autoreverses: true), value: pulseOpacity)
                    )
                    .onTapGesture(count: 2) {
                        onExpandReactor()
                    }
                
                Text("JARVIS")
                    .font(.system(size: 14, weight: .bold, design: .monospaced))
                    .foregroundColor(.jarvisGreen)
                    .tracking(2)
                
                // Status badge
                if isConnected {
                    Text("v\(serverVersion) ONLINE")
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundColor(.white.opacity(0.3))
                        .tracking(1)
                } else {
                    Text(statusText)
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundColor(.orange.opacity(0.6))
                        .tracking(1)
                        .lineLimit(1)
                }
            }
            .padding(.leading, 20)
            
            Spacer()
            
            // Right: Segmented control
            Picker("", selection: $hudTab) {
                ForEach(HUDTab.allCases, id: \.self) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 140)
            .padding(.trailing, 20)
        }
        .frame(height: 44)
        .background(Color.black.opacity(0.2))
        .overlay(
            Rectangle()
                .fill(Color.jarvisGreen.opacity(0.1))
                .frame(height: 1),
            alignment: .bottom
        )
    }
    
    // MARK: - Reactor Colors
    
    private var reactorCenterColor: Color {
        switch hudState {
        case .online, .processing: return .jarvisGreen
        case .connecting: return .yellow
        case .offline: return .gray
        default: return .jarvisGreen
        }
    }
    
    private var reactorEdgeColor: Color {
        reactorCenterColor.opacity(0.3)
    }
    
    private var reactorGlowColor: Color {
        reactorCenterColor
    }
    
    private var pulseOpacity: Double {
        switch hudState {
        case .online: return 1.0
        case .processing: return 0.6
        case .connecting: return 0.4
        default: return 0.2
        }
    }
}
```

**Note:** `HUDState` and `.jarvisGreen` are already defined in the project (AppState.swift and JARVISColors.swift). The `default` cases in the switch handle any additional HUDState cases that may exist.

- [ ] **1.2: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
git add JARVIS-Apple/JARVISHUD/Views/CompactHeaderBar.swift
git commit -m "feat(hud): add CompactHeaderBar component for collapsed mode"
```

---

## Task 2: Collapsible HUDView

**Files:**
- Modify: `JARVIS-Apple/JARVISHUD/Views/HUDView.swift`

This is the main task — modifying the existing HUDView to support collapsed/expanded modes.

**CRITICAL:** You MUST read the full HUDView.swift file first. It is ~760 lines. The file already has `hudTab`, `hiveStore`, `HUDTab` enum, `VoiceStatus`, `VoiceState`, and many other state variables. Do NOT recreate or duplicate any of these.

- [ ] **2.1: Read the full HUDView.swift**

Read `/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/JARVIS-Apple/JARVISHUD/Views/HUDView.swift` completely to understand the structure.

- [ ] **2.2: Add collapse state variables**

Find the `@State` declarations block (around lines 41-56). Add these two new state variables:

```swift
@AppStorage("hudCollapsed") private var isCollapsed: Bool = false
@State private var hasAutoCollapsed: Bool = false  // Tracks if boot auto-collapse has fired
```

- [ ] **2.3: Replace the main VStack content with conditional layout**

Find the main `VStack(spacing: 0) {` (around line 93). The content inside this VStack needs to be wrapped in a conditional. Replace the entire content of the VStack (from line 93's opening brace to its closing brace) with:

**EXPANDED MODE** — wrap the existing title, spacer, reactor, spacer, status, voice banner in `if !isCollapsed { ... }`:

```swift
VStack(spacing: 0) {
    if isCollapsed {
        // ===== COLLAPSED (Compact) MODE =====
        
        CompactHeaderBar(
            hudState: hudState,
            statusText: statusText,
            serverVersion: pythonBridge.serverVersion,
            isConnected: pythonBridge.connectionStatus == .connected,
            hudTab: $hudTab,
            onExpandReactor: {
                withAnimation(.spring(duration: 0.4)) {
                    isCollapsed = false
                }
            }
        )
        
        // Slim voice status
        HStack {
            Text(voiceStatusIcon)
                .font(.system(size: 10))
            Text(voiceStatus.message)
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundColor(.jarvisGreen.opacity(0.5))
            if !currentTranscript.isEmpty {
                Text("• \(currentTranscript)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundColor(.white.opacity(0.4))
                    .lineLimit(1)
            }
            Spacer()
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 6)
        .background(Color.jarvisGreen.opacity(0.02))
        
    } else {
        // ===== EXPANDED (Hero) MODE =====
        
        // Keep ALL existing code: title, spacer, reactor, spacer, status, voice banner
        // (lines 96-156 of the original file — copy them here exactly as-is)
        
        VStack(spacing: 8) {
            Text("J.A.R.V.I.S.")
                .font(.system(size: 72, weight: .bold, design: .monospaced))
                .tracking(20)
                .foregroundColor(.jarvisGreen)
                .shadow(color: Color.jarvisGreenGlow(opacity: 0.8), radius: 20)
                .shadow(color: Color.jarvisGreenGlow(opacity: 0.6), radius: 40)
            
            Text("JUST A RATHER VERY INTELLIGENT SYSTEM")
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .tracking(4)
                .foregroundColor(.jarvisGreen)
                .shadow(color: Color.jarvisGreenGlow(opacity: 0.6), radius: 10)
        }
        .padding(.top, 60)
        
        Spacer()
        
        ArcReactorView(state: hudState, onQuit: onQuit)
            .frame(width: 440, height: 440)
            .onTapGesture(count: 2) {
                withAnimation(.spring(duration: 0.4)) {
                    isCollapsed = true
                }
            }
        
        Spacer()
        
        // Status message below reactor
        VStack(spacing: 8) {
            Text(statusText)
                .font(.system(size: 14, weight: .bold, design: .monospaced))
                .tracking(3)
                .foregroundColor(.jarvisGreen)
                .shadow(color: Color.jarvisGreenGlow(opacity: 0.6), radius: 10)
            
            if !pythonBridge.detailedConnectionState.isEmpty && pythonBridge.detailedConnectionState != statusText {
                HStack(spacing: 6) {
                    ConnectionStateIndicator(state: pythonBridge.connectionStatus)
                    Text(pythonBridge.detailedConnectionState)
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundColor(.white.opacity(0.5))
                        .tracking(1)
                }
            }
            
            if pythonBridge.connectionStatus == .connected, !pythonBridge.serverVersion.isEmpty, pythonBridge.serverVersion != "unknown" {
                Text("v\(pythonBridge.serverVersion) • \(pythonBridge.serverCapabilities.joined(separator: ", "))")
                    .font(.system(size: 9, weight: .regular, design: .monospaced))
                    .foregroundColor(.white.opacity(0.3))
                    .tracking(1)
            }
        }
        .padding(.bottom, 30)
        
        VoiceStatusBanner(voiceStatus: voiceStatus, currentTranscript: currentTranscript)
            .padding(.horizontal, 60)
            .padding(.bottom, 10)
        
        // Collapse hint
        Text("Double-click reactor or ⌘⇧R to collapse")
            .font(.system(size: 9, design: .monospaced))
            .foregroundColor(.white.opacity(0.15))
            .padding(.bottom, 8)
        
        // Segmented tab control (expanded mode)
        Picker("", selection: $hudTab) {
            ForEach(HUDTab.allCases, id: \.self) { tab in
                Text(tab.rawValue).tag(tab)
            }
        }
        .pickerStyle(.segmented)
        .padding(.horizontal, 20)
        .frame(maxWidth: 200)
    }
    
    // ===== SHARED: Content + Command Input (both modes) =====
    
    if hudTab == .chat {
        ScrollView {
            TranscriptView(messages: transcriptMessages)
                .padding(.vertical, 20)
        }
        .frame(maxWidth: 1000)
        .padding(.horizontal, isCollapsed ? 20 : 60)
        .padding(.bottom, 10)
        .layoutPriority(1)  // Content gets priority space
        
        // Command input - keep existing styling
        // (copy the existing HStack with TextField + SEND button exactly as-is)
    } else {
        HiveView(hiveStore: hiveStore)
            .layoutPriority(1)
    }
}
```

**IMPORTANT:** The implementer must:
1. Read the FULL existing HUDView.swift
2. Keep ALL existing code for the command input, vision button, screen lock overlay, etc.
3. Only restructure the title/reactor/status/voice/segmented section into the if/else
4. Remove the `.frame(height: 250)` from the ScrollView (let it fill available space)
5. Keep the command input section OUTSIDE the if/else (shared by both modes)

- [ ] **2.4: Add voice status icon helper**

Add a computed property to HUDView (inside the struct, after the `pythonBridge` accessor):

```swift
private var voiceStatusIcon: String {
    switch voiceStatus.state {
    case .inactive: return "🔇"
    case .waitingForWakeWord: return "🎤"
    case .listeningForCommand: return "🔴"
    case .processing: return "⏳"
    }
}
```

- [ ] **2.5: Add boot auto-collapse logic**

Add an `.onAppear` modifier to the main ZStack (the outermost container in `body`):

```swift
.onAppear {
    if !hasAutoCollapsed && !isCollapsed {
        // First appearance: show hero for 5s, then collapse
        hasAutoCollapsed = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            withAnimation(.spring(duration: 0.4)) {
                isCollapsed = true
            }
        }
    }
}
```

- [ ] **2.6: Add keyboard shortcut**

Add a `.keyboardShortcut` handler. In SwiftUI for macOS, add this to the outermost view:

```swift
.background(
    Button("") {
        withAnimation(.spring(duration: 0.4)) {
            isCollapsed.toggle()
        }
    }
    .keyboardShortcut("r", modifiers: [.command, .shift])
    .opacity(0)
    .frame(width: 0, height: 0)
)
```

This creates a hidden button that captures ⌘⇧R.

- [ ] **2.7: Verify build in Xcode**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/JARVIS-Apple
xcodegen generate
xcodebuild -project JARVIS-Apple.xcodeproj -scheme JARVISHUD -destination "platform=macOS" build 2>&1 | grep -E "error:|BUILD"
```

Expected: `BUILD SUCCEEDED`

- [ ] **2.8: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
git add JARVIS-Apple/JARVISHUD/Views/HUDView.swift
git commit -m "feat(hud): add collapsible mode — hero boot + compact working mode"
```

---

## Summary

| Task | Component | Dependencies |
|------|-----------|-------------|
| 1 | CompactHeaderBar (new file) | None |
| 2 | Collapsible HUDView (modify existing) | Task 1 |

**2 tasks, sequential.** Task 2 is the heavy lift — it restructures the existing 760-line HUDView. The implementer MUST read the full file before making changes.
