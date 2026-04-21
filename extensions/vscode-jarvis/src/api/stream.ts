/**
 * SSE parser + reconnect loop for the Slice 2 /observability/stream
 * endpoint.
 *
 * Uses the Node 18+ `fetch` API with the streaming body reader
 * (ReadableStream). No external EventSource polyfill — the parser
 * is a ~30-line state machine tailored to the Slice 2 wire format:
 *
 *     id: <event_id>
 *     event: <event_type>
 *     data: <json>
 *     <blank line>
 *
 * Features:
 *   * AbortController-driven cancellation
 *   * Exponential backoff with jitter on reconnect (bounded)
 *   * Last-Event-ID header on reconnect — replay requested from the
 *     server if the ring-buffer still has it
 *   * Discriminated-union event types via isTaskEvent / isControlEvent
 */

import { StreamEventFrame, isSupportedSchema } from './types';

export interface StreamOptions {
  readonly endpoint: string;
  readonly opIdFilter?: string | undefined;
  readonly autoReconnect: boolean;
  readonly reconnectMaxBackoffMs: number;
  readonly fetchFn?: typeof fetch;
  readonly logger?: (msg: string) => void;
  /** Jitter [0..1) — inject in tests for determinism. */
  readonly jitterFn?: () => number;
  /** Sleep override for tests. Takes ms, returns promise. */
  readonly sleepFn?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export type StreamListener = (frame: StreamEventFrame) => void | Promise<void>;
export type StreamStateListener = (state: StreamState) => void;

export type StreamState =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'closed'
  | 'error';

const BASE_BACKOFF_MS = 500;

export class StreamConsumer {
  private readonly opts: StreamOptions;
  private readonly listeners: Set<StreamListener> = new Set();
  private readonly stateListeners: Set<StreamStateListener> = new Set();
  private controller: AbortController | null = null;
  private runningLoop: Promise<void> | null = null;
  private lastEventId: string | null = null;
  private state: StreamState = 'disconnected';
  private consecutiveFailures = 0;
  /** Public for test-only introspection — do NOT rely on this. */
  public _testInternal_lastBackoff = 0;

  public constructor(opts: StreamOptions) {
    this.opts = opts;
  }

  public onEvent(listener: StreamListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  public onState(listener: StreamStateListener): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  public getState(): StreamState {
    return this.state;
  }

  public isRunning(): boolean {
    return this.runningLoop !== null;
  }

  public start(): void {
    if (this.runningLoop !== null) {
      return;
    }
    this.controller = new AbortController();
    this.runningLoop = this.runLoop(this.controller.signal).finally(() => {
      this.runningLoop = null;
      this.transition(
        this.state === 'closed' ? 'closed' : 'disconnected',
      );
    });
  }

  public async stop(): Promise<void> {
    this.transition('closed');
    if (this.controller !== null) {
      this.controller.abort();
      this.controller = null;
    }
    if (this.runningLoop !== null) {
      try {
        await this.runningLoop;
      } catch {
        // swallowed — loop-exits are expected.
      }
    }
  }

  private transition(next: StreamState): void {
    if (this.state === next) {
      return;
    }
    this.state = next;
    for (const l of this.stateListeners) {
      try {
        l(next);
      } catch (exc) {
        this.log(
          `[stream] state-listener threw: ${
            exc instanceof Error ? exc.message : String(exc)
          }`,
        );
      }
    }
  }

  private log(msg: string): void {
    if (this.opts.logger !== undefined) {
      this.opts.logger(msg);
    }
  }

  private async runLoop(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      try {
        this.transition(
          this.consecutiveFailures === 0 ? 'connecting' : 'reconnecting',
        );
        await this.connectAndStream(signal);
        this.consecutiveFailures = 0;
      } catch (exc) {
        if (signal.aborted) {
          return;
        }
        this.consecutiveFailures += 1;
        const msg = exc instanceof Error ? exc.message : String(exc);
        this.log(
          `[stream] connection dropped: ${msg} (failures=${this.consecutiveFailures})`,
        );
        this.transition('error');
        if (!this.opts.autoReconnect) {
          return;
        }
      }
      if (!this.opts.autoReconnect || signal.aborted) {
        return;
      }
      const backoff = this.computeBackoff();
      this._testInternal_lastBackoff = backoff;
      await (this.opts.sleepFn ?? defaultSleep)(backoff, signal);
    }
  }

