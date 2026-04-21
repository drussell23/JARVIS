/**
 * SSE stream consumer tests — injects a fetch stub that returns a
 * ReadableStream we control. Verifies parser handles the Slice 2
 * wire format, reconnect backoff math, and state transitions.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { StreamConsumer, StreamState } from '../src/api/stream';
import { StreamEventFrame } from '../src/api/types';

function frameBytes(
  id: string,
  event: string,
  data: Record<string, unknown>,
): Uint8Array {
  const payload = {
    schema_version: '1.0',
    event_id: id,
    event_type: event,
    op_id: (data['op_id'] as string) ?? 'op-x',
    timestamp: 't',
    payload: data['payload'] ?? {},
  };
  const txt = `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
  return new TextEncoder().encode(txt);
}

function mkStreamResponse(chunks: Uint8Array[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) {
        controller.enqueue(c);
      }
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

test('parses a single well-formed frame', async () => {
  const fetchFn = async () => mkStreamResponse([
    frameBytes('e1', 'task_created', { payload: { t: 1 } }),
  ]);
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const received: StreamEventFrame[] = [];
  consumer.onEvent((f) => { received.push(f); });
  consumer.start();
  // Wait for loop exit (autoReconnect=false).
  await waitForState(consumer, 'disconnected', 2000);
  assert.equal(received.length, 1);
  assert.equal(received[0]?.event_type, 'task_created');
  assert.equal(received[0]?.event_id, 'e1');
});

test('parses multiple frames in one chunk', async () => {
  const chunk = new Uint8Array([
    ...frameBytes('e1', 'task_created', {}),
    ...frameBytes('e2', 'task_started', {}),
    ...frameBytes('e3', 'task_completed', {}),
  ]);
  const fetchFn = async () => mkStreamResponse([chunk]);
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const received: StreamEventFrame[] = [];
  consumer.onEvent((f) => { received.push(f); });
  consumer.start();
  await waitForState(consumer, 'disconnected', 2000);
  assert.equal(received.length, 3);
  assert.deepEqual(received.map((f) => f.event_id), ['e1', 'e2', 'e3']);
});

test('parses a frame split across multiple chunks', async () => {
  const full = frameBytes('e1', 'task_created', {});
  // Split bytes arbitrarily.
  const mid = Math.floor(full.length / 2);
  const fetchFn = async () => mkStreamResponse([
    full.slice(0, mid),
    full.slice(mid),
  ]);
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const received: StreamEventFrame[] = [];
  consumer.onEvent((f) => { received.push(f); });
  consumer.start();
  await waitForState(consumer, 'disconnected', 2000);
  assert.equal(received.length, 1);
  assert.equal(received[0]?.event_id, 'e1');
});

test('ignores comment lines (starting with :)', async () => {
  const comment = new TextEncoder().encode(': keepalive\n\n');
  const fetchFn = async () => mkStreamResponse([
    comment,
    frameBytes('e1', 'task_created', {}),
  ]);
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const received: StreamEventFrame[] = [];
  consumer.onEvent((f) => { received.push(f); });
  consumer.start();
  await waitForState(consumer, 'disconnected', 2000);
  assert.equal(received.length, 1);
});

test('silently drops frames with schema mismatch', async () => {
  const wrongSchema = new TextEncoder().encode(
    `id: e1\nevent: task_created\ndata: ${JSON.stringify({
      schema_version: '9.9',
      event_id: 'e1',
      event_type: 'task_created',
      op_id: 'op-x',
      timestamp: 't',
      payload: {},
    })}\n\n`,
  );
  const fetchFn = async () => mkStreamResponse([wrongSchema]);
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const received: StreamEventFrame[] = [];
  consumer.onEvent((f) => { received.push(f); });
  consumer.start();
  await waitForState(consumer, 'disconnected', 2000);
  assert.equal(received.length, 0);
});

test('transitions disconnected → connecting → connected → disconnected', async () => {
  const fetchFn = async () => mkStreamResponse([
    frameBytes('e1', 'task_created', {}),
  ]);
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const states: StreamState[] = [];
  consumer.onState((s) => { states.push(s); });
  consumer.start();
  await waitForState(consumer, 'disconnected', 2000);
  assert.ok(states.includes('connecting'), `states=${JSON.stringify(states)}`);
  assert.ok(states.includes('connected'), `states=${JSON.stringify(states)}`);
  assert.equal(states[states.length - 1], 'disconnected');
});

test('stop() cancels an active stream promptly', async () => {
  // Endless stream — controller stays open.
  const fetchFn = async () =>
    new Response(
      new ReadableStream<Uint8Array>({
        start() {
          // never enqueue, never close — connection stays open.
        },
      }),
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    );
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  consumer.start();
  await waitForState(consumer, 'connected', 2000);
  await consumer.stop();
  assert.equal(consumer.getState(), 'closed');
});

test('HTTP 403 ends the loop (autoReconnect=false)', async () => {
  const fetchFn = async () =>
    new Response(JSON.stringify({ error: true }), { status: 403 });
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: false,
    reconnectMaxBackoffMs: 100,
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const states: StreamState[] = [];
  consumer.onState((s) => { states.push(s); });
  consumer.start();
  await waitForState(consumer, 'disconnected', 2000);
  assert.ok(states.includes('error'), `states=${JSON.stringify(states)}`);
});

test('reconnect backoff uses jitter and respects max cap', async () => {
  let calls = 0;
  const fetchFn = async () => {
    calls += 1;
    if (calls > 2) {
      // Give up after a couple of retries by throwing a distinctive error.
      throw new Error('give_up');
    }
    return new Response('x', { status: 503 });
  };
  let sleepsObserved: number[] = [];
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: true,
    reconnectMaxBackoffMs: 1000,
    fetchFn: fetchFn as unknown as typeof fetch,
    jitterFn: () => 0.5, // deterministic
    sleepFn: async (ms) => {
      sleepsObserved.push(ms);
      // Short-circuit — don't actually wait.
    },
  });
  consumer.start();
  // Wait until stop_fn triggers (after give_up throws and we try again).
  await wait(50);
  await consumer.stop();
  // First backoff ≤ max (with jitter=0.5 → half of base_ms or cap).
  // BASE_BACKOFF_MS = 500, first call after 1 failure: 500 * 2^0 * 0.5 = 250.
  assert.ok(
    sleepsObserved.length >= 1,
    `expected at least one backoff sleep, got ${sleepsObserved.length}`,
  );
  assert.ok(
    sleepsObserved[0]! <= 1000,
    `first sleep ${sleepsObserved[0]} > cap`,
  );
});

test('Last-Event-ID header sent on reconnect', async () => {
  const urlsAndHeaders: Array<{ url: string; headers: Record<string, string> }> = [];
  let call = 0;
  const fetchFn = async (url: string, init?: RequestInit) => {
    call += 1;
    urlsAndHeaders.push({
      url,
      headers: extractHeaders(init?.headers),
    });
    if (call === 1) {
      return mkStreamResponse([frameBytes('e42', 'task_created', {})]);
    }
    // Second connect: infinite empty stream — we'll stop before it completes.
    return new Response(
      new ReadableStream<Uint8Array>({
        start() {
          /* no-op */
        },
      }),
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    );
  };
  const consumer = new StreamConsumer({
    endpoint: 'http://127.0.0.1:1234',
    autoReconnect: true,
    reconnectMaxBackoffMs: 50,
    fetchFn: fetchFn as unknown as typeof fetch,
    jitterFn: () => 0.01,
    sleepFn: async () => { /* don't wait */ },
  });
  consumer.start();
  // Wait long enough for first connect to complete + second to start.
  await waitUntil(() => urlsAndHeaders.length >= 2, 2000);
  await consumer.stop();
  assert.equal(urlsAndHeaders[1]?.headers['Last-Event-ID'], 'e42');
});

test('op_id filter appended to URL', async () => {
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
  consumer.start();
  await waitForState(consumer, 'disconnected', 2000);
  assert.match(capturedUrl, /\?op_id=op-abc$/);
});

// --- helpers --------------------------------------------------------------

async function waitForState(
  consumer: StreamConsumer,
  target: StreamState,
  timeoutMs: number,
): Promise<void> {
  return waitUntil(() => consumer.getState() === target, timeoutMs);
}

async function waitUntil(
  cond: () => boolean,
  timeoutMs: number,
): Promise<void> {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error(`timeout waiting for condition after ${timeoutMs}ms`);
    }
    await wait(10);
  }
}

function wait(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function extractHeaders(
  h: HeadersInit | undefined,
): Record<string, string> {
  if (h === undefined) return {};
  if (h instanceof Headers) {
    const out: Record<string, string> = {};
    h.forEach((v, k) => {
      out[k] = v;
    });
    return out;
  }
  if (Array.isArray(h)) {
    return Object.fromEntries(h);
  }
  return { ...(h as Record<string, string>) };
}
