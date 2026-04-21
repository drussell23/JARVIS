/**
 * SSE parser tests — wire-format and payload parsing only.
 * See stream.lifecycle.test.ts + stream.reconnect.test.ts for
 * connection-management and reconnect tests.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { StreamConsumer } from '../src/api/stream';
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

async function waitForState(
  consumer: StreamConsumer,
  target: 'connecting' | 'connected' | 'disconnected' | 'closed' | 'error' | 'reconnecting',
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
  await waitForState(consumer, 'disconnected', 2000);
  assert.equal(received.length, 1);
  assert.equal(received[0]?.event_type, 'task_created');
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
});

test('parses a frame split across chunks', async () => {
  const full = frameBytes('e1', 'task_created', {});
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
});

test('ignores SSE comment lines', async () => {
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

test('silently drops schema-mismatched frames', async () => {
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
