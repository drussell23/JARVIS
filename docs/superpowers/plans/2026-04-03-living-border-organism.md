# JARVIS Living Border — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken full-screen overlay with a Living Border (ambient screen-edge glow) + slide-in Panel, turning JARVIS into a living organism that inhabits macOS.

**Architecture:** Two-window system: (1) LivingBorderWindow — borderless, full-screen, `ignoresMouseEvents=true`, renders only an animated border glow via CoreAnimation. (2) JARVISPanel — right-anchored NSPanel with NSVisualEffectView for frosted glass, contains all SwiftUI content (header, tabs, chat, hive). Voice commands "show"/"hide" control panel visibility. The border is always on.

**Tech Stack:** Swift 6.0, SwiftUI, AppKit (NSWindow, NSPanel, NSVisualEffectView), CoreAnimation (CAShapeLayer, CABasicAnimation), macOS 14+

**Spec:** `docs/superpowers/specs/2026-04-03-living-border-organism-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `JARVISHUD/Views/LivingBorderWindow.swift` | Borderless full-screen window rendering animated edge glow |
| `JARVISHUD/Views/JARVISPanel.swift` | Right-anchored NSPanel with frosted glass + SwiftUI content |

### Modified Files

| File | Change |
|------|--------|
| `JARVISHUD/JARVISHUDApp.swift` | Replace ClickThroughWindow with LivingBorderWindow + JARVISPanel |
| `JARVISHUD/Views/HUDView.swift` | Remove collapsible/expanded logic — panel handles layout now |
| `JARVISHUD/Services/WakeWordListener.swift` | No changes needed (onCommand callback already exists) |

### Deleted Files

| File | Reason |
|------|--------|
| `JARVISHUD/Views/ClickThroughWindow.swift` | Replaced by LivingBorderWindow |
| `JARVISHUD/Views/ClickThroughHostingView.swift` | Panel uses standard NSHostingView |

---

## Task 1: LivingBorderWindow

**Files:**
- Create: `JARVIS-Apple/JARVISHUD/Views/LivingBorderWindow.swift`

The border glow window. Full-screen, completely invisible to interaction. Just renders an animated border.

- [ ] **1.1: Create LivingBorderWindow.swift**

```swift
// JARVISHUD/Views/LivingBorderWindow.swift
// The Living Border — JARVIS's heartbeat.
// A full-screen borderless window that renders an animated glow around
// the screen edges. Ignores ALL mouse events. Takes zero workspace.

import AppKit
import QuartzCore

/// Cognitive state drives border color and animation speed
enum BorderState: String {
    case baseline   // Green, 4s breath
    case rem        // Purple, 3s pulse
    case flow       // Orange, 2s pulse
    case alert      // Red, 1.5s pulse
    case offline    // Dim gray, no animation
    
    var color: NSColor {
        switch self {
        case .baseline: return NSColor(red: 0.29, green: 0.87, blue: 0.50, alpha: 1.0) // #4ade80
        case .rem:      return NSColor(red: 0.65, green: 0.55, blue: 0.98, alpha: 1.0) // #a78bfa
        case .flow:     return NSColor(red: 0.98, green: 0.45, blue: 0.09, alpha: 1.0) // #f97316
        case .alert:    return NSColor(red: 0.94, green: 0.27, blue: 0.27, alpha: 1.0) // #ef4444
        case .offline:  return NSColor(red: 0.39, green: 0.45, blue: 0.55, alpha: 1.0) // #64748b
        }
    }
    
    var breathDuration: CFTimeInterval {
        switch self {
        case .baseline: return 4.0
        case .rem:      return 3.0
        case .flow:     return 2.0
        case .alert:    return 1.5
        case .offline:  return 0  // No animation
        }
    }
    
    var glowIntensity: CGFloat {
        switch self {
        case .baseline: return 0.15
        case .rem:      return 0.20
        case .flow:     return 0.25
        case .alert:    return 0.30
        case .offline:  return 0.05
        }
    }
}

class LivingBorderWindow: NSWindow {
    
    private var borderLayer: CAShapeLayer?
    private var glowLayer: CAShapeLayer?
    private var currentState: BorderState = .baseline
    
