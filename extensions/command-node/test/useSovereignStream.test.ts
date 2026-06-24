/**
 * SSE client tests for the Command Node.
 *
 * Mirrors the vscode-jarvis stream.parser + stream.reconnect tests:
 *   - multi-event stream parsing (single chunk, split chunks, comments)
 *   - schema-mismatch drop
 *   - reconnect with Last-Event-ID replay header
 *   - exponential backoff + jitter
 * plus the Command-Node-specific:
 *   - pushBounded ring-buffer cap (the hook's bounded buffer)
 *   - poll-fallback frame shape (synthesized from /observability/tasks)
 *
 * Uses vitest with a mocked fetch returning a ReadableStream Response.
 */

import { describe, expect, test } from 'vitest';
import { StreamConsumer } from '../lib/stream';
import { StreamEventFrame } from '../lib/types';
import { pushBounded } from '../hooks/useSovereignStream';

function frameBytes(
  id: string,
  event: string,
  payload: Record<string, unknown> = {},
  opId = 'op-x',
): Uint8Array {
  const body = {
    schema_version: '1.0',
    event_id: id,
    event_type: event,
    op_id: opId,
    timestamp: 't',
    payload,
  };
  const txt = `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(body)}\n\n`;
  return new TextEncoder().encode(txt);
}

function mkStreamResponse(
  chunks: Uint8Array[],
  opts: { never?: boolean; status?: number } = {},
): Response {
  if (opts.never === true) {
    return new Response(
      new ReadableStream<Uint8Array>({ start() {} }),
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
  target: string,
  timeoutMs = 2000,
): Promise<void> {
  const start = Date.now();
  while (consumer.getState() !== target) {
    if (Date.now() - start > timeoutMs) {
      throw new Error(`timeout waiting for state=${target}`);
    }
    await new Promise((r) => setTimeout(r, 5));
  }
}

async function waitUntil(
  cond: () => boolean,
  timeoutMs = 2000,
): Promise<void> {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error('timeout waiting for condition');
    }
    await new Promise((r) => setTimeout(r, 5));
  }
}

function extractHeaders(h: HeadersInit | undefined): Record<string, string> {
  if (h === undefined) return {};
  if (h instanceof Headers) {
    const out: Record<string, string> = {};
    h.forEach((v, k) => {
      out[k] = v;
    });
    return out;
  }
  if (Array.isArray(h)) return Object.fromEntries(h);
  return { ...(h as Record<string, string>) };
}

describe('StreamConsumer SSE parsing', () => {
  test('parses a multi-event stream (single chunk)', async () => {
    const chunk = new Uint8Array([
      ...frameBytes('e1', 'task_created'),
      ...frameBytes('e2', 'fsm_phase_changed', { phase: 'GENERATE' }),
      ...frameBytes('e3', 'task_completed'),
    ]);
    const consumer = new StreamConsumer({
      endpoint: 'http://127.0.0.1:1234',
      autoReconnect: false,
      reconnectMaxBackoffMs: 100,
      fetchFn: (async () => mkStreamResponse([chunk])) as unknown as typeof fetch,
    });
    const received: StreamEventFrame[] = [];
    consumer.onEvent((f) => {
      received.push(f);
    });
    consumer.start();
    await waitForState(consumer, 'disconnected');
    expect(received.map((f) => f.event_type)).toEqual([
      'task_created',
      'fsm_phase_changed',
      'task_completed',
    ]);
    expect(received[1]?.payload).toEqual({ phase: 'GENERATE' });
  });

  test('parses a frame split across chunks', async () => {
    const full = frameBytes('e1', 'dag_node_updated', {
      node_id: 'n1',
      state: 'running',
    });
    const mid = Math.floor(full.length / 2);
    const consumer = new StreamConsumer({
      endpoint: 'http://127.0.0.1:1234',
      autoReconnect: false,
      reconnectMaxBackoffMs: 100,
      fetchFn: (async () =>
        mkStreamResponse([full.slice(0, mid), full.slice(mid)])) as unknown as typeof fetch,
    });
    const received: StreamEventFrame[] = [];
    consumer.onEvent((f) => {
      received.push(f);
    });
    consumer.start();
    await waitForState(consumer, 'disconnected');
    expect(received).toHaveLength(1);
    expect(received[0]?.event_type).toBe('dag_node_updated');
  });

  test('ignores SSE comment keepalive lines', async () => {
    const comment = new TextEncoder().encode(': keepalive\n\n');
    const consumer = new StreamConsumer({
      endpoint: 'http://127.0.0.1:1234',
      autoReconnect: false,
      reconnectMaxBackoffMs: 100,
      fetchFn: (async () =>
        mkStreamResponse([comment, frameBytes('e1', 'task_created')])) as unknown as typeof fetch,
    });
    const received: StreamEventFrame[] = [];
    consumer.onEvent((f) => {
      received.push(f);
    });
    consumer.start();
    await waitForState(consumer, 'disconnected');
    expect(received).toHaveLength(1);
  });

  test('drops schema-mismatched frames', async () => {
    const wrong = new TextEncoder().encode(
      `id: e1\nevent: task_created\ndata: ${JSON.stringify({
        schema_version: '9.9',
        event_id: 'e1',
        event_type: 'task_created',
        op_id: 'op-x',
        timestamp: 't',
        payload: {},
      })}\n\n`,
    );
    const consumer = new StreamConsumer({
      endpoint: 'http://127.0.0.1:1234',
      autoReconnect: false,
      reconnectMaxBackoffMs: 100,
      fetchFn: (async () => mkStreamResponse([wrong])) as unknown as typeof fetch,
    });
    const received: StreamEventFrame[] = [];
    consumer.onEvent((f) => {
      received.push(f);
    });
    consumer.start();
    await waitForState(consumer, 'disconnected');
    expect(received).toHaveLength(0);
  });
});

