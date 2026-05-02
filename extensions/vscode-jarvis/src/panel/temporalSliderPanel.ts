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
  DagDiffResponse,
  DagRecordResponse,
  DagSessionResponse,
  ReplayHealthResponse,
  ReplayVerdictsResponse,
  SessionListResponse,
  StreamEventFrame,
  isReplayEvent,
} from '../api/types';
import { EntityRef } from '../api/entityTypes';
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
    | 'set_prefix_filter'
    | 'set_anchor'
    | 'clear_anchor';
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

interface SetAnchorPayload {
  readonly record_id: string;
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
    anchorRecordId: null,
    diff: null,
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

  /**
   * Q2 Slice 7 — cross-panel entity reveal. Scopes the panel to
   * the supplied entity:
   *   * ``session_id`` → opens panel + selects that session
   *   * ``record_id`` → opens panel + selects session (from
   *     ``context.session_id``) + scrubs to the record
   *
   * Other kinds are no-ops (the kind isn't owned by this panel;
   * the linker should have filtered).
   *
   * NEVER raises. Best-effort: if the session isn't in the
   * current sessions list, the panel still opens but stays at
   * the default state.
   */
  public async revealEntity(ref: EntityRef): Promise<void> {
    await this.show();
    if (ref.kind === 'session_id') {
      await this.handleSelectSession({ session_id: ref.id });
      return;
    }
    if (ref.kind === 'record_id') {
      const sessionId = ref.context?.session_id;
      if (typeof sessionId !== 'string' || sessionId === '') {
        this.logger(
          `temporalSlider.revealEntity: record_id ${ref.id} ` +
          `missing session_id context — opening panel only`,
        );
        return;
      }
      // Set the session first
      if (this.state.selectedSessionId !== sessionId) {
        await this.handleSelectSession({ session_id: sessionId });
      }
      // Then locate + select the record by id. Refresh re-built
      // the DAG; selectedRecordIndex was set to 0 by handleSelectSession.
      const dag = this.state.dag;
      if (dag === null) return;
      const idx = dag.record_ids.indexOf(ref.id);
      if (idx >= 0) {
        await this.handleSelectRecord({ record_index: idx });
      } else {
        this.logger(
          `temporalSlider.revealEntity: record_id ${ref.id} ` +
          `not in session ${sessionId} DAG`,
        );
      }
    }
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
      case 'set_anchor':
        await this.handleSetAnchor(
          msg.payload as SetAnchorPayload,
        );
        return;
      case 'clear_anchor':
        await this.handleClearAnchor();
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
    // Q2 Slice 6: ALSO clear the anchor — diffing across sessions
    // is unsupported (substrate forbids cross-session DAG queries),
    // so a stale anchor would dangle.
    this.state = {
      ...this.state,
      selectedSessionId: sid,
      selectedRecordIndex: 0,  // auto-select first record
      record: null,
      anchorRecordId: null,
      diff: null,
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
    const sid = this.state.selectedSessionId;
    const recordId = this.state.dag.record_ids[idx];
    const record = await this.fetchRecord(sid, recordId);
    if (myToken !== this.fetchToken || this.panel === null) return;

    // Q2 Slice 6 — auto-fetch diff against the anchor when one
    // is set AND the new record differs from the anchor.
    let diff: DagDiffResponse | null = null;
    const anchor = this.state.anchorRecordId;
    if (anchor !== null && anchor !== recordId) {
      // Render with diff=null first (renderer shows "computing…")
      this.state = {
        ...this.state,
        selectedRecordIndex: idx,
        record,
        diff: null,
      };
      this.renderState();
      diff = await this.fetchDiff(sid, anchor, recordId);
      if (myToken !== this.fetchToken || this.panel === null) return;
      this.state = { ...this.state, diff };
      this.renderState();
      return;
    }

    // No anchor → clear any stale diff
    this.state = {
      ...this.state,
      selectedRecordIndex: idx,
      record,
      diff: anchor === null ? null : this.state.diff,
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

  private async handleSetAnchor(
    payload: SetAnchorPayload,
  ): Promise<void> {
    const rid = payload?.record_id;
    if (typeof rid !== 'string' || rid === '') return;
    if (this.state.selectedSessionId === null) return;
    this.state = {
      ...this.state, anchorRecordId: rid, diff: null,
    };
    this.renderState();
    // If the operator's currently-selected record is different
    // from the new anchor, kick off a diff fetch immediately.
    const cur = this.state.record;
    const sid = this.state.selectedSessionId;
    if (cur !== null && cur.record_id !== rid && sid !== null) {
      const myToken = ++this.fetchToken;
      const diff = await this.fetchDiff(sid, rid, cur.record_id);
      if (myToken !== this.fetchToken || this.panel === null) return;
      this.state = { ...this.state, diff };
      this.renderState();
    }
  }

  private async handleClearAnchor(): Promise<void> {
    this.state = {
      ...this.state, anchorRecordId: null, diff: null,
    };
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

  private async fetchDiff(
    sessionId: string, recordIdA: string, recordIdB: string,
  ): Promise<DagDiffResponse | null> {
    try {
      return await this.getClient().dagDiff(
        sessionId, recordIdA, recordIdB,
      );
    } catch (exc) {
      this.logErr('dagDiff', exc);
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
      anchorRecordId: null, diff: null,
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
