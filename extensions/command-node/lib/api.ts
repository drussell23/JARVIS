/**
 * HTTP client for the JARVIS observability GET surface, used by the
 * Command Node for initial state, reconnect resync, the poll fallback,
 * and on-demand blast-radius loads.
 *
 * Thin wrapper around the browser's native fetch. Mirrors
 * `extensions/vscode-jarvis/src/api/client.ts` -- read-only, never
 * POSTs. Schema-version mismatches throw SchemaMismatchError.
 */

import {
  AuthorizeRequest,
  BlastRadiusResponse,
  ChallengeResponse,
  ElevationChallenge,
  ElevationVerdict,
  HealthResponse,
  SUPPORTED_SCHEMA_VERSION,
  TaskDetailResponse,
  TaskListResponse,
  isSupportedSchema,
} from './types';

export class ObservabilityError extends Error {
  public readonly status: number;
  public readonly reasonCode: string;
  public constructor(message: string, status: number, reasonCode = '') {
    super(message);
    this.name = 'ObservabilityError';
    this.status = status;
    this.reasonCode = reasonCode;
  }
}

export class SchemaMismatchError extends Error {
  public readonly expected: string;
  public readonly received: string;
  public constructor(received: string) {
    super(
      `schema_version mismatch: expected ${SUPPORTED_SCHEMA_VERSION}, got ${received}`,
    );
    this.name = 'SchemaMismatchError';
    this.expected = SUPPORTED_SCHEMA_VERSION;
    this.received = received;
  }
}

const OP_ID_RE = /^[A-Za-z0-9_\-:.]{1,128}$/;

export interface ClientOptions {
  /** Base URL of the EventChannelServer, e.g. http://127.0.0.1:8765 */
  readonly endpoint: string;
  /** Optional fetch override (tests inject a stub). */
  readonly fetchFn?: typeof fetch;
  /** Abort signal for request cancellation. */
  readonly signal?: AbortSignal;
}

type AnyEnvelope = { schema_version?: string };

export class ObservabilityClient {
  private readonly endpoint: string;
  private readonly fetchFn: typeof fetch;
  private readonly signal?: AbortSignal;

  public constructor(opts: ClientOptions) {
    this.endpoint = trimTrailingSlash(opts.endpoint);
    this.fetchFn = opts.fetchFn ?? fetch;
    this.signal = opts.signal;
  }

  public url(path: string): string {
    return `${this.endpoint}${path.startsWith('/') ? path : `/${path}`}`;
  }

  public async health(): Promise<HealthResponse> {
    return this.get<HealthResponse>('/observability/health');
  }

  public async taskList(): Promise<TaskListResponse> {
    return this.get<TaskListResponse>('/observability/tasks');
  }

  public async taskDetail(opId: string): Promise<TaskDetailResponse> {
    if (!OP_ID_RE.test(opId)) {
      throw new ObservabilityError(
        `malformed op_id: ${opId}`,
        400,
        'client.malformed_op_id',
      );
    }
    return this.get<TaskDetailResponse>(
      `/observability/tasks/${encodeURIComponent(opId)}`,
    );
  }

  public async blastRadius(opId: string): Promise<BlastRadiusResponse> {
    if (!OP_ID_RE.test(opId)) {
      throw new ObservabilityError(
        `malformed op_id: ${opId}`,
        400,
        'client.malformed_op_id',
      );
    }
    return this.get<BlastRadiusResponse>(
      `/observability/blast-radius/${encodeURIComponent(opId)}`,
    );
  }

  private async get<T extends AnyEnvelope>(path: string): Promise<T> {
    const url = this.url(path);
    let response: Response;
    try {
      response = await this.fetchFn(url, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        signal: this.signal,
        cache: 'no-store',
      });
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      throw new ObservabilityError(
        `fetch failed: ${msg}`,
        -1,
        'client.network_error',
      );
    }

    if (!response.ok) {
      const code = await extractReasonCode(response);
      throw new ObservabilityError(
        `${path} returned ${response.status}`,
        response.status,
        code,
      );
    }

    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ObservabilityError(
        `JSON parse failed for ${path}`,
        response.status,
        'client.invalid_json',
      );
    }

    if (!isSupportedSchema(body as AnyEnvelope)) {
      throw new SchemaMismatchError(
        (body as AnyEnvelope)?.schema_version ?? '(missing)',
      );
    }

    return body as T;
  }
}

/**
 * Raised when the biometric write-path is gated off (the backend returns
 * 404 on the challenge endpoint when `JARVIS_COMMAND_NODE_AUTH_ENABLED`
 * is false). The UI catches this to show "biometric auth disabled"
 * instead of crashing.
 */
export class AuthDisabledError extends Error {
  public constructor(message = 'biometric auth disabled') {
    super(message);
    this.name = 'AuthDisabledError';
  }
}