    init() {
        guard let screen = NSScreen.main else {
            super.init(contentRect: .zero, styleMask: .borderless, backing: .buffered, defer: false)
            return
        }
        
        super.init(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        
        // Window properties — completely invisible to interaction
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = false
        self.level = .screenSaver  // Above everything but doesn't capture input
        self.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        self.ignoresMouseEvents = true  // CRITICAL: passes ALL events through
        self.titlebarAppearsTransparent = true
        self.titleVisibility = .hidden
        
        // Set up the content view with layers
        let view = NSView(frame: screen.frame)
        view.wantsLayer = true
        view.layer?.backgroundColor = .clear
        self.contentView = view
        
        setupBorderLayers()
        setFrame(screen.frame, display: true)
        orderFrontRegardless()
    }
    
    // MARK: - Border Rendering
    
    private func setupBorderLayers() {
        guard let view = contentView, let layer = view.layer else { return }
        let bounds = view.bounds
        
        // Outer glow layer (soft, wide)
        let glow = CAShapeLayer()
        glow.path = CGPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), cornerWidth: 0, cornerHeight: 0, transform: nil)
        glow.fillColor = nil
        glow.strokeColor = currentState.color.withAlphaComponent(currentState.glowIntensity * 0.5).cgColor
        glow.lineWidth = 8
        glow.shadowColor = currentState.color.cgColor
        glow.shadowRadius = 15
        glow.shadowOpacity = Float(currentState.glowIntensity)
        glow.shadowOffset = .zero
        layer.addSublayer(glow)
        glowLayer = glow
        
        // Inner border layer (sharp, thin)
        let border = CAShapeLayer()
        border.path = CGPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), cornerWidth: 0, cornerHeight: 0, transform: nil)
        border.fillColor = nil
        border.strokeColor = currentState.color.withAlphaComponent(currentState.glowIntensity).cgColor
        border.lineWidth = 2
        layer.addSublayer(border)
        borderLayer = border
        
        // Start breathing animation
        startBreathing()
    }
    
    private func startBreathing() {
        guard currentState != .offline else {
            borderLayer?.removeAllAnimations()
            glowLayer?.removeAllAnimations()
            return
        }
        
        let duration = currentState.breathDuration
        let intensity = currentState.glowIntensity
        let color = currentState.color
        
        // Glow opacity animation
        let glowAnim = CABasicAnimation(keyPath: "shadowOpacity")
        glowAnim.fromValue = Float(intensity * 0.3)
        glowAnim.toValue = Float(intensity)
        glowAnim.duration = duration
        glowAnim.autoreverses = true
        glowAnim.repeatCount = .infinity
        glowAnim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        glowLayer?.add(glowAnim, forKey: "breathe-glow")
        
        // Border opacity animation
        let borderAnim = CABasicAnimation(keyPath: "strokeColor")
        borderAnim.fromValue = color.withAlphaComponent(intensity * 0.4).cgColor
        borderAnim.toValue = color.withAlphaComponent(intensity).cgColor
        borderAnim.duration = duration
        borderAnim.autoreverses = true
        borderAnim.repeatCount = .infinity
        borderAnim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        borderLayer?.add(borderAnim, forKey: "breathe-border")
        
        // Shadow radius pulse
        let radiusAnim = CABasicAnimation(keyPath: "shadowRadius")
        radiusAnim.fromValue = 8
        radiusAnim.toValue = 20
        radiusAnim.duration = duration
        radiusAnim.autoreverses = true
        radiusAnim.repeatCount = .infinity
        radiusAnim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        glowLayer?.add(radiusAnim, forKey: "breathe-radius")
    }
    
    // MARK: - State Updates
    
    func updateState(_ newState: BorderState) {
        guard newState != currentState else { return }
        currentState = newState
        
        let color = newState.color
        let intensity = newState.glowIntensity
        
        // Update colors
        CATransaction.begin()
        CATransaction.setAnimationDuration(0.5)
        borderLayer?.strokeColor = color.withAlphaComponent(intensity).cgColor
        glowLayer?.strokeColor = color.withAlphaComponent(intensity * 0.5).cgColor
        glowLayer?.shadowColor = color.cgColor
        glowLayer?.shadowOpacity = Float(intensity)
        CATransaction.commit()
        
        // Restart breathing with new speed
        startBreathing()
    }
}
```

- [ ] **1.2: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
git add JARVIS-Apple/JARVISHUD/Views/LivingBorderWindow.swift
git commit -m "feat(hud): add LivingBorderWindow — animated screen-edge glow"
```

