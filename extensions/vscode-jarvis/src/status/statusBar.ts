/**
 * Status bar item that reflects the live stream connection state.
 *
 * Click → runs connect/disconnect. Tooltip shows the endpoint + last
 * event id when available. No network calls happen from this
 * module — it only reacts to state transitions pushed by the
 * StreamConsumer.
 */

import * as vscode from 'vscode';
import { StreamState } from '../api/stream';

export class StatusBar {
  private readonly item: vscode.StatusBarItem;
  private lastState: StreamState = 'disconnected';
  private endpoint = '';

  public constructor() {
    this.item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Right,
      97,
    );
    this.setState('disconnected');
    this.item.show();
  }

  public setEndpoint(endpoint: string): void {
    this.endpoint = endpoint;
    this.refreshTooltip();
  }

  public setState(state: StreamState): void {
    this.lastState = state;
    switch (state) {
      case 'connecting':
        this.item.text = '$(sync~spin) JARVIS';
        this.item.backgroundColor = undefined;
        this.item.command = 'jarvisObservability.disconnect';
        break;
      case 'connected':
        this.item.text = '$(pulse) JARVIS';
        this.item.backgroundColor = undefined;
        this.item.command = 'jarvisObservability.disconnect';
        break;
      case 'reconnecting':
        this.item.text = '$(sync~spin) JARVIS reconnecting';
        this.item.backgroundColor = new vscode.ThemeColor(
          'statusBarItem.warningBackground',
        );
        this.item.command = 'jarvisObservability.disconnect';
        break;
      case 'disconnected':
        this.item.text = '$(circle-slash) JARVIS';
        this.item.backgroundColor = undefined;
        this.item.command = 'jarvisObservability.connect';
        break;
      case 'error':
        this.item.text = '$(alert) JARVIS error';
        this.item.backgroundColor = new vscode.ThemeColor(
          'statusBarItem.errorBackground',
        );
        this.item.command = 'jarvisObservability.connect';
        break;
      case 'closed':
        this.item.text = '$(circle-slash) JARVIS';
        this.item.backgroundColor = undefined;
        this.item.command = 'jarvisObservability.connect';
        break;
      default:
        this.item.text = '$(question) JARVIS';
        this.item.backgroundColor = undefined;
    }
    this.refreshTooltip();
  }

  public getState(): StreamState {
    return this.lastState;
  }

  private refreshTooltip(): void {
    const md = new vscode.MarkdownString(
      `**JARVIS Observability**\n\n` +
        `Endpoint: \`${this.endpoint || '(unset)'}\`\n\n` +
        `State: \`${this.lastState}\`\n\n` +
        `[Show output log](command:jarvisObservability.showLog)`,
    );
    md.isTrusted = true;
    this.item.tooltip = md;
  }

  public dispose(): void {
    this.item.dispose();
  }
}
