# HUD Collapsible Redesign — Design Spec

**Date:** 2026-04-02
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Approved

## Overview

Redesign the JARVISHUD overlay to reclaim screen real estate. The current layout wastes ~60% on a 72pt title and 440px Arc Reactor, leaving only 250px for content. The new design adds a collapsible toggle: hero mode for demos/boot, compact mode for work.

## Current Problem

```
Title (72pt + subtitle)     ~120px
Spacer                       ~60px
Arc Reactor (440x440)       ~440px
Spacer                       ~60px
Status + Voice               ~80px
Segmented Control            ~30px
Transcript (ONLY 250px!)    ~250px
Command Input                ~50px
─────────────────────────────
Total                       ~1090px
Content ratio: 23%
```

## New Layout

### Expanded (Hero Mode)

Shown on first launch for 5 seconds, or toggled manually. Same dramatic look as current design.

- Title: "J.A.R.V.I.S." 72pt monospaced with green glow (unchanged)
- Subtitle: "JUST A RATHER VERY INTELLIGENT SYSTEM" (unchanged)
- Arc Reactor: 440px with animation (unchanged)
- Status text + connection info below reactor
- Hint text at bottom: "Double-click reactor or ⌘⇧R to collapse"

### Collapsed (Compact Mode)

The working mode — content is king.

**Header bar (~44px):**
```
┌─────────────────────────────────────────────────────┐
│ (●) JARVIS  v3.1 ONLINE     [ Chat | Hive ]        │
└─────────────────────────────────────────────────────┘
```

- Mini reactor dot: 20px circle, radial gradient matching hudState color (green=online, yellow=connecting, red=error, gray=offline). Pulsing glow animation. Double-click to expand.
- "JARVIS" text: 14pt monospaced bold, jarvisGreen color, letter-spacing 2px
- Version + status: 9pt, white 30% opacity
- Segmented control "Chat | Hive": right-aligned in header bar
- Border-bottom: 1px jarvisGreen at 10% opacity

**Voice status bar (~24px):**
```
┌─────────────────────────────────────────────────────┐
│ 🎤 Say "Hey JARVIS"                                │
└─────────────────────────────────────────────────────┘
```
- Slim single line, jarvisGreen at 50% opacity, 10pt mono
- Shows real-time transcript when listening

**Content area (remaining height — ~75-80% of screen):**
- Chat tab: ScrollView with transcript messages (no fixed 250px height — uses all available space via `Spacer` or flexible frame)
- Hive tab: HiveView with cognitive state bar + thread list
- `layoutPriority(1)` to ensure content gets priority space

**Command input (~44px):**
- Same styling as current, border-top separator

**Total compact layout:**
```
Header bar                   ~44px
Voice status                 ~24px
Content (FLEXIBLE!)         ~ALL REMAINING
Command input                ~44px
─────────────────────────────
Content ratio: ~80%
```

### Transition

- Animation: `.spring(duration: 0.4)` — reactor and title scale smoothly, content area expands
- Trigger: double-click mini reactor dot, or keyboard shortcut `⌘⇧R`
- From expanded: title shrinks, reactor scales to 20px dot, slides into header position
- From collapsed: header expands, reactor scales up to 440px, title appears

### Boot Sequence

1. App launches → expanded mode (hero) with current boot animation
2. After 5 seconds (or immediately if `@AppStorage("hudCollapsed")` is true from previous session) → auto-collapse with spring animation
3. First-ever launch: always shows hero for 5s, then collapses
4. Subsequent launches: if user left it collapsed, start collapsed immediately (skip hero). If user left it expanded, start expanded and stay.

Implementation: `@AppStorage("hudCollapsed") private var isCollapsed: Bool = false` + `@State private var isFirstLaunch: Bool = true`

On appear:
```
if isFirstLaunch {
    // Always start expanded on first launch
    isCollapsed = false
    // Auto-collapse after 5s
    DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
        withAnimation(.spring(duration: 0.4)) { isCollapsed = true }
    }
    isFirstLaunch = false
} else {
    // Use persisted state
}
```

### Keyboard Shortcut

`⌘⇧R` (Cmd+Shift+R) toggles collapsed/expanded. Registered in `JARVISHUDApp.swift` via `.keyboardShortcut("r", modifiers: [.command, .shift])`.

## Files Modified

| File | Change |
|------|--------|
| `JARVISHUD/Views/HUDView.swift` | Major rewrite: add `isCollapsed` state, conditional layout (hero vs compact), header bar component, animation, keyboard shortcut |
| `JARVISHUD/Views/CompactHeaderBar.swift` | New: mini reactor + JARVIS text + status + segmented control in one horizontal bar |

## Files NOT Modified

- `ArcReactorView.swift` — unchanged, still used in expanded mode
- `JARVISHUDApp.swift` — no changes needed (keyboard shortcut lives in HUDView)
- `BrainstemLauncher.swift` — no changes
- `HiveView.swift` — no changes (just gets more space)

## Out of Scope

- Sidebar layout (Option B from brainstorm — deferred)
- Custom reactor animations for compact mode
- Drag-to-resize content area
