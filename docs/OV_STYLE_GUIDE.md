# O+V · Terminal Visual Style Guide

**Ouroboros + Venom — the design grammar for a proactive autonomous cockpit, not a reactive chat.**

> Claude Code is a *conversation*: you speak, it works, it answers. O+V is an *organism*: a Sentinel probes, a
> Supervisor arms, a Swarm chunks — with you as the overseer, not the initiator. Every rule below exists to make
> that continuous, self-driven activity **legible at a glance**. Steal Claude Code's restraint; reject its
> turn-based transcript.

This document is the **single source of truth** for the O+V CLI/TUI look. The palette, glyph vocabulary, and
severity ladder here are wired into code at `backend/core/ouroboros/ui/theme.py` (the Reactive Theme Singleton)
and consumed by `event_breadcrumb_registry.py`. Change the look *here first*, then the tokens in code.

---

## 01 · First Principles

Six rules. When a design decision is unclear, the one that serves these wins.

| # | Principle | What it means |
|---|-----------|---------------|
| ◈ | **Cockpit, not chat** | The resting state is a *live activity canvas*, not an empty prompt. The organism is always doing something; the UI's job is to show what it just decided. |
| ⎿ | **Hierarchy by glyph** | Structure comes from *glyphs + typography*, not borders everywhere. A filled marker opens a thought; a corner glyph subordinates. One cheap, legible system. |
| ✓ | **Green is an outcome** | Reserve the Venom green for *things that succeeded*. If everything is green, nothing is. Default text is soft off-white; secondary is dim. |
| ⚡ | **Motion is the polish** | One in-place spinner, never six stacked log lines. Stream tokens. The *movement* is what separates a product from a script. |
| ◇ | **Framed where it counts** | The background telemetry earns a *bounded panel* (Zone 1). The prompt stays anchored (Zone 2). Nothing else gets a box unless it needs one. |
| · | **Restraint by default** | Terse one-liners; detail on demand (`/expand`). Emojis allowed but rationed. Silence is a valid state — an idle organism looks calm, not blank. |

---

## 02 · Palette

Grounded in the **Venom signature** (green → purple) over a near-black terminal ground with a faint cool bias.
The accent is cyan; semantic colors map to event severity. Deliberately **dark-only** — the subject is a dark
terminal, so a light mode would be incoherent.

**The Venom gradient** (`#5EE06A → #43D6D0 → #A371F7`) is for the wordmark, active-route accents, and the boot
sweep. **Never body text.**

### Core

| Role | Hex | Rich token |
|------|-----|-----------|
| Ground | `#0A0E0D` | — (terminal bg) |
| Surface | `#111917` | — |
| Hairline | `#1E2B28` | `rule` |
| Ink (primary) | `#DBE6E1` | `body` / `default` |
| Muted (secondary) | `#6C7D77` | `muted` / `grey50` |
| Faint | `#47554F` | `bright_black` |

### Brand + accent

| Role | Hex | Rich token |
|------|-----|-----------|
| Venom green | `#5EE06A` | `venom_green` |
| Venom purple | `#A371F7` | `venom_purple` |
| Cyan (accent/borders) | `#43D6D0` | `accent` |

### Semantic — the severity ladder

| Role | Hex | Rich token |
|------|-----|-----------|
| Critical | `#F85149` | `danger` / `crit` |
| Important | `#E3B341` | `warning` |
| Info | `#58B0F8` | `info` |
| Verbose | `#6C7D77` | `verbose` |
| Success | `#3FB950` | `success` |

---

## 03 · Typography

Two roles. **Monospace** carries everything inside the organism — every event line, status, glyph, number —
because alignment *is* the information. A humanist **sans** carries only human chrome (help prose). Numbers use
tabular figures so columns don't dance.

```
mono · TUI body      ◈ chunk 313/500 ✓ factorial (mathy.py)
mono · label         PHASE  cost  runway
mono · numeric       $0.47 / $2.50    5,000,000 tok    p50 8.9s
sans · help prose    Reserved for multi-sentence explanation only.
```

