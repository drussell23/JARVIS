/**
 * HTTP client for the JARVIS Gap #2 Slice 4 Confidence-policy
 * write authority surface.
 *
 * Sibling to ``ObservabilityClient`` — separate schema discipline
 * (``ide_policy_router.1`` vs the read surface's ``"1.0"``),
 * separate POST methods, separate error class. Sharing the same
 * fetch substrate but disciplinarily isolated so the read surface
 * stays AST-pinned read-only.
 */

import {
  ConfidenceSnapshot,
  DecisionBody,
  DecisionResponse,
  isSupportedPolicySchema,
  POLICY_ROUTER_SCHEMA_VERSION,
  ProposeBody,
  ProposeResponse,
} from './policyTypes';

export class PolicyClientError extends Error {
  public readonly status: number;
  public readonly reasonCode: string;
  public readonly detail: string;
  public constructor(
    message: string,
    status: number,
    reasonCode = '',
    detail = '',
  ) {
    super(message);
    this.name = 'PolicyClientError';
    this.status = status;
    this.reasonCode = reasonCode;
    this.detail = detail;
  }
}

export class PolicySchemaMismatchError extends Error {
  public readonly expected: string;
  public readonly received: string;
  public constructor(received: string) {
    super(
      `policy schema_version mismatch: expected ` +
        `${POLICY_ROUTER_SCHEMA_VERSION}, got ${received}`,
    );
    this.name = 'PolicySchemaMismatchError';
    this.expected = POLICY_ROUTER_SCHEMA_VERSION;
    this.received = received;
  }
}

export interface PolicyClientOptions {
  /** Base URL of the EventChannelServer, e.g. http://127.0.0.1:8765 */
  readonly endpoint: string;
  /** Optional fetch override (tests inject a stub). */
  readonly fetchFn?: typeof fetch;
  /** Abort signal for request cancellation. */
  readonly signal?: AbortSignal;
}

const PROPOSAL_ID_RE = /^[A-Za-z0-9_\-:.]{1,128}$/;

export class PolicyClient {
  private readonly endpoint: string;
  private readonly fetchFn: typeof fetch;
  private readonly signal?: AbortSignal;

  public constructor(opts: PolicyClientOptions) {
    this.endpoint = trimTrailingSlash(opts.endpoint);
    this.fetchFn = opts.fetchFn ?? fetch;
    this.signal = opts.signal;
  }

  public url(path: string): string {
    return `${this.endpoint}${path.startsWith('/') ? path : `/${path}`}`;
  }

  public async snapshot(): Promise<ConfidenceSnapshot> {
    return this.request<ConfidenceSnapshot>(
      'GET', '/policy/confidence',
    );
  }

  public async propose(body: ProposeBody): Promise<ProposeResponse> {
    return this.request<ProposeResponse>(
      'POST', '/policy/confidence/proposals', body,
    );
  }

  public async approve(
    proposalId: string, body: DecisionBody,
  ): Promise<DecisionResponse> {
    if (!PROPOSAL_ID_RE.test(proposalId)) {
      throw new PolicyClientError(
        `malformed proposal_id: ${proposalId}`,
        400, 'client.malformed_proposal_id',
      );
    }
    return this.request<DecisionResponse>(
      'POST',
      `/policy/confidence/proposals/${encodeURIComponent(proposalId)}/approve`,
      body,
    );
  }

  public async reject(
    proposalId: string, body: DecisionBody,
  ): Promise<DecisionResponse> {
    if (!PROPOSAL_ID_RE.test(proposalId)) {
      throw new PolicyClientError(
        `malformed proposal_id: ${proposalId}`,
        400, 'client.malformed_proposal_id',
      );
    }
    return this.request<DecisionResponse>(
      'POST',
      `/policy/confidence/proposals/${encodeURIComponent(proposalId)}/reject`,
      body,
    );
  }

  private async request<T extends { schema_version?: string }>(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
  ): Promise<T> {
    const url = this.url(path);
    const init: RequestInit = {
      method,
      headers: { Accept: 'application/json' },
      signal: this.signal,
      cache: 'no-store',
    };
    if (method === 'POST') {
      (init.headers as Record<string, string>)['Content-Type'] =
        'application/json';
      init.body = JSON.stringify(body ?? {});
    }

    let response: Response;
    try {
      response = await this.fetchFn(url, init);
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      throw new PolicyClientError(
        `fetch failed: ${msg}`, -1, 'client.network_error',
      );
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new PolicyClientError(
        `JSON parse failed for ${path}`,
        response.status,
        'client.invalid_json',
      );
    }

    if (!response.ok) {
      const errBody = payload as {
        reason_code?: string; detail?: string;
      };
      throw new PolicyClientError(
        `${path} returned ${response.status}`,
        response.status,
        errBody?.reason_code ?? '',
        errBody?.detail ?? '',
      );
    }

    if (!isSupportedPolicySchema(payload as { schema_version?: string })) {
      throw new PolicySchemaMismatchError(
        (payload as { schema_version?: string })?.schema_version
          ?? '(missing)',
      );
    }

    return payload as T;
  }
}

function trimTrailingSlash(url: string): string {
  return url.endsWith('/') ? url.slice(0, -1) : url;
}