---

## Task 2: JARVISPanel

**Files:**
- Create: `JARVIS-Apple/JARVISHUD/Views/JARVISPanel.swift`

The slide-in panel. Right-anchored NSPanel with frosted glass.

- [ ] **2.1: Create JARVISPanel.swift**

```swift
// JARVISHUD/Views/JARVISPanel.swift
// The JARVIS Panel — slides in from the right when summoned.
// Contains all SwiftUI content: header, tabs, chat, hive, command input.
// Frosted glass via NSVisualEffectView. Standard NSPanel behavior.

import AppKit
import SwiftUI

class JARVISPanel: NSPanel {
    
    static let defaultWidth: CGFloat = 360
    private var hostingView: NSHostingView<AnyView>?
    
    init(contentView swiftUIView: AnyView) {
        guard let screen = NSScreen.main else {
            super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                       backing: .buffered, defer: false)
            return
        }
        
        // Right-anchored, full height, default width
        let panelFrame = NSRect(
            x: screen.frame.maxX - Self.defaultWidth,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )
        
        super.init(contentRect: panelFrame,
                   styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView],
                   backing: .buffered, defer: false)
        
        // Panel properties
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = true
        self.level = .floating
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.titlebarAppearsTransparent = true
        self.titleVisibility = .hidden
        self.isMovableByWindowBackground = false
        self.becomesKeyOnlyIfNeeded = true
        
        // Frosted glass background
        let visualEffect = NSVisualEffectView(frame: panelFrame)
        visualEffect.material = .hudWindow
        visualEffect.blendingMode = .behindWindow
        visualEffect.state = .active
        visualEffect.wantsLayer = true
        visualEffect.layer?.cornerRadius = 0
        
        // Host the SwiftUI content on top of the glass
        let hosting = NSHostingView(rootView: swiftUIView)
        hosting.translatesAutoresizingMaskIntoConstraints = false
        
        visualEffect.addSubview(hosting)
        NSLayoutConstraint.activate([
            hosting.topAnchor.constraint(equalTo: visualEffect.topAnchor),
            hosting.bottomAnchor.constraint(equalTo: visualEffect.bottomAnchor),
            hosting.leadingAnchor.constraint(equalTo: visualEffect.leadingAnchor),
            hosting.trailingAnchor.constraint(equalTo: visualEffect.trailingAnchor),
        ])
        
        self.contentView = visualEffect
        hostingView = hosting
    }
    
    // MARK: - Panel Behavior
    
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    
    // MARK: - Show/Hide with Animation
    
    func showPanel() {
        guard let screen = NSScreen.main else { return }
        
        // Start offscreen to the right
        let offscreenFrame = NSRect(
            x: screen.frame.maxX,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )
        self.setFrame(offscreenFrame, display: false)
        self.orderFrontRegardless()
        self.alphaValue = 0
        
        // Slide in
        let targetFrame = NSRect(
            x: screen.frame.maxX - Self.defaultWidth,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )
        
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.35
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            self.animator().setFrame(targetFrame, display: true)
            self.animator().alphaValue = 1.0
        })
    }
    
    func hidePanel() {
        guard let screen = NSScreen.main else { return }
        
        let offscreenFrame = NSRect(
            x: screen.frame.maxX,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )
        
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.25
            context.timingFunction = CAMediaTimingFunction(name: .easeIn)
            self.animator().setFrame(offscreenFrame, display: true)
            self.animator().alphaValue = 0
        }, completionHandler: {
            self.orderOut(nil)
        })
    }
    
    var isPanelVisible: Bool {
        return isVisible && alphaValue > 0
    }
}
```

- [ ] **2.2: Commit**

```bash
git add JARVIS-Apple/JARVISHUD/Views/JARVISPanel.swift
git commit -m "feat(hud): add JARVISPanel — frosted glass slide-in panel"
```

---

## Task 3: Simplify HUDView for Panel Mode

**Files:**
- Modify: `JARVIS-Apple/JARVISHUD/Views/HUDView.swift`