**Type stack** — mono: `SF Mono / JetBrains Mono / Menlo`; sans: `system-ui`. No web fonts.

---

## 04 · The Glyph Grammar

The heart of the system. Each glyph has **one meaning**, everywhere. Learn eleven marks and the whole organism
is readable. Color is layered on top by severity (§05). Each has an ASCII degradation so 16-color / no-color
terminals keep identical geometry.

| Glyph | ASCII | Name | Means | Example |
|:---:|:---:|------|-------|---------|
| `⏺` | `*` | open | A primary action / status begins | `⏺ attached — phase IDLE` |
| `⎿` | `-` | continue | A line subordinate to the one above | `⎿ liquidity anthropic 5.0M` |
| `◆` | `#` | state (filled) | A lifecycle transition took hold | `◆ supervisor armed` |
| `◇` | `o` | state (hollow) | A lifecycle stood down | `◇ supervisor disarmed` |
| `⚡` | `!` | ignite | A high-energy autonomous fire | `⚡ recovery → soak launched` |
| `◈` | `+` | tick | One unit of map-reduce progress | `◈ chunk 4/7 ✓ factorial` |
| `↻` | `~` | cycle | Resume / restart / self-heal | `↻ Sentinel self-healed` |
| `☠` | `X` | poison | Quarantined / dead-lettered | `☠ poison chunk DLQ'd` |
| `✓` | `OK` | done | Completed / verified | `✓ soak run done` |
| `⚠` | `!` | warn | Degraded but not fatal | `⚠ a provider runway is dry` |
| `·` | `-` | trace | Verbose telemetry (piped stdout) | `· probe DEGRADED stage=pass1` |

Status dots `●` online / `●` disconnected and trend `▲`/`▼` are the two exceptions that also live in the HUD
halo + menu-bar.

---

## 05 · Severity → Color (derive, don't pick)

Every event carries a **severity rank**; the color is *derived*, never hand-picked per message. This is what
keeps 149 event types coherent — one table, not 149 decisions. The `/breadcrumbs` verbosity floor filters by
this same rank. Defined once in the theme as `SEVERITY_STYLE`.

| Rank | Level | Reads as | Fires on |
|:---:|-------|----------|----------|
| 3 | **CRITICAL** | red · bold | trips · exhaustion · quarantine · anomalies |
| 2 | **IMPORTANT** | yellow | degradation · throttle · drift · armed/disarmed |
| 1 | **INFO** | cyan | posture · plans · resume · learning |
| 0 | **VERBOSE** | dim gray | telemetry · heartbeats · per-chunk ticks |

Tailored descriptors may override the derived color for a specific event (e.g. `awe_soak_launched` is green-bold,
not red, despite being CRITICAL-urgent) — but the override references a **theme token**, never a raw literal.

---

## 06 · The Bipartite Layout

The canonical running surface. **Zone 1 — the Proactive Canvas**: a rounded panel every background event
auto-scrolls into. **Zone 2 — the Command Deck**: a permanently anchored prompt. Driven by a full-screen
`prompt_toolkit` app so streaming telemetry can never corrupt a keystroke.

```
╭─ ◇ O+V · proactive canvas ────────────────────────────────────────╮
│ ◆ supervisor armed — 1 pending, DW DEGRADED → Sentinel pid 4242    │
│ · sentinel telemetry — probe DEGRADED stage=pass1 (ttft 71s)       │
│ ⚡ awe soak launched — doubleword recovery → soak 22737b8f         │
│ ↻ soak resumed — 0/7 done, 7 to go                                 │
│ ◈ chunk 1/7 ✓ factorial (mathy.py)                                 │
│ ◈ chunk 2/7 ✓ is_even (mathy.py)                                   │
│ ☠ poison chunk DLQ'd ✗ slow_sum (widgets.py) — ctx blown           │
│ ✓ soak run done — 6 committed, 1 quarantined, 0 failed             │
╰──────────────────────────────────────────────────── 9 events ─────╯
› type a verb or plain text   ·   ⌃C detach · wake for voice
```

