/**
 * Op Detail webview — read-only visualization for a single op_id.
 *
 * Security:
 *   * `enableScripts: false` — no JS, pure HTML output.
 *   * `localResourceRoots: []` — no local asset loading.
 *   * CSP meta tag disallows every source except inline styles.
 */

import * as vscode from 'vscode';
import { ObservabilityClient } from '../api/client';
import { renderErrorHtml, renderHtml } from './renderers';

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