The HUDView no longer needs to manage the full-screen overlay, collapsible states, or the Arc Reactor hero mode. It becomes a clean panel content view.

**CRITICAL:** Read the FULL HUDView.swift first. It's ~800 lines. You need to:

1. **Remove** the `isCollapsed` / `hasAutoCollapsed` / collapsible logic entirely
2. **Remove** the expanded hero mode (big title, arc reactor, spacers)
3. **Remove** the `CompactHeaderBar` usage
4. **Keep** the compact header inline (mini reactor, JARVIS text, status, tabs)
5. **Keep** all the existing functionality: transcript, command input, Hive view, voice banner, vision prompt, screen lock animation
6. **Remove** the `onQuit` callback (panel just hides, doesn't quit)
7. **Make** the content area fill all available space (no fixed heights)
8. **Set** `background(Color.clear)` on the body so the frosted glass shows through

The simplified structure should be:

```
VStack(spacing: 0) {
    // Header bar (always shown — mini reactor + JARVIS + status + tabs)
    CompactHeaderBar(...)  // Reuse existing, remove onExpandReactor
    
    // Slim voice status
    VoiceStatusLine(...)
    
    // Content (fills remaining space)
    if hudTab == .chat {
        ScrollView { TranscriptView(...) }
            .layoutPriority(1)
        CommandInput(...)
    } else {
        HiveView(hiveStore: hiveStore)
            .layoutPriority(1)
    }
}
.background(Color.clear)  // Frosted glass shows through
```

- [ ] **3.1: Read and simplify HUDView.swift**

Read the full file. Remove the `if isCollapsed / else` branching. Keep only the compact header + content layout. Remove Arc Reactor, big title, spacers, hero mode.

Remove these state vars:
- `@AppStorage("hudCollapsed") private var isCollapsed`
- `@State private var hasAutoCollapsed`
- The keyboard shortcut background button for collapse toggle

Remove `var onQuit: (() -> Void)? = nil` — the panel hides, doesn't quit.

Remove the `.onAppear` auto-collapse logic.

Keep everything else: transcript, command input, Hive, vision prompt overlay, screen lock overlay, voice status.

- [ ] **3.2: Update CompactHeaderBar**

Remove `onExpandReactor` from CompactHeaderBar since there's no expand mode. The mini reactor dot no longer needs a double-tap gesture.

- [ ] **3.3: Verify build**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/JARVIS-Apple
xcodegen generate
xcodebuild -project JARVIS-Apple.xcodeproj -scheme JARVISHUD -destination "platform=macOS" build 2>&1 | grep -E "error:|BUILD"
```

- [ ] **3.4: Commit**

```bash
git add JARVIS-Apple/JARVISHUD/Views/HUDView.swift JARVIS-Apple/JARVISHUD/Views/CompactHeaderBar.swift
git commit -m "refactor(hud): simplify HUDView for panel mode — remove overlay/collapsible logic"
```

---

## Task 4: Wire Everything in JARVISHUDApp

**Files:**
- Modify: `JARVIS-Apple/JARVISHUD/JARVISHUDApp.swift`

Replace the ClickThroughWindow overlay with LivingBorderWindow + JARVISPanel.

**CRITICAL:** Read the full JARVISHUDApp.swift (~452 lines). Key changes:

- [ ] **4.1: Replace window properties**

Find the `overlayWindow` property declaration and replace:

```swift
// OLD:
private var overlayWindow: ClickThroughWindow?
private var hudVisible = false

// NEW:
private var borderWindow: LivingBorderWindow?
private var panel: JARVISPanel?
private var panelVisible = false
```

- [ ] **4.2: Replace ensureOverlayWindow with new window setup**

Replace the `ensureOverlayWindow()` method:

```swift
private func setupWindows() {
    // Border window — always on, always breathing
    if borderWindow == nil {
        borderWindow = LivingBorderWindow()
    }
    
    // Panel — created once, shown/hidden on demand
    if panel == nil {
        let hudView = HUDView(hiveStore: hiveStore)
            .environmentObject(appState)
        panel = JARVISPanel(contentView: AnyView(hudView))
    }
}
```

- [ ] **4.3: Replace showHUD / hideHUD**

```swift
private func showHUD() {
    setupWindows()
    guard let panel, !panelVisible else { return }
    panelVisible = true
    panel.showPanel()
    statusMenu?.item(withTag: 200)?.title = "Hide JARVIS"
}

private func hideHUD() {
    guard let panel, panelVisible else { return }
    panelVisible = false
    panel.hidePanel()
    statusMenu?.item(withTag: 200)?.title = "Show JARVIS"
}

func togglePanel() {
    if panelVisible { hideHUD() } else { showHUD() }
}
```

- [ ] **4.4: Wire voice commands for show/hide**

Find the `wakeWord.onCommand` callback (around line 90). Add panel control commands BEFORE the existing command routing:

```swift
wakeWord.onCommand = { [weak self] command in
    guard let self else { return }
    let lower = command.lowercased().trimmingCharacters(in: .whitespaces)
    
    // Panel control voice commands
    if lower.contains("show yourself") || lower.contains("show panel") || lower.contains("appear") {
        Task { @MainActor in self.showHUD() }
        return
    }
    if lower.contains("hide") || lower.contains("dismiss") || lower.contains("go away") || lower.contains("disappear") {
        Task { @MainActor in self.hideHUD() }
        return
    }
    
    // Show panel for any other command (organism responds visually)
    Task { @MainActor in
        if !self.panelVisible { self.showHUD() }
    }
    
    // ... existing command routing continues ...
}
```

- [ ] **4.5: Wire keyboard shortcut ⌘⇧J**

Find where keyboard shortcuts are registered (or add in `applicationDidFinishLaunching`):

```swift
// Add global keyboard shortcut for ⌘⇧J
NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
    if event.modifierFlags.contains([.command, .shift]) && event.characters == "j" {
        Task { @MainActor in self?.togglePanel() }
        return nil  // Consume the event
    }
    return event
}
```

- [ ] **4.6: Wire border color to cognitive state**

In `applicationDidFinishLaunching` or after HiveStore is created, observe the cognitive state:

```swift
// Update border color based on Hive cognitive state
// Check periodically or observe HiveStore changes
Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
    guard let self else { return }
    Task { @MainActor in
        let state: BorderState
        if self.appState.pythonBridge.connectionStatus == .disconnected {
            state = .offline
        } else {
            switch self.hiveStore.cognitiveState {
            case .flow: state = .flow
            case .rem: state = .rem
            default: state = .baseline
            }
        }
        self.borderWindow?.updateState(state)
            }
        }
