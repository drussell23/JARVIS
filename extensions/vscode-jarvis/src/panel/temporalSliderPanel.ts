/**
 * Temporal Slider panel — interactive read-only webview for Gap #1.
 *
 * Closes the §2 Deep Observability "no SSE-driven temporal slider
 * for replay across the DAG" gap. The data is on the agent
 * (DAG GET routes + replay GET routes + replay_start/end SSE);
 * this panel renders it.
 *
 * Authority discipline:
 *   * READ-ONLY — the panel never POSTs. The only inbound
 *     messages are ``select_session`` / ``select_record`` /
 *     ``set_prefix_filter`` / ``refresh``. No write surface; no
 *     policy decisions.
 *   * Strict CSP with nonce; no eval; no external script sources.
 *   * Webview script never makes network calls — every fetch
 *     routes through the extension via ``postMessage`` →
 *     ``ObservabilityClient`` GETs.
 *
 * State machine (entirely in the extension host):
 *   * ``selectedSessionId``    — null initially; set on session pick
 *   * ``selectedRecordIndex``  — -1 initially; 0-based after pick
 *   * ``prefix``               — empty initially; debounced from input
 *   * Each state change fetches the relevant slice + re-renders
 */

import * as vscode from 'vscode';
import { ObservabilityClient, ObservabilityError } from '../api/client';
import {
  DagRecordResponse,
  DagSessionResponse,
  ReplayHealthResponse,
  ReplayVerdictsResponse,
  SessionListResponse,
  StreamEventFrame,
  isReplayEvent,
} from '../api/types';
import {
  TemporalSliderState,
  renderErrorHtml,
  renderHtml,
} from './temporalSliderRenderers';

interface MessageFromWebview {
  readonly type:
    | 'refresh'
    | 'select_session'
    | 'select_record'
    | 'set_prefix_filter';
  readonly payload?: unknown;
}

interface SelectSessionPayload {
  readonly session_id: string;
}

interface SelectRecordPayload {
  readonly record_index: number;
}

interface PrefixFilterPayload {
  readonly prefix: string;
}

export class TemporalSliderPanel {
  private static readonly viewType =
    'jarvisObservability.temporalSlider';

  private panel: vscode.WebviewPanel | null = null;
  private nonce: string = '';

  // State machine — single source of truth for what the panel
  // should render.
  private state: TemporalSliderState = {
    sessions: null,
    selectedSessionId: null,
    dag: null,
    selectedRecordIndex: -1,
    record: null,
    replayHealth: null,
    replayVerdicts: null,
  };

  // Debounce / dedup state.
  private prefix: string = '';
  private fetchToken: number = 0;  // monotonic; latest wins on race

  public constructor(
    private readonly getClient: () => ObservabilityClient,
    private readonly logger: (msg: string) => void,
  ) {}

  public async show(): Promise<void> {
    if (this.panel === null) {
      this.nonce = generateNonce();
      this.panel = vscode.window.createWebviewPanel(
        TemporalSliderPanel.viewType,
        'JARVIS Temporal Slider',
        {
          viewColumn: vscode.ViewColumn.Active,
          preserveFocus: false,
        },
        {
          enableScripts: true,
          enableCommandUris: false,
          enableFindWidget: false,
          retainContextWhenHidden: true,
          localResourceRoots: [],
        },
      );
      this.panel.onDidDispose(() => {
        this.panel = null;
        this.resetState();
      });
      this.panel.webview.onDidReceiveMessage(
        (msg: MessageFromWebview) => {
          this.handleMessage(msg).catch((exc) =>
            this.logger(
              `temporalSlider message handler raised: ${
                exc instanceof Error ? exc.message : String(exc)
              }`,
            ),
          );
        },
      );
    } else {
      this.panel.reveal(vscode.ViewColumn.Active, false);
    }
    await this.refresh();
  }

  public isOpen(): boolean {
    return this.panel !== null;
  }

  public dispose(): void {
    if (this.panel !== null) {
      this.panel.dispose();
      this.panel = null;
    }
    this.resetState();
  }

