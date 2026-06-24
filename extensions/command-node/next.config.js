/**
 * Next.js config for the Sovereign Command Node (Phase 1, read-only).
 *
 * The dashboard is a pure client-side consumer of the existing JARVIS
 * /observability surface. No server-side data fetching, no API routes
 * here -- the backend URL is resolved at runtime from the
 * NEXT_PUBLIC_OBSERVABILITY_BASE env var (see lib/config.ts). There is
 * NO hardcoded localhost in source.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig = {
  reactStrictMode: true,
  // The observability backend is a separate process; we never proxy or
  // rewrite to it from Next -- the browser talks to it directly via the
  // env-configured base URL. CORS on the backend (loopback allowlist)
  // governs access.
};

module.exports = nextConfig;
