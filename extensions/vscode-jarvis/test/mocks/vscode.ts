/**
 * Minimal vscode module mock for node --test.
 *
 * Only the surface opsProvider.ts + statusBar.ts exercise is mocked.
 * The extension.ts module is NOT covered by the unit-test harness —
 * it needs a real VS Code host, which is a Slice-4 concern.
 */

export class EventEmitter<T> {
  private listeners: Array<(value: T) => void> = [];
  public event = (listener: (value: T) => void): { dispose: () => void } => {
    this.listeners.push(listener);
    return {
      dispose: () => {
        this.listeners = this.listeners.filter((l) => l !== listener);
      },
    };
  };
  public fire(value: T): void {
    for (const l of this.listeners) l(value);
  }
  public dispose(): void {
    this.listeners = [];
  }
}

export enum TreeItemCollapsibleState {
  None = 0,
  Collapsed = 1,
  Expanded = 2,
}

export class TreeItem {
  public label: string;
  public collapsibleState: TreeItemCollapsibleState;
  public description?: string;
  public iconPath?: unknown;
  public tooltip?: unknown;
  public contextValue?: string;
  public command?: unknown;
  public constructor(
    label: string,
    collapsibleState: TreeItemCollapsibleState,
  ) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
}

export class ThemeIcon {
  public constructor(public id: string) {}
}

export class ThemeColor {
  public constructor(public id: string) {}
}

export class MarkdownString {
  public isTrusted = false;
  public constructor(public value: string) {}
}

export enum StatusBarAlignment {
  Left = 1,
  Right = 2,
}

export enum ViewColumn {
  Active = -1,
  Beside = -2,
  One = 1,
  Two = 2,
  Three = 3,
}

const noop = (): void => {
  /* no-op */
};

export const window = {
  createOutputChannel: (_name: string) => ({
    appendLine: noop,
    append: noop,
    show: noop,
    hide: noop,
    clear: noop,
    dispose: noop,
    replace: noop,
    name: _name,
  }),
  createStatusBarItem: (_align?: StatusBarAlignment, _priority?: number) => ({
    text: '',
    tooltip: undefined as unknown,
    command: undefined as unknown,
    backgroundColor: undefined as unknown,
    show: noop,
    hide: noop,
    dispose: noop,
  }),
  createWebviewPanel: (_type: string, _title: string, _col: unknown, _opts?: unknown) => ({
    webview: { html: '', asWebviewUri: (x: unknown) => x, options: {} },
    title: _title,
    reveal: noop,
    dispose: noop,
    onDidDispose: () => ({ dispose: noop }),
  }),
  registerTreeDataProvider: (_id: string, _provider: unknown) => ({ dispose: noop }),
};

export const workspace = {
  getConfiguration: (_section?: string) => ({
    get: <T>(_key: string, def: T): T => def,
  }),
  onDidChangeConfiguration: (_listener: unknown) => ({ dispose: noop }),
};

export const commands = {
  registerCommand: (_cmd: string, _fn: unknown) => ({ dispose: noop }),
  executeCommand: async (..._args: unknown[]): Promise<void> => {
    /* no-op */
  },
};
