# JARVIS Living Border — The Intelligent Organism UX

**Date:** 2026-04-03
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Approved
**Philosophy:** JARVIS is not an app. It is a living AI organism that inhabits macOS. It sees, understands, remembers, and acts in real time.

## The Vision

JARVIS is an intelligent living organism that inhabits your MacBook. It is always alive — seeing your screen, understanding context, remembering patterns, acting when needed. The interface is not a window or an app — it is an ambient presence woven into macOS itself.

This goes beyond Apple Intelligence and Siri. Those are tools you invoke. JARVIS is something that **lives** in your Mac.

## 1. The Living Border (Heartbeat)

A subtle ambient glow wraps the entire screen edge — JARVIS's heartbeat. Takes up zero pixels of workspace. The color and breathing speed communicate state without any UI.

| State | Color | Breath Speed | Meaning |
|-------|-------|-------------|---------|
| **BASELINE** | Green | 4s slow breath | Alive, all quiet, watching |
| **REM** | Purple | 3s gentle pulse | Council reviewing (idle thinking) |
| **FLOW** | Orange | 2s active pulse | Agents debating (active thinking) |
| **ALERT** | Red | 1.5s rapid pulse | Needs your attention |
| **OFFLINE** | Dim gray | No animation | Dormant / brainstem not running |

**Implementation:** A borderless, click-through NSWindow at `.screenSaver` level. Contains only a `CALayer` with animated `box-shadow`/border glow. Ignores all mouse events. Renders the glow using CoreAnimation with breathing keyframes. Zero impact on desktop interaction.

## 2. Voice as Primary Interface

Voice is the natural way to interact with a living organism. Keyboard shortcuts are a fallback.

**Summon panel:**
- "Hey JARVIS" (existing wake word → show panel)
- "Hey JARVIS, show yourself"
- "Hey JARVIS, what's happening?" (shows panel + answers)

**Dismiss panel:**
- "Hey JARVIS, hide"
- "Hey JARVIS, dismiss"
- "Hey JARVIS, go away"
- Click outside the panel
- ⌘⇧J (keyboard fallback — must actually work)

**Commands without panel:**
- "Hey JARVIS, what's the Hive status?" → speaks answer, border briefly brightens, no panel needed
- "Hey JARVIS, open Chrome" → executes, no panel needed
- "Hey JARVIS, analyze my screen" → processes, speaks result

The organism can respond with voice only (no panel) for quick queries, or summon the panel for complex interactions.

## 3. The Panel (Face-to-Face)

When summoned, the panel slides in from the right edge of the screen. It's the organism showing itself — a window into its mind.

**Behavior:**
- Slides in from right with spring animation (0.4s, cubic-bezier)
- Desktop does NOT push left — the panel overlays the right edge with frosted glass blur
- Click-through on the panel's transparent areas (only UI elements capture clicks)
- Width: 340px default, resizable by dragging left edge
- Frosted glass: `backdrop-filter: blur(40px)` equivalent via NSVisualEffectView
- Subtle green accent line on the left edge of the panel

**Content:**
- Mini reactor dot (22px, pulsing, matches border color)
- "JARVIS" title + connection status
- Voice status line ("Say Hey JARVIS" / "Listening..." / real-time transcript)
- Chat | Hive segmented tabs
- Chat: conversation with JARVIS
- Hive: Trinity agent debate feed (from the Hive backend we built)
- Command input at bottom

**Dismiss animation:** Panel slides out to the right, opacity fades, returns to just the Living Border.

## 4. The Organism's Senses

JARVIS sees, hears, and understands in real time:

- **Sees:** Persistent 1fps screen capture via ScreenCaptureKit (already built). The organism always knows what's on screen.
- **Hears:** Wake word detection via SFSpeechRecognizer (already built). Always listening for "Hey JARVIS".
- **Remembers:** Episodic memory, Hive thread history, REM Council findings (already built in backend).
- **Acts:** Ghost Hands (UI automation), VLA pipeline (vision-language actions), Ouroboros (code synthesis).

The Living Border reflects all of this — when the organism is seeing and processing, the border breathes faster. When it acts, the border brightens momentarily.

## 5. Architecture Changes

### Replace Current Overlay

The current `ClickThroughWindow` full-screen overlay is removed entirely. Replaced with two windows:

**Window 1: Border Window (always on)**
- Borderless NSWindow at `.screenSaver` level
- Covers full screen but `ignoresMouseEvents = true` (absolute)
- Contains only the animated border glow (CAShapeLayer with animated stroke)
- Never captures input. Never blocks desktop. Never visible as a "window."
- Runs as long as the app is alive

**Window 2: Panel Window (on demand)**
- Standard NSPanel with `.floating` level
- Right-anchored, 340px wide, full height
- `NSVisualEffectView` for frosted glass background
- Contains all SwiftUI content (header, tabs, chat, hive, input)
- Hidden by default. Shown/hidden via voice or ⌘⇧J
- Proper first responder handling so keyboard shortcuts work

### Voice Command Extensions

Add to `WakeWordListener` or command processor:
- Detect "show", "hide", "dismiss", "go away" after wake word
- Route to panel show/hide before other command processing
- "What's happening" / "status" → speak summary, optionally show panel

## 6. Files to Change

| File | Change |
|------|--------|
| Remove: `JARVISHUD/Views/ClickThroughWindow.swift` | Delete — replaced by Border + Panel windows |
| Remove: `JARVISHUD/Views/ClickThroughHostingView.swift` | Delete — no longer needed |
| Create: `JARVISHUD/Views/LivingBorderWindow.swift` | Border glow window with CoreAnimation |
| Create: `JARVISHUD/Views/JARVISPanel.swift` | Right-anchored NSPanel with frosted glass |
| Modify: `JARVISHUD/JARVISHUDApp.swift` | Replace overlay window with border + panel |
| Modify: `JARVISHUD/Views/HUDView.swift` | Simplify — no longer manages full-screen overlay |
| Modify: `JARVISHUD/Services/BrainstemLauncher.swift` | Route hive events to panel's HiveStore |
| Modify: `JARVISHUD/Services/WakeWordListener.swift` | Add show/hide voice commands |
| Modify: `JARVISHUD/Services/AppState.swift` | Expose border color state based on cognitive state |

## 7. Voice Commands for Panel Control

| Phrase | Action |
|--------|--------|
| "Hey JARVIS" + any command | Show panel + execute command |
| "Hey JARVIS, show yourself" | Show panel |
| "Hey JARVIS, hide" | Hide panel |
| "Hey JARVIS, dismiss" | Hide panel |
| "Hey JARVIS, go away" | Hide panel |
| "Hey JARVIS, what's happening?" | Speak status summary (panel optional) |
| "Hey JARVIS, show the Hive" | Show panel on Hive tab |

## 8. Border Color Binding

The Living Border color is driven by HiveStore's cognitive state + connection status:

```
if connectionStatus == .offline → gray (no animation)
else if cognitiveState == .flow → orange (2s pulse)
else if cognitiveState == .rem → purple (3s pulse)  
else if alertActive → red (1.5s pulse)
else → green (4s breath) // BASELINE
```

The border window observes these states and updates its CAAnimation accordingly.

## 9. Out of Scope (v1)

- Border glow intensity based on voice volume (future: border reacts to your voice)
- Border traveling light effect (light that moves around the edge — future enhancement)
- Multi-monitor border (v1 = primary display only)
- Panel on left side option
- Menu bar integration (the border replaces the need for menu bar indicators)
