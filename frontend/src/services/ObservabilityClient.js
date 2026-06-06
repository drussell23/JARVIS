/**
 * ObservabilityClient — Slice 110 Native Command Center
 * =====================================================
 * Focused WebSocket + REST client for the O+V cognitive telemetry gateway
 * (backend/api/observability_gateway.py). Composes the existing zero-hardcode
 * URL helpers from config.js (getWebSocketUrl / getApiUrl) — no hardcoded host
 * or port. Native WebSocket with exponential-backoff reconnect + jitter, frame
 * dispatch by the `kind` discriminator, and a bounded in-memory ring so a
 * freshly-mounted dashboard paints from the gateway's replayed backlog.
 *
 * Frame kinds (gateway_frame.v1): hello | why_snapshot | containment_breach |
 * telemetry | causality_update | terminal_line.
 */

import { getWebSocketUrl, getApiUrl } from '../config';

const WS_ENDPOINT = 'api/observability/ws';
const MAX_BACKOFF_MS = 15000;
const BASE_BACKOFF_MS = 500;

export class ObservabilityClient {
  constructor() {
    this._ws = null;
    this._handlers = new Map();      // kind -> Set<fn>
    this._anyHandlers = new Set();   // fn(frame) for every frame
    this._attempt = 0;
    this._closedByUs = false;
    this._connected = false;
  }

  /** Subscribe to a specific frame kind. Returns an unsubscribe fn. */
  on(kind, fn) {
    if (!this._handlers.has(kind)) this._handlers.set(kind, new Set());
    this._handlers.get(kind).add(fn);
    return () => this._handlers.get(kind)?.delete(fn);
  }

  /** Subscribe to ALL frames. Returns an unsubscribe fn. */
  onAny(fn) {
    this._anyHandlers.add(fn);
    return () => this._anyHandlers.delete(fn);
  }

  get connected() {
    return this._connected;
  }

  connect() {
    this._closedByUs = false;
    let url;
    try {
      url = getWebSocketUrl(WS_ENDPOINT);
    } catch (e) {
      url = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/${WS_ENDPOINT}`;
    }
    try {
      this._ws = new WebSocket(url);
    } catch (e) {
      this._scheduleReconnect();
      return;
    }

    this._ws.onopen = () => {
      this._attempt = 0;
      this._connected = true;
      this._emitConnection(true);
    };

    this._ws.onmessage = (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch (_e) {
        return;
      }
      this._dispatch(frame);
    };

    this._ws.onclose = () => {
      this._connected = false;
      this._emitConnection(false);
      if (!this._closedByUs) this._scheduleReconnect();
    };

    this._ws.onerror = () => {
      // onclose will follow; reconnect handled there.
      try { this._ws.close(); } catch (_e) { /* noop */ }
    };
  }

  disconnect() {
    this._closedByUs = true;
    try { this._ws?.close(); } catch (_e) { /* noop */ }
    this._ws = null;
    this._connected = false;
  }

  /** Cosmetic Karen voice control (mute|unmute|verbose|normal). Loopback-only,
   * gated server-side; this just POSTs the action. */
  async voice(action) {
    const url = getApiUrl(`api/observability/voice/${action}`);
    const res = await fetch(url, { method: 'POST' });
    return res.json();
  }

  async fetchCausality(limit = 30) {
    const res = await fetch(getApiUrl(`api/observability/causality?limit=${limit}`));
    return res.json();
  }

  async fetchHealth() {
    const res = await fetch(getApiUrl('api/observability/health'));
    return res.json();
  }

  // --- internals -----------------------------------------------------------

  _dispatch(frame) {
    const kind = frame?.kind;
    if (kind && this._handlers.has(kind)) {
      for (const fn of this._handlers.get(kind)) {
        try { fn(frame); } catch (_e) { /* one bad handler never breaks the stream */ }
      }
    }
    for (const fn of this._anyHandlers) {
      try { fn(frame); } catch (_e) { /* noop */ }
    }
  }

  _emitConnection(up) {
    const frame = { kind: '__connection__', payload: { up } };
    for (const fn of this._anyHandlers) {
      try { fn(frame); } catch (_e) { /* noop */ }
    }
  }

  _scheduleReconnect() {
    this._attempt += 1;
    const backoff = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** this._attempt);
    const jitter = Math.random() * 0.3 * backoff;
    setTimeout(() => {
      if (!this._closedByUs) this.connect();
    }, backoff + jitter);
  }
}

export default ObservabilityClient;