  /**
   * Fetch the bare-state-required slices and re-render. Best-
   * effort: per-slice fetch failures degrade to ``null`` for that
   * slice; the renderer surfaces empty/loading UX. A single
   * fetch token is held so out-of-order responses don't clobber
   * the latest user action.
   */
  public async refresh(): Promise<void> {
    if (this.panel === null) {
      return;
    }
    const myToken = ++this.fetchToken;

    // Sessions + replay health are independent of selection.
    const [sessions, health] = await Promise.all([
      this.fetchSessions(),
      this.fetchReplayHealth(),
    ]);

    if (myToken !== this.fetchToken || this.panel === null) {
      return;  // superseded by a newer fetch
    }

    this.state = {
      ...this.state, sessions, replayHealth: health,
    };

    // Selected session: re-fetch DAG + selected record + verdicts.
    if (this.state.selectedSessionId !== null) {
      const dag = await this.fetchDag(this.state.selectedSessionId);
      if (myToken !== this.fetchToken || this.panel === null) return;

      // Clamp selectedRecordIndex into the new DAG's record range
      // (the DAG may have grown / shrunk between fetches).
      let sel = this.state.selectedRecordIndex;
      if (dag !== null) {
        if (sel >= dag.record_ids.length) sel = dag.record_ids.length - 1;
        if (sel < 0 && dag.record_ids.length > 0) sel = 0;
      } else {
        sel = -1;
      }

      let record: DagRecordResponse | null = null;
      if (
        dag !== null && sel >= 0 && sel < dag.record_ids.length
      ) {
        record = await this.fetchRecord(
          this.state.selectedSessionId, dag.record_ids[sel],
        );
        if (myToken !== this.fetchToken || this.panel === null) return;
      }

      this.state = {
        ...this.state,
        dag,
        selectedRecordIndex: sel,
        record,
      };
    } else {
      this.state = {
        ...this.state, dag: null,
        selectedRecordIndex: -1, record: null,
      };
    }

    const verdicts = await this.fetchVerdicts();
    if (myToken !== this.fetchToken || this.panel === null) return;

    this.state = { ...this.state, replayVerdicts: verdicts };
    this.renderState();
  }

  /**
   * SSE event hook — refresh on replay_start / replay_end so the
   * verdicts ribbon stays current. Idempotent: if the panel
   * isn't open or the event is unrelated, it's a no-op.
   */
  public async onStreamEvent(
    frame: StreamEventFrame,
  ): Promise<void> {
    if (this.panel === null) {
      return;
    }
    if (!isReplayEvent(frame)) {
      return;
    }
    // Only re-fetch the verdicts slice; full refresh would cause
    // an unnecessary full DAG re-fetch on every replay tick.
    const myToken = ++this.fetchToken;
    const verdicts = await this.fetchVerdicts();
    if (myToken !== this.fetchToken || this.panel === null) return;
    this.state = { ...this.state, replayVerdicts: verdicts };
    this.renderState();
  }

  // --- message handler ----------------------------------------------------

  private async handleMessage(msg: MessageFromWebview): Promise<void> {
    if (this.panel === null) {
      return;
    }
    switch (msg.type) {
      case 'refresh':
        await this.refresh();
        return;
      case 'select_session':
        await this.handleSelectSession(
          msg.payload as SelectSessionPayload,
        );
        return;
      case 'select_record':
        await this.handleSelectRecord(
          msg.payload as SelectRecordPayload,
        );
        return;
      case 'set_prefix_filter':
        await this.handlePrefixFilter(
          msg.payload as PrefixFilterPayload,
        );
        return;
      default:
        this.logger(
          `temporalSlider: unknown message type=${
            (msg as { type?: string }).type ?? '(none)'
          }`,
        );
    }
  }

