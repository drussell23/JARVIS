/**
 * Tailwind config for the Sovereign Command Node.
 *
 * The theme is EXTENDED from the CSS-variable token block in
 * `app/globals.css` `:root` -- NOT from raw hex. Every color/spacing/etc.
 * resolves to `var(--token)`, so the canonical values live in exactly one
 * place and a Figma extraction that overwrites `:root` reskins the tailwind
 * utilities for free. See DESIGN_TOKENS.md for the canonical names.
 *
 * @type {import('tailwindcss').Config}
 */
module.exports = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './hooks/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        surface: {
          0: 'var(--surface-0)',
          1: 'var(--surface-1)',
          2: 'var(--surface-2)',
          3: 'var(--surface-3)',
          center: 'var(--surface-center)',
        },
        ink: {
          primary: 'var(--text-primary)',
          muted: 'var(--text-muted)',
          inverse: 'var(--text-inverse)',
        },
        accent: {
          DEFAULT: 'var(--accent)',
          bright: 'var(--accent-bright)',
          deep: 'var(--accent-deep)',
        },
        repo: {
          body: 'var(--repo-body)',
          mind: 'var(--repo-mind)',
          nerves: 'var(--repo-nerves)',
        },
        state: {
          ok: 'var(--state-ok)',
          danger: 'var(--state-danger)',
          warn: 'var(--state-warn)',
          pending: 'var(--state-pending)',
          attention: 'var(--state-attention)',
        },
      },
      borderColor: {
        subtle: 'var(--border-subtle)',
        strong: 'var(--border-strong)',
        bright: 'var(--border-bright)',
      },
      boxShadow: {
        'glow-primary': 'var(--glow-primary)',
        'glow-danger': 'var(--glow-danger)',
        'glow-ok': 'var(--glow-ok)',
        'glow-warn': 'var(--glow-warn)',
        panel: 'var(--shadow-panel)',
        modal: 'var(--shadow-modal)',
      },
      fontFamily: {
        mono: 'var(--font-mono)',
        sans: 'var(--font-sans)',
      },
      borderRadius: {
        xs: 'var(--radius-xs)',
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
        pill: 'var(--radius-pill)',
      },
    },
  },
  plugins: [],
};