const PR_ID_RE = /^[A-Za-z0-9_\-:.#/]{1,256}$/;

/**
 * The write-path client for the biometric AuthorizeElevation flow.
 *
 * Deliberately SEPARATE from the read-only ObservabilityClient: this is
 * the only surface that issues a write (the POST). It still does not
 * decide anything -- it transmits the captured audio + nonce and reflects
 * the backend verdict. Fail-CLOSED: a gated-off backend (404) surfaces as
 * AuthDisabledError; the verdict POST returns the typed verdict for both
 * 200 (AUTHORIZED) and 403 (REJECTED) -- only transport/parse failures
 * throw.
 */
export class BiometricAuthClient {
  private readonly endpoint: string;
  private readonly fetchFn: typeof fetch;
  private readonly signal?: AbortSignal;

  public constructor(opts: ClientOptions) {
    this.endpoint = trimTrailingSlash(opts.endpoint);
    this.fetchFn = opts.fetchFn ?? fetch;
    this.signal = opts.signal;
  }

  private url(path: string): string {
    return `${this.endpoint}${path.startsWith('/') ? path : `/${path}`}`;
  }

  /**
   * Fetch a FRESH single-use challenge for a PR. The query params bind the
   * challenge to this exact mutation + blast radius. A 404 means the
   * write-path is gated off -> AuthDisabledError (the dashboard stays
   * read-only).
   */
  public async challenge(
    prId: string,
    astMutationId: string,
    blastRadiusHash: string,
  ): Promise<ElevationChallenge> {
    if (!PR_ID_RE.test(prId)) {
      throw new ObservabilityError(
        `malformed pr_id: ${prId}`,
        400,
        'client.malformed_pr_id',
      );
    }
    const qs = new URLSearchParams({
      ast_mutation_id: astMutationId,
      blast_radius_hash: blastRadiusHash,
    });
    const path = `/command-node/elevation/${encodeURIComponent(prId)}/challenge?${qs.toString()}`;
    const url = this.url(path);

    let response: Response;
    try {
      response = await this.fetchFn(url, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        signal: this.signal,
        cache: 'no-store',
      });
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      throw new ObservabilityError(
        `challenge fetch failed: ${msg}`,
        -1,
        'client.network_error',
      );
    }

    if (response.status === 404) {
      throw new AuthDisabledError();
    }
    if (!response.ok) {
      const code = await extractReasonCode(response);
      throw new ObservabilityError(
        `challenge returned ${response.status}`,
        response.status,
        code,
      );
    }

    let body: ChallengeResponse;
    try {
      body = (await response.json()) as ChallengeResponse;
    } catch {
      throw new ObservabilityError(
        'JSON parse failed for challenge',
        response.status,
        'client.invalid_json',
      );
    }
    if (!isSupportedSchema(body)) {
      throw new SchemaMismatchError(body?.schema_version ?? '(missing)');
    }
    if (body.challenge === undefined || body.challenge === null) {
      throw new ObservabilityError(
        'challenge response missing challenge',
        response.status,
        'client.malformed_challenge',
      );
    }
    return body.challenge;
  }

  /**
   * POST the captured audio + the single-use nonce. Returns the typed
   * verdict for BOTH 200 (AUTHORIZED) and 403 (REJECTED) -- the decision
   * lives in the body, not the status code. Only transport / non-verdict
   * errors throw.
   */
  public async authorize(req: AuthorizeRequest): Promise<ElevationVerdict> {
    const url = this.url('/command-node/authorize-elevation');
    let response: Response;
    try {
      response = await this.fetchFn(url, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(req),
        signal: this.signal,
        cache: 'no-store',
      });
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      throw new ObservabilityError(
        `authorize POST failed: ${msg}`,
        -1,
        'client.network_error',
      );
    }

    if (response.status === 404) {
      throw new AuthDisabledError();
    }

    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ObservabilityError(
        'JSON parse failed for authorize',
        response.status,
        'client.invalid_json',
      );
    }

    const verdict = body as Partial<ElevationVerdict> | null;
    // A well-formed verdict body is the authority for both 200 and 403.
    if (
      verdict !== null &&
      typeof verdict.decision === 'string' &&
      typeof verdict.reason === 'string'
    ) {
      return verdict as ElevationVerdict;
    }

    // No verdict body on a non-OK status -> treat as a transport error.
    throw new ObservabilityError(
      `authorize returned ${response.status} without a verdict`,
      response.status,
      'client.malformed_verdict',
    );
  }
}

function trimTrailingSlash(url: string): string {
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

async function extractReasonCode(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { reason_code?: string };
    return body.reason_code ?? '';
  } catch {
    return '';
  }
}
