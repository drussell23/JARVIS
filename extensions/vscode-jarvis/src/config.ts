/**
 * Type-safe settings accessors. Centralizes all reads from
 * vscode.workspace.getConfiguration so the rest of the code doesn't
 * need to know about defaults or schema.
 */

import * as vscode from 'vscode';

export interface ExtensionConfig {
  readonly endpoint: string;
  readonly enabled: boolean;
  readonly autoReconnect: boolean;
  readonly reconnectMaxBackoffMs: number;
  readonly pollIntervalMs: number;
  readonly opIdFilter: string;
  readonly maxOpsCached: number;
}

export function readConfig(): ExtensionConfig {
  const c = vscode.workspace.getConfiguration('jarvisObservability');
  return {
    endpoint: c.get<string>('endpoint', 'http://127.0.0.1:8765'),
    enabled: c.get<boolean>('enabled', true),
    autoReconnect: c.get<boolean>('autoReconnect', true),
    reconnectMaxBackoffMs: c.get<number>('reconnectMaxBackoffMs', 30_000),
    pollIntervalMs: c.get<number>('pollIntervalMs', 5000),
    opIdFilter: c.get<string>('opIdFilter', ''),
    maxOpsCached: c.get<number>('maxOpsCached', 256),
  };
}

export function onConfigChange(
  listener: (cfg: ExtensionConfig) => void,
): vscode.Disposable {
  return vscode.workspace.onDidChangeConfiguration((evt) => {
    if (evt.affectsConfiguration('jarvisObservability')) {
      listener(readConfig());
    }
  });
}
