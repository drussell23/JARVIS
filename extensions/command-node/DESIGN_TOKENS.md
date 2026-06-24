# Sovereign Design Tokens

The **canonical** design-token list for the Sovereign Command Node. The
**single source of truth** is the `:root` block in `app/globals.css`. Every
component (CSS class or inline/JS style) resolves to a `var(--token)` -- the
ONLY place raw hex / raw values live is that `:root` block.

A Figma extraction should map its variables to **these exact names** so it can
overwrite `:root` 1:1 without touching a single component. The TypeScript
mirror (`var(--token)` strings for inline styles + React Flow node styles)
lives in `lib/tokens.ts`.

> Aesthetic: a military-grade AGI console -- deep space-black layered
> surfaces, an electric neon-cyan primary with layered neon-glow shadows, the
> Trinity repo identity (Body=blue / Mind=amber / Nerves=violet), and an
> explicit semantic-state palette. Dark is the default (and only) theme; the
> structure is mode-agnostic (`[data-theme=...]` can redefine the same names).

## Primitive scale (referenced only by semantic tokens)

Raw palette steps. Components must NOT use these directly -- they exist so the
semantic tokens below have a coherent ramp to point at.

`--space-black --space-900 --space-800 --space-700 --space-600 --space-500`
`--ink-700 --ink-600 --ink-500 --ink-400 --ink-300 --ink-200 --ink-100 --ink-050 --white`
`--cyan-bright --cyan-core --cyan-deep`
`--blue-core --blue-bright --blue-deep --amber-core --amber-bright --violet-core --violet-bright`
`--green-core --green-bright --red-core --red-deep --red-mid --warn-core --orange-core`

## Semantic tokens (use THESE)

### Surfaces (layered elevation, near-black)
| Token | Role |
|-------|------|
| `--surface-0` | App canvas (deepest) |
| `--surface-1` | Panels / cards |
| `--surface-2` | Inset / recessed regions |
| `--surface-3` | Raised chips / buttons |
| `--surface-overlay` | Modal scrim base |
| `--surface-center` | Blast-graph center node (deep neutral) |

### Borders & text
| Token | Role |
|-------|------|
| `--border-subtle` / `--border-strong` / `--border-bright` | Dividers / panel edges / bright hairlines |
| `--node-edge` | Graph-node hairline overlay (translucent) |
| `--text-primary` / `--text-muted` / `--text-inverse` | Body text / secondary / on-color |
| `--text-on-accent` | Text on the neon accent fill |

### Primary / accent (electric neon)
| Token | Role |
|-------|------|
| `--accent` | Primary accent (cyan core) |
| `--accent-bright` | Active / highlighted accent |
| `--accent-deep` | Pressed / border accent |
| `--accent-ring` | Focus-ring rgba for the active-state glow |

### Trinity repo identity (load-bearing -- keep)
| Token | Repo |
|-------|------|
| `--repo-body` / `--repo-body-border` | jarvis = **Body** (blue) |
| `--repo-mind` / `--repo-mind-border` | prime = **Mind** (amber) |
| `--repo-nerves` / `--repo-nerves-border` | reactor = **Nerves** (violet) |
| `--repo-unknown` / `--repo-unknown-border` | Open-vocabulary fallback (neutral) |

### Semantic state
| Token | State |
|-------|-------|
| `--state-ok` / `--state-ok-bright` | AUTHORIZED / complete (green) |
| `--state-danger` / `--state-danger-deep` | REJECTED / FRACTURE (red) / blocked |
| `--state-warn` | Warning (amber) |
| `--state-pending` | Pending / idle |
| `--state-running` | Running |
| `--state-applied` | Applied |
| `--state-attention` | Quarantine / reconnecting / **Immutable Orange** (orange) |

### Neon-glow box-shadows (the console signature)
| Token | Role |
|-------|------|
| `--glow-primary` | Active / accent glow (layered cyan) |
| `--glow-danger` | REJECTED / FRACTURE glow (red) |
| `--glow-ok` | AUTHORIZED glow (green) |
| `--glow-warn` | Immutable Orange / warning glow |
| `--shadow-panel` | Standard panel drop shadow |
| `--shadow-modal` | Modal drop shadow |

### Typography
| Token | Role |
|-------|------|
| `--font-mono` | Technical / data (mono stack) |
| `--font-sans` | Chrome / prose (sans stack) |
| `--text-xs --text-sm --text-base --text-md --text-lg --text-xl` | Type scale (11/12/14/16/20/28px) |

### Radius / spacing / borders
| Token | Role |
|-------|------|
| `--radius-xs --radius-sm --radius-md --radius-lg --radius-pill` | 3 / 6 / 8 / 10 / 999px |
| `--space-1 .. --space-8` | 2 / 4 / 6 / 8 / 12 / 16 / 24px scale |
| `--border-width` | 1px base border |

### Texture & z-index
| Token | Role |
|-------|------|
| `--scanline-color` | Subtle cyan scanline overlay |
| `--grid-color` | Subtle console grid overlay |
| `--z-modal` / `--z-toast` | Stacking order |

## Tailwind mapping

`tailwind.config.js` extends the theme from these vars (NOT from hex), so
utilities like `bg-surface-1`, `text-accent-bright`, `shadow-glow-danger`,
`border-subtle`, `font-mono`, and `rounded-md` all resolve to the same
`:root` values. See `tailwind.config.js`.