| | Zone 1 · Canvas | Zone 2 · Deck |
|---|-----------------|---------------|
| **border** | rounded, cyan-dim (state-reactive) | none (a single row) |
| **content** | bounded ring, tail auto-scrolls | editable prompt + affordances |
| **owner** | the organism (writes) | the human (types) |
| **motion** | new events slide in at the bottom | caret blink only — stays still |

### State-Reactive border

The canvas border color is **not static** — it reflects the organism's meta-state, updated in place (zero
flicker, no teardown) as broker events flow. This is the Reactive Theme Singleton (`theme.get_reactive_theme()`).

| Meta-state | Border | Triggered by |
|------------|--------|--------------|
| `DORMANT` | dim gray | idle / `supervisor_disarmed` |
| `ARMED` | amber | `supervisor_armed` |
| `SOAKING` | venom green | `awe_soak_launched` / `soak_chunk_committed` |
| `DEGRADED` | red | `provider_state_changed` → DEGRADED |
| `HEALTHY` | cyan | `provider_state_changed` → HEALTHY |

---

## 07 · Density & Motion

Where "basic" becomes "product." The single biggest lever: **collapse repetition into one updating line**, and
let detail expand on request.

```
the boot wake — one in-place spinner, not six stacked lines:
⠹ waking organism · 27s                    ← updates in place
● organism live · 27s                       ← resolves to one ✓

collapsed op block — terse by default, /expand o-3 for the diff:
⏺ edit widgets.py · +4 −1 · applied · o-3
```

- Spinners refresh **in place**.
- Progress shows `done/total`, not a new line each tick.
- Op blocks collapse to one line + a `ref`.
- Stream model output token-by-token.

---

## 08 · Do & Don't

| ✗ Avoid | ✓ Prefer |
|---------|----------|
| A flat wall of one green — no hierarchy | Dim → ink → accent hierarchy |
| Stacked identical log lines (`waking · 0s / 5s / 11s`) | One in-place spinner → single result |
| Boxes around everything | One framed zone (the canvas); rest flows |
| Raw tracebacks in the operator surface | A calm breadcrumb + a pull-verb |
| Green as decoration | Green **only** on ✓ / committed / live |
| A blank screen while busy | An idle-but-alive resting state |

---

## 09 · Token Cheat-Sheet

The whole system, compressed — mirrors `theme.py`.

```
# ground / ink
ground   #0A0E0D   surface  #111917   hairline #1E2B28
ink      #DBE6E1   muted    #6C7D77   faint    #47554F

# brand + accent
venom.green #5EE06A   venom.purple #A371F7   cyan #43D6D0

# severity ladder (rank → color, bold at 3)
3 CRITICAL #F85149   2 IMPORTANT #E3B341
1 INFO     #58B0F8   0 VERBOSE   #6C7D77   ok #3FB950

# state → border accent (reactive)
DORMANT muted   ARMED amber   SOAKING venom.green
DEGRADED crit   HEALTHY cyan

# glyphs — one meaning each (ASCII fallback in theme._GLYPHS)
⏺ open   ⎿ continue   ◆◇ state   ⚡ ignite   ◈ tick
↻ cycle  ☠ poison     ✓ done     ⚠ warn     · trace

# type
mono = TUI body · labels · numbers (tabular)
sans = human prose only

# layout
zone1 = rounded cyan panel · bounded ring · auto-scroll   (organism writes)
zone2 = anchored `› ` prompt · one row                    (human types)
rule  = green == outcome · one spinner not six · framed where it counts
```

---

*O+V Terminal Style Guide · v1 · ouroboros + venom · a proactive cockpit.*