  private computeBackoff(): number {
    const raw = BASE_BACKOFF_MS * 2 ** (this.consecutiveFailures - 1);
    const capped = Math.min(raw, this.opts.reconnectMaxBackoffMs);
    const jitter = (this.opts.jitterFn ?? Math.random)();
    // full-jitter: [0, capped)
    return Math.floor(capped * jitter);
  }

  private async connectAndStream(signal: AbortSignal): Promise<void> {
    const url = `${trimTrailingSlash(this.opts.endpoint)}/observability/stream${
      this.opts.opIdFilter !== undefined && this.opts.opIdFilter !== ''
        ? `?op_id=${encodeURIComponent(this.opts.opIdFilter)}`
        : ''
    }`;
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-store',
    };
    if (this.lastEventId !== null) {
      headers['Last-Event-ID'] = this.lastEventId;
    }
    const fetchFn = this.opts.fetchFn ?? fetch;
    const response = await fetchFn(url, {
      method: 'GET',
      headers,
      signal,
    });
    if (!response.ok) {
      throw new Error(`stream returned ${response.status}`);
    }
    if (response.body === null) {
      throw new Error('stream response has no body');
    }
    this.transition('connected');
    await this.consumeBody(response.body, signal);
  }

  private async consumeBody(
    body: ReadableStream<Uint8Array>,
    signal: AbortSignal,
  ): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    // Wire abort → reader.cancel(), so reader.read() resolves with
    // {done: true} promptly when the consumer is stopping. Without
    // this, a pending read() against a mocked/hung stream would sit
    // forever and .stop() would appear to hang.
    const onAbort = (): void => {
      reader.cancel().catch(() => {
        /* cancel may race with natural close — swallow */
      });
    };
    if (signal.aborted) {
      onAbort();
    } else {
      signal.addEventListener('abort', onAbort, { once: true });
    }
    try {
      for (;;) {
        if (signal.aborted) {
          return;
        }
        const { value, done } = await reader.read();
        if (done) {
          return;
        }
        buffer += decoder.decode(value, { stream: true });
        let eventEnd = buffer.indexOf('\n\n');
        while (eventEnd !== -1) {
          const rawEvent = buffer.slice(0, eventEnd);
          buffer = buffer.slice(eventEnd + 2);
          await this.parseAndDispatch(rawEvent);
          eventEnd = buffer.indexOf('\n\n');
        }
      }
    } finally {
      signal.removeEventListener('abort', onAbort);
      try {
        reader.releaseLock();
      } catch {
        // releaseLock throws when the reader is already closed — OK.
      }
    }
  }

  private async parseAndDispatch(rawEvent: string): Promise<void> {
    // Comment lines start with ":" and are ignored.
    const lines = rawEvent.split('\n').filter((l) => l !== '' && !l.startsWith(':'));
    if (lines.length === 0) {
      return;
    }
    let id: string | null = null;
    let eventType: string | null = null;
    const dataParts: string[] = [];
    for (const line of lines) {
      const colonIdx = line.indexOf(':');
      if (colonIdx === -1) {
        continue;
      }
      const field = line.slice(0, colonIdx).trim();
      // Strip exactly one leading space after the colon per spec.
      const value = line.slice(colonIdx + 1).replace(/^ /, '');
      if (field === 'id') {
        id = value;
      } else if (field === 'event') {
        eventType = value;
      } else if (field === 'data') {
        dataParts.push(value);
      }
    }
    if (id === null || eventType === null || dataParts.length === 0) {
      this.log('[stream] incomplete frame, dropping');
      return;
    }
    const dataText = dataParts.join('\n');
    let parsed: StreamEventFrame;
    try {
      parsed = JSON.parse(dataText) as StreamEventFrame;
    } catch {
      this.log(`[stream] invalid JSON payload for event ${id}`);
      return;
    }
    if (!isSupportedSchema(parsed)) {
      this.log(
        `[stream] schema mismatch — expected 1.0, got ${parsed.schema_version}`,
      );
      return;
    }
    // Persist the last event_id so a reconnect replays from the
    // correct point. We do this BEFORE dispatch so a listener
    // exception doesn't lose the ack point.
    this.lastEventId = id;
    for (const l of this.listeners) {
      try {
        await l(parsed);
      } catch (exc) {
        this.log(
          `[stream] listener threw for ${parsed.event_type}: ${
            exc instanceof Error ? exc.message : String(exc)
          }`,
        );
      }
    }
  }
}

function trimTrailingSlash(url: string): string {
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise<void>((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      signal.removeEventListener('abort', onAbort);
      resolve();
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}
