/**
 * Runtime config resolution for the Command Node.
 *
 * NO hardcoded localhost in source: the backend base URL comes from
 * NEXT_PUBLIC_OBSERVABILITY_BASE. We fall back to a loopback default
 * built from NEXT_PUBLIC_OBSERVABILITY_PORT (also env-driven) only so a
 * fresh `npm run dev` works out of the box -- but both are overridable.
 *
 * All caps (event buffer size, reconnect backoff ceiling, poll
 * interval, max consecutive failures before poll fallback) are
 * env-tunable via NEXT_PUBLIC_* so nothing operational is baked in.
 */

const DEFAULT_PORT = '8765';

function envStr(key: string, fallback: string): string {
  const raw = process.env[key];
  return raw !== undefined && raw !== '' ? raw : fallback;
}

function envInt(key: string, fallback: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw === '') {
    return fallback;
  }
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

export interface CommandNodeConfig {
  /** Base URL of the EventChannelServer, e.g. http://127.0.0.1:8765 */
  readonly observabilityBase: string;
  /** Max in-memory SSE event frames to retain (ring buffer). */
  readonly eventBufferCap: number;
  /** Ceiling for the exponential reconnect backoff (ms). */
  readonly reconnectMaxBackoffMs: number;
  /** Poll fallback interval (ms) once SSE is deemed unreliable. */
  readonly pollIntervalMs: number;
  /** Consecutive SSE failures before degrading to poll fallback. */
  readonly maxFailuresBeforePoll: number;
}

export function resolveConfig(): CommandNodeConfig {
  const port = envStr('NEXT_PUBLIC_OBSERVABILITY_PORT', DEFAULT_PORT);
  const base = envStr(
    'NEXT_PUBLIC_OBSERVABILITY_BASE',
    `http://127.0.0.1:${port}`,
  );
  return {
    observabilityBase: base.endsWith('/') ? base.slice(0, -1) : base,
    eventBufferCap: envInt('NEXT_PUBLIC_EVENT_BUFFER_CAP', 500),
    reconnectMaxBackoffMs: envInt(
      'NEXT_PUBLIC_RECONNECT_MAX_BACKOFF_MS',
      15000,
    ),
    pollIntervalMs: envInt('NEXT_PUBLIC_POLL_INTERVAL_MS', 5000),
    maxFailuresBeforePoll: envInt('NEXT_PUBLIC_MAX_FAILURES_BEFORE_POLL', 5),
  };
}
