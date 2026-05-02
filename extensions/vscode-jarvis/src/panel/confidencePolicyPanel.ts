/**
 * Confidence Policy panel — interactive webview for the Gap #2
 * Slice 4 write authority surface.
 *
 * Distinct from ``OpDetailPanel`` (read-only HTML, scripts off):
 * this panel needs operator INPUT (form for proposed deltas,
 * approve/reject buttons), so ``enableScripts: true`` with a
 * strict nonce-based CSP. The webview script communicates with
 * the extension host via VS Code's message-passing API; the
 * extension proxies HTTP calls through ``PolicyClient``.
 *
 * Security discipline:
 *   * CSP allows only the inline script with the matching nonce —
 *     no external sources, no eval, no unsafe-inline scripts.
 *   * The webview script never makes network calls directly; all
 *     I/O is mediated by the extension via ``postMessage``.
 *   * Operator input is validated client-side as a UX hint, then
 *     re-validated server-side by the cage. The client check is
 *     defense-in-depth, NOT the gate.
 */

import * as vscode from 'vscode';
import { PolicyClient, PolicyClientError } from '../api/policyClient';
import {
  ConfidencePolicy,
  ConfidenceSnapshot,
  ProposalProjection,
  classifyClientSide,
  isPolicyEventType,
} from '../api/policyTypes';
import { StreamEventFrame } from '../api/types';
import { EntityRef } from '../api/entityTypes';

interface MessageFromWebview {
  readonly type:
    | 'refresh'
    | 'propose'
    | 'approve'
    | 'reject'
    | 'classify';
  readonly payload?: unknown;
}

interface ProposePayload {
  readonly current: ConfidencePolicy;
  readonly proposed: ConfidencePolicy;
  readonly evidence_summary: string;
  readonly observation_count: number;
  readonly operator: string;
  readonly proposal_id?: string;
}

interface DecisionPayload {
  readonly proposal_id: string;
  readonly operator: string;
  readonly reason?: string;
}

export class ConfidencePolicyPanel {
  private static readonly viewType =
    'jarvisObservability.confidencePolicy';

  private panel: vscode.WebviewPanel | null = null;
  private nonce: string = '';

  public constructor(
    private readonly getClient: () => PolicyClient,
    private readonly logger: (msg: string) => void,
  ) {}

