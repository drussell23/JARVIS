/**
 * PolicyClient regression suite — stub-fetch isolation.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PolicyClient,
  PolicyClientError,
  PolicySchemaMismatchError,
} from '../src/api/policyClient';
import {
  POLICY_ROUTER_SCHEMA_VERSION,
  classifyClientSide,
  isPolicyEventType,
  isSupportedPolicySchema,
} from '../src/api/policyTypes';

type StubFetch = (
  url: string,
  init?: RequestInit,
) => Promise<Response>;

function mkResponse(
  body: unknown, status = 200,
): Response {
  return new Response(
    typeof body === 'string' ? body : JSON.stringify(body),
    {
      status,
      headers: { 'Content-Type': 'application/json' },
    },
  );
}

const baseSnapshot = {
  schema_version: POLICY_ROUTER_SCHEMA_VERSION,
  current_effective: {
    floor: 0.05, window_k: 16,
    approaching_factor: 1.5, enforce: false,
  },
  adapted: {
    loader_enabled: true, in_effect: false,
    values: {}, proposal_id: '', approved_at: '', approved_by: '',
  },
  proposals: { pending: 0, approved: 0, rejected: 0, items: [] },
  policy_substrate_enabled: true,
};

test('snapshot() returns parsed body on 200', async () => {
  const fetchFn: StubFetch = async () => mkResponse(baseSnapshot);
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const s = await c.snapshot();
  assert.equal(s.current_effective.floor, 0.05);
  assert.equal(s.proposals.pending, 0);
});

test('snapshot() throws SchemaMismatchError on wrong schema', async () => {
  const fetchFn: StubFetch = async () =>
    mkResponse({ schema_version: '1.0', stuff: 'x' });
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await assert.rejects(
    () => c.snapshot(),
    PolicySchemaMismatchError,
  );
});

test('propose() POSTs JSON body and returns 201 envelope', async () => {
  let capturedBody = '';
  const fetchFn: StubFetch = async (_url, init) => {
    capturedBody = init?.body as string;
    return mkResponse({
      schema_version: POLICY_ROUTER_SCHEMA_VERSION,
      ok: true,
      proposal_id: 'conf-x',
      kind: 'raise_floor',
      moved_dimensions: ['raise_floor'],
      current_state_hash: 'sha256:a',
      proposed_state_hash: 'sha256:b',
      monotonic_tightening_verdict: 'passed',
    }, 201);
  };
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const res = await c.propose({
    current: { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false },
    proposed: { floor: 0.10, window_k: 16, approaching_factor: 1.5, enforce: false },
    evidence_summary: 'floor 0.05 → 0.10',
    observation_count: 5,
    operator: 'alice',
  });
  assert.equal(res.proposal_id, 'conf-x');
  assert.equal(res.kind, 'raise_floor');
  const sent = JSON.parse(capturedBody);
  assert.equal(sent.operator, 'alice');
  assert.equal(sent.proposed.floor, 0.10);
});

test('propose() surfaces 400 reason_code as PolicyClientError', async () => {
  const fetchFn: StubFetch = async () => mkResponse({
    schema_version: POLICY_ROUTER_SCHEMA_VERSION,
    error: true,
    reason_code: 'ide_policy_router.policy_would_loosen',
    detail: 'floor 0.10 → 0.05 loosen',
  }, 400);
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  try {
    await c.propose({
      current: { floor: 0.10, window_k: 16, approaching_factor: 1.5, enforce: false },
      proposed: { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false },
      evidence_summary: 'loosen attempt',
      observation_count: 1,
      operator: 'mallory',
    });
    assert.fail('expected propose() to throw');
  } catch (exc) {
    assert.ok(exc instanceof PolicyClientError);
    assert.equal((exc as PolicyClientError).status, 400);
    assert.equal(
      (exc as PolicyClientError).reasonCode,
      'ide_policy_router.policy_would_loosen',
    );
  }
});

test('approve() URL-encodes proposal_id', async () => {
  let capturedUrl = '';
  const fetchFn: StubFetch = async (url) => {
    capturedUrl = url;
    return mkResponse({
      schema_version: POLICY_ROUTER_SCHEMA_VERSION,
      ok: true,
      proposal_id: 'conf-1.2:test',
      operator_decision: 'approved',
      operator: 'alice',
      applied: null,
    });
  };
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await c.approve('conf-1.2:test', { operator: 'alice' });
  assert.match(capturedUrl, /conf-1\.2%3Atest\/approve$/);
});

test('approve() rejects malformed proposal_id', async () => {
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: (async () => mkResponse({})) as unknown as typeof fetch,
  });
  await assert.rejects(
    () => c.approve('has spaces!', { operator: 'a' }),
    (err: unknown) =>
      err instanceof PolicyClientError &&
      err.reasonCode === 'client.malformed_proposal_id',
  );
});

test('reject() reaches the right route', async () => {
  let capturedUrl = '';
  const fetchFn: StubFetch = async (url) => {
    capturedUrl = url;
    return mkResponse({
      schema_version: POLICY_ROUTER_SCHEMA_VERSION,
      ok: true,
      proposal_id: 'conf-1',
      operator_decision: 'rejected',
      operator: 'alice',
      applied: null,
    });
  };
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await c.reject('conf-1', { operator: 'alice' });
  assert.ok(capturedUrl.endsWith('/conf-1/reject'));
});

test('network failure surfaces as PolicyClientError(-1)', async () => {
  const fetchFn: StubFetch = async () => {
    throw new Error('ECONNREFUSED');
  };
  const c = new PolicyClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  try {
    await c.snapshot();
    assert.fail('expected throw');
  } catch (exc) {
    assert.ok(exc instanceof PolicyClientError);
    assert.equal((exc as PolicyClientError).status, -1);
    assert.equal(
      (exc as PolicyClientError).reasonCode,
      'client.network_error',
    );
  }
});

// --- types/helpers --------------------------------------------------------

test('isSupportedPolicySchema accepts only the canonical version', () => {
  assert.equal(
    isSupportedPolicySchema({ schema_version: POLICY_ROUTER_SCHEMA_VERSION }),
    true,
  );
  assert.equal(
    isSupportedPolicySchema({ schema_version: '1.0' }),
    false,
  );
  assert.equal(isSupportedPolicySchema(null), false);
  assert.equal(isSupportedPolicySchema(undefined), false);
});

test('isPolicyEventType accepts the four canonical events', () => {
  assert.equal(isPolicyEventType('confidence_policy_proposed'), true);
  assert.equal(isPolicyEventType('confidence_policy_approved'), true);
  assert.equal(isPolicyEventType('confidence_policy_rejected'), true);
  assert.equal(isPolicyEventType('confidence_policy_applied'), true);
  assert.equal(isPolicyEventType('confidence_policy_unknown'), false);
  assert.equal(isPolicyEventType('task_created'), false);
});

// --- classifyClientSide --------------------------------------------------

test('classifyClientSide: floor raise classifies as tighten', () => {
  const r = classifyClientSide(
    { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false },
    { floor: 0.10, window_k: 16, approaching_factor: 1.5, enforce: false },
  );
  assert.equal(r.is_tighten, true);
  assert.equal(r.is_no_op, false);
  assert.deepEqual(r.moved, ['raise_floor']);
});

test('classifyClientSide: floor lower classifies as loosen', () => {
  const r = classifyClientSide(
    { floor: 0.10, window_k: 16, approaching_factor: 1.5, enforce: false },
    { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false },
  );
  assert.equal(r.is_tighten, false);
  assert.equal(r.is_no_op, false);
  assert.match(r.reason, /loosen/);
});

test('classifyClientSide: identical inputs are no-op', () => {
  const policy = { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false };
  const r = classifyClientSide(policy, { ...policy });
  assert.equal(r.is_no_op, true);
  assert.equal(r.is_tighten, false);
});

test('classifyClientSide: window_k shrink is tighten, grow is loosen', () => {
  const base = { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false };
  assert.equal(classifyClientSide(base, { ...base, window_k: 8 }).is_tighten, true);
  assert.equal(classifyClientSide(base, { ...base, window_k: 32 }).is_tighten, false);
});

test('classifyClientSide: approaching_factor widen is tighten', () => {
  const base = { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false };
  assert.equal(
    classifyClientSide(base, { ...base, approaching_factor: 2.0 }).is_tighten,
    true,
  );
  assert.equal(
    classifyClientSide(base, { ...base, approaching_factor: 1.1 }).is_tighten,
    false,
  );
});

test('classifyClientSide: enforce false→true is tighten', () => {
  const base = { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false };
  assert.equal(
    classifyClientSide(base, { ...base, enforce: true }).is_tighten,
    true,
  );
  assert.equal(
    classifyClientSide(
      { ...base, enforce: true }, { ...base, enforce: false },
    ).is_tighten,
    false,
  );
});

test('classifyClientSide: conjunctive — any loosen blocks even if others tighten', () => {
  const r = classifyClientSide(
    { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false },
    { floor: 0.10, window_k: 32, approaching_factor: 1.5, enforce: false },
  );
  assert.equal(r.is_tighten, false);
  assert.match(r.reason, /loosen/);
});

test('classifyClientSide: multi-dim tighten lists every moved kind', () => {
  const r = classifyClientSide(
    { floor: 0.05, window_k: 16, approaching_factor: 1.5, enforce: false },
    { floor: 0.10, window_k: 8, approaching_factor: 2.0, enforce: true },
  );
  assert.equal(r.is_tighten, true);
  assert.deepEqual(
    [...r.moved].sort(),
    ['enable_enforce', 'raise_floor', 'shrink_window', 'widen_approaching'],
  );
});
