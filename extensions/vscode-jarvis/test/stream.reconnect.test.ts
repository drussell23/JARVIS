/**
 * Reconnect + header tests — backoff math, Last-Event-ID, op_id filter.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { StreamConsumer, StreamState } from '../src/api/stream';

function mkStreamResponse(
  chunks: Uint8Array[] = [],
  opts: { never?: boolean; status?: number } = {},
): Response {
  if (opts.never === true) {
    return new Response(
      new ReadableStream<Uint8Array>({ start() { /* open forever */ } }),
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    );
  }
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(c);
      controller.close();
    },
  });
  return new Response(body, {
    status: opts.status ?? 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function frameBytes(id: string, event: string, op_id = 'op-x'): Uint8Array {
  const payload = {
    schema_version: '1.0',
    event_id: id,
    event_type: event,
    op_id,
    timestamp: 't',
    payload: {},
  };
  return new TextEncoder().encode(
    `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(payload)}\n\n`,
  );
}

async function waitUntil(
  cond: () => boolean, timeoutMs: number,
): Promise<void> {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error(`timeout waiting for condition`);
    }
    await new Promise((r) => setTimeout(r, 10));
  }
}

function extractHeaders(h: HeadersInit | undefined): Record<string, string> {
  if (h === undefined) return {};
  if (h instanceof Headers) {
    const out: Record<string, string> = {};
    h.forEach((v, k) => { out[k] = v; });
    return out;
  }
  if (Array.isArray(h)) return Object.fromEntries(h);
  return { ...(h as Record<string, string>) };
}

test('backoff uses jitter and respects max cap', async () => {
  let calls = 0;
  const sleepsObserved: number[] = [];
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: true,
    reconnectMaxBackoffMs: 1000,
    fetchFn: async () => {
      calls += 1;
      if (calls > 3) throw new Error('give_up');
      return new Response('x', { status: 503 });
    },
    jitterFn: () => 0.5,
    sleepFn: async (ms) => {
      sleepsObserved.push(ms);
      // Yield to the macrotask queue so the outer `waitUntil` can
      // observe progress. A pure-microtask sleepFn would starve
      // setTimeout callbacks and never exit the loop.
      await new Promise<void>((r) => setImmediate(r));
    },
  });
  consumer.start();
  await waitUntil(() => sleepsObserved.length >= 2, 2000);
  await consumer.stop();
  assert.ok(sleepsObserved.length >= 1);
  assert.ok(sleepsObserved[0]! <= 1000, `sleep[0]=${sleepsObserved[0]}`);
  // Second backoff should be ≥ first (monotone non-decreasing up to cap).
  assert.ok(sleepsObserved[1]! >= sleepsObserved[0]!);
});

test('Last-Event-ID header sent on reconnect', async () => {
  const urlsAndHeaders: Array<{ url: string; headers: Record<string, string> }> = [];
  let call = 0;
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: true,
    reconnectMaxBackoffMs: 50,
    fetchFn: (async (url: string, init?: RequestInit) => {
      call += 1;
      urlsAndHeaders.push({ url, headers: extractHeaders(init?.headers) });
      if (call === 1) {
        return mkStreamResponse([frameBytes('e42', 'task_created')]);
      }
      return mkStreamResponse([], { never: true });
    }) as unknown as typeof fetch,
    jitterFn: () => 0.01,
    sleepFn: async () => {
      await new Promise<void>((r) => setImmediate(r));
    },
  });
  consumer.start();
  await waitUntil(() => urlsAndHeaders.length >= 2, 2000);
  await consumer.stop();
  assert.equal(urlsAndHeaders[1]?.headers['Last-Event-ID'], 'e42');
});

test('op_id filter appended to URL as ?op_id=<value>', async () => {
  let capturedUrl = '';
  const fetchFn = async (url: string) => {
    capturedUrl = url;
    return mkStreamResponse([]);
  };
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    opIdFilter: 'op-abc',
    autoReconnect: false,
    reconnectMaxBackoffMs: 50,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const done = new Promise<void>((resolve) => {
    consumer.onState((s: StreamState) => {
      if (s === 'disconnected') resolve();
    });
  });
  consumer.start();
  await done;
  assert.match(capturedUrl, /\?op_id=op-abc$/);
});
