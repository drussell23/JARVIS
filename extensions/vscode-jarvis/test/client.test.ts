/**
 * HTTP client tests — injects a stub fetch so no real server is needed.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ObservabilityClient,
  ObservabilityError,
  SchemaMismatchError,
} from '../src/api/client';

type StubFetch = (
  url: string,
  init?: RequestInit,
) => Promise<Response>;

function mkResponse(
  body: unknown,
  status: number = 200,
  contentType = 'application/json',
): Response {
  return new Response(
    typeof body === 'string' ? body : JSON.stringify(body),
    {
      status,
      headers: { 'Content-Type': contentType },
    },
  );
}

test('health() returns parsed body on 200', async () => {
  const fetchFn: StubFetch = async () =>
    mkResponse({
      schema_version: '1.0',
      enabled: true,
      api_version: '1.0',
      surface: 'tasks',
      now_mono: 123.4,
    });
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const h = await c.health();
  assert.equal(h.enabled, true);
  assert.equal(h.surface, 'tasks');
});

test('taskList URL normalizes trailing slash', async () => {
  let capturedUrl = '';
  const fetchFn: StubFetch = async (url) => {
    capturedUrl = url;
    return mkResponse({ schema_version: '1.0', op_ids: [], count: 0 });
  };
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234/',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await c.taskList();
  assert.equal(capturedUrl, 'http://127.0.0.1:1234/observability/tasks');
});

test('taskDetail rejects malformed op_id before making a request', async () => {
  let called = false;
  const fetchFn: StubFetch = async () => {
    called = true;
    return mkResponse({});
  };
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await assert.rejects(
    c.taskDetail('bad space!'),
    (exc: unknown) =>
      exc instanceof ObservabilityError && exc.status === 400,
  );
  assert.equal(called, false, 'should not hit the network');
});

test('HTTP 403 raises ObservabilityError with reason_code', async () => {
  const fetchFn: StubFetch = async () =>
    mkResponse(
      { schema_version: '1.0', error: true, reason_code: 'ide_observability.disabled' },
      403,
    );
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await assert.rejects(
    c.health(),
    (exc: unknown) =>
      exc instanceof ObservabilityError &&
      exc.status === 403 &&
      exc.reasonCode === 'ide_observability.disabled',
  );
});

test('schema mismatch raises SchemaMismatchError', async () => {
  const fetchFn: StubFetch = async () =>
    mkResponse({ schema_version: '9.9', op_ids: [], count: 0 });
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await assert.rejects(
    c.taskList(),
    (exc: unknown) =>
      exc instanceof SchemaMismatchError && exc.received === '9.9',
  );
});

test('network error maps to ObservabilityError status=-1', async () => {
  const fetchFn: StubFetch = async () => {
    throw new Error('ECONNREFUSED');
  };
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await assert.rejects(
    c.health(),
    (exc: unknown) =>
      exc instanceof ObservabilityError && exc.status === -1,
  );
});

test('invalid JSON body raises ObservabilityError', async () => {
  const fetchFn: StubFetch = async () =>
    new Response('not json at all', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await assert.rejects(
    c.health(),
    (exc: unknown) =>
      exc instanceof ObservabilityError &&
      exc.reasonCode === 'client.invalid_json',
  );
});
