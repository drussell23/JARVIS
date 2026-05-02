/**
 * HTTP client for the JARVIS observability GET surface (Slice 1).
 *
 * Thin wrapper around Node's native fetch. Every request carries an
 * AbortSignal so the extension can cancel in-flight calls on
 * disconnect or config change. Schema-version mismatches throw
 * SchemaMismatchError so callers can surface an operator-visible
 * warning instead of rendering broken shapes.
 */

import {
  HealthResponse,
  SUPPORTED_SCHEMA_VERSION,
  TaskDetailResponse,
  TaskListResponse,
  WorktreeDetailResponse,
  WorktreesListResponse,
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
    if (!/^[A-Za-z0-9_\-]{1,128}$/.test(opId)) {
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

  // --- Gap #3 Slice 4 — worktree topology consumer ---------------------

  public async worktreesList(): Promise<WorktreesListResponse> {
    return this.get<WorktreesListResponse>(
      '/observability/worktrees',
    );
  }

  public async worktreeDetail(
    graphId: string,
  ): Promise<WorktreeDetailResponse> {
    // Mirror of the agent-side _SESSION_ID_RE used for graph_id
    // validation on the server (`tests/governance/test_ide_observability_worktrees.py`
    // exercises this). Surface a fast 400 client-side instead of
    // round-tripping a malformed id.
    if (!/^[A-Za-z0-9_\-:.]{1,128}$/.test(graphId)) {
      throw new ObservabilityError(
        `malformed graph_id: ${graphId}`,
        400,
        'client.malformed_graph_id',
      );
    }
    return this.get<WorktreeDetailResponse>(
      `/observability/worktrees/${encodeURIComponent(graphId)}`,
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
      // Network failures bubble up as ObservabilityError with -1
      // status so callers can distinguish transport vs HTTP errors.
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
    } catch (exc) {
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