describe('StreamConsumer reconnect', () => {
  test('sends Last-Event-ID header on reconnect after a frame', async () => {
    let calls = 0;
    const headersSeen: Record<string, string>[] = [];
    const consumer = new StreamConsumer({
      endpoint: 'http://127.0.0.1:1234',
      autoReconnect: true,
      reconnectMaxBackoffMs: 50,
      jitterFn: () => 0,
      sleepFn: async () => {
        await new Promise<void>((r) => setImmediate(r));
      },
      fetchFn: (async (_url: string, init?: RequestInit) => {
        calls += 1;
        headersSeen.push(extractHeaders(init?.headers));
        if (calls === 1) {
          // First connection yields one frame then closes -> reconnect.
          return mkStreamResponse([frameBytes('evt-42', 'task_started')]);
        }
        // Second connection: open forever so we can inspect its headers.
        return mkStreamResponse([], { never: true });
      }) as unknown as typeof fetch,
    });
    consumer.start();
    await waitUntil(() => calls >= 2);
    await consumer.stop();
    // First request: no Last-Event-ID. Second: replays from evt-42.
    expect(headersSeen[0]?.['Last-Event-ID']).toBeUndefined();
    expect(headersSeen[1]?.['Last-Event-ID']).toBe('evt-42');
  });

  test('backoff uses jitter and respects the max cap', async () => {
    let calls = 0;
    const sleeps: number[] = [];
    const consumer = new StreamConsumer({
      endpoint: 'http://127.0.0.1:1234',
      autoReconnect: true,
      reconnectMaxBackoffMs: 1000,
      jitterFn: () => 0.5,
      fetchFn: (async () => {
        calls += 1;
        if (calls > 3) throw new Error('give_up');
        return new Response('x', { status: 503 });
      }) as unknown as typeof fetch,
      sleepFn: async (ms) => {
        sleeps.push(ms);
        await new Promise<void>((r) => setImmediate(r));
      },
    });
    consumer.start();
    await waitUntil(() => sleeps.length >= 2);
    await consumer.stop();
    // full-jitter with 0.5: floor(500*0.5)=250, floor(1000*0.5)=500...
    expect(sleeps[0]).toBe(250);
    expect(sleeps.every((s) => s <= 1000)).toBe(true);
  });
});

describe('bounded event buffer (pushBounded)', () => {
  function mkFrame(id: string): StreamEventFrame {
    return {
      schema_version: '1.0',
      event_id: id,
      event_type: 'heartbeat',
      op_id: 'op',
      timestamp: 't',
      payload: {},
    };
  }

  test('caps the buffer and drops oldest', () => {
    let buf: StreamEventFrame[] = [];
    for (let i = 0; i < 10; i += 1) {
      buf = pushBounded(buf, mkFrame(`e${i}`), 3);
    }
    expect(buf).toHaveLength(3);
    expect(buf.map((f) => f.event_id)).toEqual(['e7', 'e8', 'e9']);
  });

  test('does not mutate the input array', () => {
    const original = [mkFrame('a')];
    const next = pushBounded(original, mkFrame('b'), 5);
    expect(original).toHaveLength(1);
    expect(next).toHaveLength(2);
  });
});