```

- [ ] **4.7: Start with border visible, panel hidden**

In `applicationDidFinishLaunching`, after setup:

```swift
setupWindows()
// Border always visible from launch
// Panel hidden until summoned
```

Remove any calls to `showHUD()` on launch (the old behavior that showed the full-screen overlay immediately).

- [ ] **4.8: Delete old files**

```bash
rm JARVIS-Apple/JARVISHUD/Views/ClickThroughWindow.swift
rm JARVIS-Apple/JARVISHUD/Views/ClickThroughHostingView.swift
```

- [ ] **4.9: Regenerate Xcode project and build**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/JARVIS-Apple
xcodegen generate
xcodebuild -project JARVIS-Apple.xcodeproj -scheme JARVISHUD -destination "platform=macOS" build 2>&1 | grep -E "error:|BUILD"
```

Expected: `BUILD SUCCEEDED`

- [ ] **4.10: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
git add -A JARVIS-Apple/
git commit -m "feat(hud): wire Living Border + Panel — replace full-screen overlay

Living Border window (ignoresMouseEvents=true) renders animated
screen-edge glow. JARVISPanel slides from right on summon.
Voice commands: 'show yourself', 'hide', 'dismiss', 'go away'.
Keyboard: Cmd+Shift+J toggles panel.
Border color tracks Hive cognitive state."
```

---

## Summary

| Task | Component | Dependencies |
|------|-----------|-------------|
| 1 | LivingBorderWindow (new) | None |
| 2 | JARVISPanel (new) | None |
| 3 | Simplify HUDView for panel mode | None |
| 4 | Wire everything in JARVISHUDApp + delete old files | Tasks 1, 2, 3 |

Tasks 1, 2, 3 are independent and can run in parallel. Task 4 wires them together and is the integration point.