  public async show(): Promise<void> {
    if (this.panel === null) {
      this.nonce = generateNonce();
      this.panel = vscode.window.createWebviewPanel(
        ConfidencePolicyPanel.viewType,
        'JARVIS Confidence Policy',
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
      });
      this.panel.webview.onDidReceiveMessage(
        (msg: MessageFromWebview) => {
          this.handleMessage(msg).catch((exc) =>
            this.logger(
              `confidencePolicy message handler raised: ${
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
  }

  /**
   * Q2 Slice 7 — cross-panel entity reveal. Opens the panel and
   * instructs the webview to scroll to the matching proposal row
   * via a postMessage signal.
   *
   * Currently handles ``proposal_id`` only — other kinds are
   * silently ignored (the linker should have filtered).
   *
   * NEVER raises. The reveal handling is best-effort.
   */
  public async revealEntity(ref: EntityRef): Promise<void> {
    await this.show();
    if (ref.kind !== 'proposal_id') return;
    if (this.panel === null) return;
    try {
      this.panel.webview.postMessage({
        type: 'reveal_entity',
        payload: { kind: ref.kind, id: ref.id },
      });
    } catch (exc) {
      this.logger(
        `confidencePolicy.revealEntity postMessage failed: ${
          exc instanceof Error ? exc.message : String(exc)
        }`,
      );
    }
  }

  /**
   * Fetch the latest snapshot from the agent + render into the
   * webview. Best-effort: errors render an inline banner instead
   * of dropping the panel.
   */
  public async refresh(): Promise<void> {
    if (this.panel === null) {
      return;
    }
    try {
      const snapshot = await this.getClient().snapshot();
      this.panel.webview.html = renderHtml(snapshot, this.nonce);
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      this.panel.webview.html = renderErrorHtml(msg, this.nonce);
    }
  }

  /**
   * SSE event hook — when a confidence_policy_* event arrives,
   * the extension calls this to refresh the panel without
   * polling. Idempotent: if the panel isn't open or the event is
   * unrelated, it's a no-op.
   */
  public async onStreamEvent(
    frame: StreamEventFrame,
  ): Promise<void> {
    if (this.panel === null) {
      return;
    }
    if (!isPolicyEventType(frame.event_type)) {
      return;
    }
    await this.refresh();
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
      case 'propose':
        await this.handlePropose(msg.payload as ProposePayload);
        return;
      case 'approve':
        await this.handleDecision(
          msg.payload as DecisionPayload, 'approve',
        );
        return;
      case 'reject':
        await this.handleDecision(
          msg.payload as DecisionPayload, 'reject',
        );
        return;
      case 'classify':
        // Pure client-side classification (no network). Webview
        // calls this on every form change to render live UX.
        this.respondClassify(
          msg.payload as {
            current: ConfidencePolicy;
            proposed: ConfidencePolicy;
          },
        );
        return;
      default:
        this.logger(
          `confidencePolicy: unknown message type=${
            (msg as { type?: string }).type ?? '(none)'
          }`,
        );
    }
  }

  private async handlePropose(payload: ProposePayload): Promise<void> {
    if (this.panel === null) {
      return;
    }
    if (!isProposeShape(payload)) {
      this.postToast(
        'error', 'malformed proposal payload',
      );
      return;
    }
    try {
      const result = await this.getClient().propose({
        current: payload.current,
        proposed: payload.proposed,
        evidence_summary: payload.evidence_summary,
        observation_count: payload.observation_count,
        operator: payload.operator,
        proposal_id: payload.proposal_id,
      });
      this.postToast(
        'success',
        `proposed ${result.proposal_id} (${result.kind})`,
      );
      await this.refresh();
    } catch (exc) {
      const msg = exc instanceof PolicyClientError
        ? `${exc.reasonCode}: ${exc.detail || exc.message}`
        : (exc instanceof Error ? exc.message : String(exc));
      this.postToast('error', `propose failed: ${msg}`);
    }
  }

  private async handleDecision(
    payload: DecisionPayload,
    action: 'approve' | 'reject',
  ): Promise<void> {
    if (this.panel === null) {
      return;
    }
    if (!isDecisionShape(payload)) {
      this.postToast(
        'error', 'malformed decision payload',
      );
      return;
    }
    try {
      const result = action === 'approve'
        ? await this.getClient().approve(
            payload.proposal_id,
            { operator: payload.operator, reason: payload.reason },
          )
        : await this.getClient().reject(
            payload.proposal_id,
            { operator: payload.operator, reason: payload.reason },
          );
      this.postToast(
        'success',
        `${action}d ${result.proposal_id}`,
      );
      await this.refresh();
    } catch (exc) {
      const msg = exc instanceof PolicyClientError
        ? `${exc.reasonCode}: ${exc.detail || exc.message}`
        : (exc instanceof Error ? exc.message : String(exc));
      this.postToast('error', `${action} failed: ${msg}`);
    }
  }

  private respondClassify(payload: {
    current: ConfidencePolicy;
    proposed: ConfidencePolicy;
  }): void {
    if (this.panel === null) {
      return;
    }
    const result = classifyClientSide(
      payload.current, payload.proposed,
    );
    this.panel.webview.postMessage({
      type: 'classify_result',
      payload: result,
    });
  }

  private postToast(
    kind: 'success' | 'error', message: string,
  ): void {
    if (this.panel === null) {
      return;
    }
    this.panel.webview.postMessage({
      type: 'toast',
      payload: { kind, message },
    });
  }
}

// ---------------------------------------------------------------------------
// Type-guard validators (defense-in-depth on incoming webview messages)
// ---------------------------------------------------------------------------

function isConfidencePolicyShape(p: unknown): p is ConfidencePolicy {
  if (typeof p !== 'object' || p === null) return false;
  const o = p as Record<string, unknown>;
  return (
    typeof o.floor === 'number' &&
    typeof o.window_k === 'number' &&
    typeof o.approaching_factor === 'number' &&
    typeof o.enforce === 'boolean'
  );
}

function isProposeShape(p: unknown): p is ProposePayload {
  if (typeof p !== 'object' || p === null) return false;
  const o = p as Record<string, unknown>;
  return (
    isConfidencePolicyShape(o.current) &&
    isConfidencePolicyShape(o.proposed) &&
    typeof o.evidence_summary === 'string' &&
    typeof o.observation_count === 'number' &&
    typeof o.operator === 'string' &&
    (o.proposal_id === undefined || typeof o.proposal_id === 'string')
  );
}

function isDecisionShape(p: unknown): p is DecisionPayload {
  if (typeof p !== 'object' || p === null) return false;
  const o = p as Record<string, unknown>;
  return (
    typeof o.proposal_id === 'string' &&
    typeof o.operator === 'string' &&
    (o.reason === undefined || typeof o.reason === 'string')
  );
}

// ---------------------------------------------------------------------------
// Nonce + HTML rendering
// ---------------------------------------------------------------------------

function generateNonce(): string {
  // Cryptographically random 128-bit nonce, hex-encoded. Imported
  // lazily so non-Node test environments can stub.
  const crypto = require('crypto') as typeof import('crypto');
  return crypto.randomBytes(16).toString('hex');
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderRow(
  k: string, v: unknown,
): string {
  return (
    `<tr><td class="k">${escapeHtml(k)}</td>` +
    `<td class="v">${escapeHtml(String(v))}</td></tr>`
  );
}

function renderProposalRow(p: ProposalProjection): string {
  const id = escapeHtml(p.proposal_id);
  const decisionBy = p.operator_decision_by
    ? `<span class="dim">by ${escapeHtml(p.operator_decision_by)}</span>`
    : '';
  const buttons = p.status === 'pending'
    ? `<button data-action="approve" data-id="${id}">Approve</button>` +
      `<button data-action="reject" data-id="${id}" class="warn">Reject</button>`
    : '';
  return (
    `<tr><td>${id}</td>` +
    `<td>${escapeHtml(p.kind)}</td>` +
    `<td><span class="status status-${escapeHtml(p.status)}">` +
    `${escapeHtml(p.status)}</span> ${decisionBy}</td>` +
    `<td>${escapeHtml(p.proposed_at)}</td>` +
    `<td>${buttons}</td></tr>`
  );
}

export function renderHtml(
  snapshot: ConfidenceSnapshot, nonce: string,
): string {
  const ce = snapshot.current_effective;
  const adapted = snapshot.adapted;
  const proposals = snapshot.proposals;

  const proposalRows = proposals.items
    .map(renderProposalRow)
    .join('') ||
    '<tr><td colspan="5" class="dim">no proposals on record</td></tr>';

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>JARVIS Confidence Policy</title>
<style>
  body { font: 13px/1.45 -apple-system, BlinkMacSystemFont, sans-serif; padding: 16px; }
  h1 { font-size: 14px; margin: 0 0 12px 0; }
  h2 { font-size: 13px; margin: 18px 0 6px 0; color: var(--vscode-descriptionForeground); }
  table { border-collapse: collapse; width: 100%; margin-bottom: 8px; }
  td, th { padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--vscode-panel-border); font-size: 12px; }
  td.k { color: var(--vscode-descriptionForeground); width: 240px; }
  td.v { font-family: monospace; }
  .dim { color: var(--vscode-descriptionForeground); }
  .status { padding: 1px 6px; border-radius: 3px; font-size: 11px; text-transform: uppercase; }
  .status-pending  { background: var(--vscode-charts-yellow);  color: black; }
  .status-approved { background: var(--vscode-charts-green);   color: black; }
  .status-rejected { background: var(--vscode-charts-red);     color: white; }
  form { border: 1px solid var(--vscode-panel-border); padding: 12px; margin-top: 8px; }
  fieldset { border: 1px solid var(--vscode-panel-border); padding: 8px; margin: 6px 0; }
  legend { font-size: 11px; color: var(--vscode-descriptionForeground); padding: 0 4px; }
  label { display: inline-block; width: 200px; font-size: 12px; }
  input, textarea, select { font-family: monospace; padding: 2px 4px; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); }
  button { padding: 4px 10px; margin-right: 6px; cursor: pointer; background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; }
  button.warn { background: var(--vscode-errorForeground); color: white; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  #classify { margin-top: 6px; font-style: italic; }
  #classify.tighten { color: var(--vscode-charts-green); }
  #classify.loosen { color: var(--vscode-charts-red); }
  #classify.no-op { color: var(--vscode-descriptionForeground); }
  #toast { position: fixed; top: 12px; right: 12px; padding: 8px 12px; border-radius: 4px; display: none; max-width: 360px; }
  #toast.success { background: var(--vscode-charts-green); color: black; }
  #toast.error { background: var(--vscode-errorForeground); color: white; }
  /* Q2 Slice 7 — reveal-entity flash highlight */
  @keyframes reveal-pulse {
    0%   { outline: 0 solid var(--vscode-charts-yellow); }
    50%  { outline: 4px solid var(--vscode-charts-yellow); }
    100% { outline: 0 solid var(--vscode-charts-yellow); }
  }
  .reveal-flash { animation: reveal-pulse 1.5s ease-out; outline-offset: 2px; }
  .small { font-size: 11px; color: var(--vscode-descriptionForeground); }
  .row { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
</style>
</head>
<body>
<h1>JARVIS Confidence Policy</h1>
<div class="small">
  Substrate: ${snapshot.policy_substrate_enabled ? 'enabled' : '<span class="dim">disabled</span>'}.
  Loader: ${adapted.loader_enabled ? 'enabled' : '<span class="dim">disabled</span>'}.
</div>

<h2>Current Effective</h2>
<table>
  ${renderRow('floor', ce.floor)}
  ${renderRow('window_k', ce.window_k)}
  ${renderRow('approaching_factor', ce.approaching_factor)}
  ${renderRow('enforce', ce.enforce)}
</table>

<h2>Adapted YAML</h2>
${adapted.in_effect
    ? `<table>` +
      `${renderRow('proposal_id', adapted.proposal_id)}` +
      `${renderRow('approved_at', adapted.approved_at)}` +
      `${renderRow('approved_by', adapted.approved_by)}` +
      `${renderRow('floor (adapted)', adapted.values.floor ?? '∅')}` +
      `${renderRow('window_k (adapted)', adapted.values.window_k ?? '∅')}` +
      `${renderRow('approaching_factor (adapted)', adapted.values.approaching_factor ?? '∅')}` +
      `${renderRow('enforce (adapted)', adapted.values.enforce ?? '∅')}` +
      `</table>`
    : `<div class="dim">no adapted thresholds in effect</div>`}

<h2>Proposals (${proposals.pending} pending • ${proposals.approved} approved • ${proposals.rejected} rejected)</h2>
<table>
  <thead><tr><th>id</th><th>kind</th><th>status</th><th>proposed_at</th><th>action</th></tr></thead>
  <tbody>${proposalRows}</tbody>
</table>

<h2>Submit Proposal</h2>
<form id="propose-form">
  <fieldset>
    <legend>operator</legend>
    <div class="row">
      <label for="operator">operator name</label>
      <input id="operator" type="text" required minlength="1" maxlength="64" placeholder="e.g. alice">
    </div>
    <div class="row">
      <label for="evidence">evidence summary <span class="dim">(must include →)</span></label>
      <input id="evidence" type="text" required minlength="3" maxlength="512" placeholder="floor 0.05 → 0.10; observed N events" style="width: 360px;">
    </div>
    <div class="row">
      <label for="obs-count">observation count</label>
      <input id="obs-count" type="number" required min="1" max="999" value="5">
    </div>
  </fieldset>

  <fieldset>
    <legend>proposed thresholds (only tightenings accepted)</legend>
    <div class="row">
      <label for="floor">floor <span class="small">(↑ tightens, range [0,1])</span></label>
      <input id="floor" type="number" required step="0.01" min="0" max="1" value="${ce.floor ?? 0.05}">
    </div>
    <div class="row">
      <label for="window">window_k <span class="small">(↓ tightens, ≥1)</span></label>
      <input id="window" type="number" required step="1" min="1" value="${ce.window_k ?? 16}">
    </div>
    <div class="row">
      <label for="approaching">approaching_factor <span class="small">(↑ tightens, ≥1)</span></label>
      <input id="approaching" type="number" required step="0.1" min="1" value="${ce.approaching_factor ?? 1.5}">
    </div>
    <div class="row">
      <label for="enforce">enforce <span class="small">(false → true tightens)</span></label>
      <input id="enforce" type="checkbox" ${ce.enforce ? 'checked' : ''}>
    </div>
  </fieldset>

  <div id="classify" class="no-op">no-op proposal (current = proposed)</div>
  <div style="margin-top: 8px;">
    <button id="submit-btn" type="submit">Submit Proposal</button>
    <button id="refresh-btn" type="button">Refresh</button>
  </div>
</form>

<div id="toast"></div>

<script nonce="${nonce}">
  (function () {
    const vscode = acquireVsCodeApi();
    const current = ${JSON.stringify(ce)};

    function readForm() {
      const proposed = {
        floor: parseFloat(document.getElementById('floor').value),
        window_k: parseInt(document.getElementById('window').value, 10),
        approaching_factor: parseFloat(document.getElementById('approaching').value),
        enforce: document.getElementById('enforce').checked,
      };
      return {
        current: {
          floor: Number(current.floor),
          window_k: Number(current.window_k),
          approaching_factor: Number(current.approaching_factor),
          enforce: Boolean(current.enforce),
        },
        proposed,
        evidence_summary: document.getElementById('evidence').value,
        observation_count: parseInt(document.getElementById('obs-count').value, 10),
        operator: document.getElementById('operator').value,
      };
    }

    function classify() {
      const f = readForm();
      vscode.postMessage({
        type: 'classify',
        payload: { current: f.current, proposed: f.proposed },
      });
    }

    document.getElementById('floor').addEventListener('input', classify);
    document.getElementById('window').addEventListener('input', classify);
    document.getElementById('approaching').addEventListener('input', classify);
    document.getElementById('enforce').addEventListener('change', classify);

    document.getElementById('propose-form').addEventListener('submit', function (e) {
      e.preventDefault();
      vscode.postMessage({ type: 'propose', payload: readForm() });
    });

    document.getElementById('refresh-btn').addEventListener('click', function () {
      vscode.postMessage({ type: 'refresh' });
    });

    // Approve / reject delegation on the proposals table
    document.querySelectorAll('button[data-action]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const id = btn.getAttribute('data-id');
        const action = btn.getAttribute('data-action');
        const operator = document.getElementById('operator').value || 'operator';
        const reason = action === 'reject' ? prompt('reason for rejection?') || '' : '';
        vscode.postMessage({
          type: action,
          payload: { proposal_id: id, operator: operator, reason: reason },
        });
      });
    });

    // Inbound messages from extension
    window.addEventListener('message', function (e) {
      const msg = e.data || {};
      if (msg.type === 'classify_result') {
        const el = document.getElementById('classify');
        const r = msg.payload || {};
        el.className = r.is_no_op ? 'no-op' : (r.is_tighten ? 'tighten' : 'loosen');
        el.textContent = r.reason || '';
        document.getElementById('submit-btn').disabled = r.is_no_op || !r.is_tighten;
      } else if (msg.type === 'toast') {
        const t = document.getElementById('toast');
        const p = msg.payload || {};
        t.className = p.kind || 'success';
        t.textContent = p.message || '';
        t.style.display = 'block';
        setTimeout(function () { t.style.display = 'none'; }, 4000);
      } else if (msg.type === 'reveal_entity') {
        // Q2 Slice 7 — flash the matching proposal row
        const p = msg.payload || {};
        if (p.kind === 'proposal_id' && typeof p.id === 'string') {
          const rows = document.querySelectorAll('tr');
          for (let i = 0; i < rows.length; i++) {
            const td = rows[i].querySelector('td');
            if (td && td.textContent === p.id) {
              rows[i].scrollIntoView({behavior: 'smooth', block: 'center'});
              rows[i].classList.add('reveal-flash');
              setTimeout(function () {
                rows[i].classList.remove('reveal-flash');
              }, 1500);
              break;
            }
          }
        }
      }
    });

    // Trigger initial classify so the submit button reflects current state
    classify();
  })();
</script>
</body>
</html>`;
}

export function renderErrorHtml(error: string, nonce: string): string {
  return `<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { font: 13px -apple-system, sans-serif; padding: 16px; }
  .err { background: var(--vscode-inputValidation-errorBackground); padding: 12px; border: 1px solid var(--vscode-errorForeground); }
  pre { font-family: monospace; white-space: pre-wrap; word-break: break-word; }
  button { padding: 4px 10px; margin-top: 8px; cursor: pointer; }
</style>
</head>
<body>
<div class="err">
  <h2>Failed to load Confidence Policy</h2>
  <pre>${escapeHtml(error)}</pre>
  <button id="retry">Retry</button>
</div>
<script nonce="${nonce}">
  (function () {
    const vscode = acquireVsCodeApi();
    document.getElementById('retry').addEventListener('click', function () {
      vscode.postMessage({ type: 'refresh' });
    });
  })();
</script>
</body></html>`;
}
