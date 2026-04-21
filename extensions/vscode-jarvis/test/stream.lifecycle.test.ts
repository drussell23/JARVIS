/**
 * Stream lifecycle tests — state transitions, stop(), HTTP errors.
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

async function waitForState(
  consumer: StreamConsumer,
  target: StreamState,
  timeoutMs: number,
): Promise<void> {
  const start = Date.now();
  while (consumer.getState() !== target) {
    if (Date.now() - start > timeoutMs) {
      throw new Error(`timeout waiting for state=${target}`);
    }
    await new Promise((r) => setTimeout(r, 10));
  }
}

test('transitions: disconnected → connecting → connected → disconnected', async () => {
  const chunk = new TextEncoder().encode(
    `id: e1\nevent: task_created\ndata: ${JSON.stringify({
      schema_version: '1.0', event_id: 'e1', event_type: 'task_created',
      op_id: 'op-x', timestamp: 't', payload: {},
    })}\n\n`,
  );
  const fetchFn = async () => mkStreamResponse([chunk]);
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
  assert.ok(states.includes('connecting'));
  assert.ok(states.includes('connected'));
  assert.equal(states[states.length - 1], 'disconnected');
});

test('stop() cancels an active stream promptly', async () => {
  const fetchFn = async () => mkStreamResponse([], { never: true });
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
  assert.ok(states.includes('error'));
});
