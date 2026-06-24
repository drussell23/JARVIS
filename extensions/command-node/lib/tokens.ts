/**
 * tokens -- the TypeScript view of the Sovereign design tokens.
 *
 * The CANONICAL values live ONLY in `app/globals.css` `:root`. This module
 * exposes each token as a `var(--token)` STRING so inline styles + React
 * Flow node styles (which take JS color strings, not CSS classes) can pull
 * from the same single source of truth. There are NO raw hex values here --
 * a `var(--token)` resolves at render time against `:root`, so a Figma
 * extraction that overwrites `:root` reskins these JS surfaces for free.
 *
 * Keep these names 1:1 with DESIGN_TOKENS.md and the `:root` block.
 */

/** Build a CSS custom-property reference: token('accent') -> 'var(--accent)'. */
export function token(name: string): string {
  return `var(--${name})`;
}

/** Build a reference with a fallback: tokenOr('accent','#06b6d4'). The
 * fallback is only used if the var is undefined at render -- it is NOT a
 * second source of truth (still resolves to the :root value when present).
 * Kept fallback-free in practice; provided for completeness. */
export function tokenOr(name: string, fallback: string): string {
  return `var(--${name}, ${fallback})`;
}

/** Semantic surface + text tokens. */
export const SURFACE = {
  s0: token('surface-0'),
  s1: token('surface-1'),
  s2: token('surface-2'),
  s3: token('surface-3'),
  center: token('surface-center'),
} as const;

export const TEXT = {
  primary: token('text-primary'),
  muted: token('text-muted'),
  inverse: token('text-inverse'),
} as const;

export const BORDER = {
  subtle: token('border-subtle'),
  strong: token('border-strong'),
  bright: token('border-bright'),
  nodeEdge: token('node-edge'),
} as const;

export const ACCENT = {
  base: token('accent'),
  bright: token('accent-bright'),
  deep: token('accent-deep'),
} as const;

/** Trinity repo identity tokens (fill + border per repo). */
export const REPO_TOKENS = {
  jarvis: { fill: token('repo-body'), border: token('repo-body-border') },
  prime: { fill: token('repo-mind'), border: token('repo-mind-border') },
  reactor: {
    fill: token('repo-nerves'),
    border: token('repo-nerves-border'),
  },
  unknown: {
    fill: token('repo-unknown'),
    border: token('repo-unknown-border'),
  },
} as const;

/** Semantic-state tokens (state machines, badges, connection dots). */
export const STATE = {
  ok: token('state-ok'),
  okBright: token('state-ok-bright'),
  danger: token('state-danger'),
  dangerDeep: token('state-danger-deep'),
  warn: token('state-warn'),
  pending: token('state-pending'),
  running: token('state-running'),
  applied: token('state-applied'),
  attention: token('state-attention'),
} as const;

/** Neon-glow box-shadow tokens. */
export const GLOW = {
  primary: token('glow-primary'),
  danger: token('glow-danger'),
  ok: token('glow-ok'),
  warn: token('glow-warn'),
} as const;
