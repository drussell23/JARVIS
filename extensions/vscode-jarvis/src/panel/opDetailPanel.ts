/**
 * Op Detail webview — read-only visualization for a single op_id.
 *
 * Renders task states as a colored list. Minimal CSS; respects the
 * VS Code theme via CSS variables. The webview is reused across op
 * selections to avoid flickering.
 *
 * Security:
 *   * `enableScripts: false` — no JS, pure HTML output.
 *   * `localResourceRoots: []` — no local asset loading.
 *   * Content-Security-Policy header disallows inline scripts and
 *     external origins (the only inline content is the style tag,
 *     which we allow via `style-src 'unsafe-inline'`).
 */

import * as vscode from 'vscode';
import { ObservabilityClient } from '../api/client';
import { TaskDetailResponse, TaskState } from '../api/types';

export class OpDetailPanel {
  private static readonly viewType = 'jarvisObservability.opDetail';

  private panel: vscode.WebviewPanel | null = null;
  private currentOpId: string | null = null;

  public constructor(private readonly getClient: () => ObservabilityClient) {}

  public async show(opId: string): Promise<void> {
    this.currentOpId = opId;
    if (this.panel === null) {
      this.panel = vscode.window.createWebviewPanel(
        OpDetailPanel.viewType,
        `JARVIS Op: ${opId}`,
        { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
        {
          enableScripts: false,
          enableCommandUris: false,
          enableFindWidget: true,
          retainContextWhenHidden: false,
          localResourceRoots: [],
        },
      );
      this.panel.onDidDispose(() => {
        this.panel = null;
        this.currentOpId = null;
      });
    } else {
      this.panel.title = `JARVIS Op: ${opId}`;
      this.panel.reveal(vscode.ViewColumn.Beside, true);
    }
    await this.refresh();
  }

  public isShowing(opId: string): boolean {
    return this.panel !== null && this.currentOpId === opId;
  }

  public async refresh(): Promise<void> {
    if (this.panel === null || this.currentOpId === null) {
      return;
    }
    try {
      const detail = await this.getClient().taskDetail(this.currentOpId);
      this.panel.webview.html = renderHtml(detail);
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      this.panel.webview.html = renderErrorHtml(this.currentOpId, msg);
    }
  }

  public dispose(): void {
    if (this.panel !== null) {
      this.panel.dispose();
      this.panel = null;
    }
  }
}

// --- Rendering (pure functions, exported for test coverage) ----------------

export function renderHtml(detail: TaskDetailResponse): string {
  const closedBadge = detail.closed
    ? '<span class="badge closed">CLOSED</span>'
    : '<span class="badge open">LIVE</span>';
  const tasksHtml = detail.tasks
    .map((t) => {
      const stateClass = `state-${t.state}`;
      return `
        <li class="task ${stateClass}">
          <div class="task-header">
            <span class="task-id">${escapeHtml(t.task_id)}</span>
            <span class="state-chip state-${t.state}">${t.state}</span>
            <span class="seq">#${t.sequence}</span>
          </div>
          <div class="title">${escapeHtml(t.title || '(no title)')}</div>
          ${t.body !== '' ? `<div class="body">${escapeHtml(t.body)}</div>` : ''}
          ${
            t.cancel_reason !== ''
              ? `<div class="cancel-reason">reason: ${escapeHtml(t.cancel_reason)}</div>`
              : ''
          }
        </li>`;
    })
    .join('\n');
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline';">
<title>JARVIS Op: ${escapeHtml(detail.op_id)}</title>
<style>
  body {
    font-family: var(--vscode-font-family);
    color: var(--vscode-foreground);
    background: var(--vscode-editor-background);
    padding: 14px;
  }
  h1 { font-size: 1.1em; margin: 0 0 6px 0; }
  .op-meta { color: var(--vscode-descriptionForeground); font-size: 0.9em; }
  .badge {
    display: inline-block; padding: 1px 8px; border-radius: 6px;
    font-size: 0.75em; font-weight: 600; margin-left: 6px;
  }
  .badge.closed { background: var(--vscode-inputValidation-errorBackground); }
  .badge.open   { background: var(--vscode-inputValidation-infoBackground); }
  ul.tasks { list-style: none; padding: 0; margin: 14px 0 0 0; }
  li.task {
    padding: 8px 10px; margin-bottom: 8px;
    border-left: 3px solid var(--vscode-textBlockQuote-border);
    background: var(--vscode-editorWidget-background);
  }
  li.state-in_progress { border-left-color: var(--vscode-charts-blue); }
  li.state-completed   { border-left-color: var(--vscode-charts-green); }
  li.state-cancelled   { border-left-color: var(--vscode-charts-red); opacity: 0.75; }
  .task-header { display: flex; gap: 10px; align-items: center; font-size: 0.85em; }
  .task-id { font-family: var(--vscode-editor-font-family); opacity: 0.85; }
  .state-chip {
    padding: 1px 6px; border-radius: 4px; font-size: 0.75em; font-weight: 600;
    text-transform: uppercase;
  }
  .state-chip.state-pending     { background: var(--vscode-charts-yellow); color: #000; }
  .state-chip.state-in_progress { background: var(--vscode-charts-blue);   color: #fff; }
  .state-chip.state-completed   { background: var(--vscode-charts-green);  color: #000; }
  .state-chip.state-cancelled   { background: var(--vscode-charts-red);    color: #fff; }
  .seq { opacity: 0.6; }
  .title { margin-top: 4px; font-weight: 500; }
  .body  { margin-top: 4px; white-space: pre-wrap; font-size: 0.9em; opacity: 0.85; }
  .cancel-reason {
    margin-top: 4px; font-size: 0.85em; font-style: italic;
    color: var(--vscode-errorForeground);
  }
</style>
</head>
<body>
  <h1>${escapeHtml(detail.op_id)} ${closedBadge}</h1>
  <div class="op-meta">
    ${detail.board_size} task${detail.board_size === 1 ? '' : 's'}
    ${
      detail.active_task_id !== null
        ? ` · active: <code>${escapeHtml(detail.active_task_id)}</code>`
        : ''
    }
  </div>
  <ul class="tasks">
    ${tasksHtml === '' ? '<li class="task empty">No tasks yet.</li>' : tasksHtml}
  </ul>
</body>
</html>`;
}

export function renderErrorHtml(opId: string, message: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline';">
<title>JARVIS Op: ${escapeHtml(opId)}</title>
<style>
  body {
    font-family: var(--vscode-font-family);
    color: var(--vscode-errorForeground);
    background: var(--vscode-editor-background);
    padding: 14px;
  }
  .note { color: var(--vscode-descriptionForeground); font-size: 0.9em; }
</style>
</head>
<body>
  <h1>Unable to load op detail</h1>
  <p>${escapeHtml(message)}</p>
  <p class="note">Op: <code>${escapeHtml(opId)}</code></p>
</body>
</html>`;
}

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Expose TaskState type to avoid "unused import" on strict builds.
export const _STATE_TYPE: TaskState = 'pending';