  private async handleSelectSession(
    payload: SelectSessionPayload,
  ): Promise<void> {
    const sid = payload?.session_id;
    if (typeof sid !== 'string' || sid === '') return;
    // Reset record selection on new session — a different DAG.
    this.state = {
      ...this.state,
      selectedSessionId: sid,
      selectedRecordIndex: 0,  // auto-select first record
      record: null,
    };
    await this.refresh();
  }

  private async handleSelectRecord(
    payload: SelectRecordPayload,
  ): Promise<void> {
    const idx = payload?.record_index;
    if (typeof idx !== 'number' || !Number.isInteger(idx)) return;
    if (this.state.selectedSessionId === null) return;
    if (this.state.dag === null) return;
    if (idx < 0 || idx >= this.state.dag.record_ids.length) return;

    const myToken = ++this.fetchToken;
    const recordId = this.state.dag.record_ids[idx];
    const record = await this.fetchRecord(
      this.state.selectedSessionId, recordId,
    );
    if (myToken !== this.fetchToken || this.panel === null) return;
    this.state = {
      ...this.state, selectedRecordIndex: idx, record,
    };
    this.renderState();
  }

  private async handlePrefixFilter(
    payload: PrefixFilterPayload,
  ): Promise<void> {
    const p = payload?.prefix ?? '';
    if (typeof p !== 'string') return;
    this.prefix = p;
    // Re-fetch sessions only (filter affects only this slice).
    const myToken = ++this.fetchToken;
    const sessions = await this.fetchSessions();
    if (myToken !== this.fetchToken || this.panel === null) return;
    this.state = { ...this.state, sessions };
    this.renderState();
  }

  // --- fetch helpers -----------------------------------------------------
  // Each helper NEVER raises — fetch failures return null. The
  // renderer's empty/error UX kicks in for null slices.

  private async fetchSessions(): Promise<SessionListResponse | null> {
    try {
      return await this.getClient().sessionList(
        this.prefix !== '' ? { prefix: this.prefix } : undefined,
      );
    } catch (exc) {
      this.logErr('sessionList', exc);
      return null;
    }
  }

  private async fetchReplayHealth(): Promise<ReplayHealthResponse | null> {
    try {
      return await this.getClient().replayHealth();
    } catch (exc) {
      this.logErr('replayHealth', exc);
      return null;
    }
  }

  private async fetchDag(
    sessionId: string,
  ): Promise<DagSessionResponse | null> {
    try {
      return await this.getClient().dagSession(sessionId);
    } catch (exc) {
      this.logErr('dagSession', exc);
      return null;
    }
  }

  private async fetchRecord(
    sessionId: string, recordId: string,
  ): Promise<DagRecordResponse | null> {
    try {
      return await this.getClient().dagRecord(sessionId, recordId);
    } catch (exc) {
      this.logErr('dagRecord', exc);
      return null;
    }
  }

  private async fetchVerdicts(): Promise<ReplayVerdictsResponse | null> {
    try {
      return await this.getClient().replayVerdicts({ limit: 20 });
    } catch (exc) {
      this.logErr('replayVerdicts', exc);
      return null;
    }
  }

  private logErr(slice: string, exc: unknown): void {
    if (exc instanceof ObservabilityError) {
      this.logger(
        `temporalSlider.${slice}: ${exc.status} ${exc.reasonCode}`,
      );
    } else {
      this.logger(
        `temporalSlider.${slice}: ${
          exc instanceof Error ? exc.message : String(exc)
        }`,
      );
    }
  }

  private renderState(): void {
    if (this.panel === null) return;
    try {
      this.panel.webview.html = renderHtml(this.state, this.nonce);
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      this.panel.webview.html = renderErrorHtml(msg, this.nonce);
    }
  }

  private resetState(): void {
    this.state = {
      sessions: null, selectedSessionId: null, dag: null,
      selectedRecordIndex: -1, record: null,
      replayHealth: null, replayVerdicts: null,
    };
    this.prefix = '';
  }
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------


function generateNonce(): string {
  const crypto = require('crypto') as typeof import('crypto');
  return crypto.randomBytes(16).toString('hex');
}
