/**
 * Tree data provider for the "JARVIS Ops" sidebar.
 *
 * Read-only view over the Slice 1 GET endpoints + Slice 2 SSE frames.
 * The provider is authority-free: nothing it exposes lets the user
 * mutate agent state. The only user-facing actions are refresh
 * (re-fetch from GET) and select (expand a subtree / open detail).
 *
 * Bounded LRU cache prevents unbounded memory growth when many ops
 * have been seen during a long session.
 */

import * as vscode from 'vscode';
import { ObservabilityClient } from '../api/client';
import {
  StreamEventFrame,
  TaskDetailResponse,
  TaskProjection,
  TaskState,
  isControlEvent,
  isTaskEvent,
} from '../api/types';

export type OpsTreeNode = OpNode | TaskNode;

export class OpNode extends vscode.TreeItem {
  public override contextValue = 'jarvisObservability.op';
  public constructor(
    public readonly opId: string,
    public readonly closed: boolean,
    public readonly taskCount: number,
  ) {
    super(opId, vscode.TreeItemCollapsibleState.Collapsed);
    this.description = `${taskCount} task${taskCount === 1 ? '' : 's'}${
      closed ? ' (closed)' : ''
    }`;
    this.iconPath = new vscode.ThemeIcon(closed ? 'lock' : 'pulse');
    this.command = {
      command: 'jarvisObservability.showOp',
      title: 'Show Op Detail',
      arguments: [opId],
    };
  }
}

export class TaskNode extends vscode.TreeItem {
  public override contextValue = 'jarvisObservability.task';
  public constructor(
    public readonly opId: string,
    public readonly task: TaskProjection,
  ) {
    super(task.title || task.task_id, vscode.TreeItemCollapsibleState.None);
    this.description = task.state;
    this.iconPath = new vscode.ThemeIcon(iconForState(task.state));
    this.tooltip = new vscode.MarkdownString(
      `**${task.task_id}** — \`${task.state}\`\n\n${escapeMarkdown(task.body || '')}`,
    );
  }
}

function iconForState(state: TaskState): string {
  switch (state) {
    case 'pending':
      return 'circle-outline';
    case 'in_progress':
      return 'loading~spin';
    case 'completed':
      return 'pass';
    case 'cancelled':
      return 'circle-slash';
    default:
      return 'question';
  }
}

function escapeMarkdown(s: string): string {
  return s.replace(/([\\`*_{}[\]()#+\-.!])/g, '\\$1');
}

// --- Bounded LRU cache of op detail -----------------------------------------

class LruCache<K, V> {
  private readonly map = new Map<K, V>();
  public constructor(private readonly max: number) {}
  public get(key: K): V | undefined {
    const v = this.map.get(key);
    if (v !== undefined) {
      this.map.delete(key);
      this.map.set(key, v);
    }
    return v;
  }
  public set(key: K, value: V): void {
    if (this.map.has(key)) {
      this.map.delete(key);
    } else if (this.map.size >= this.max) {
      const oldest = this.map.keys().next().value;
      if (oldest !== undefined) {
        this.map.delete(oldest);
      }
    }
    this.map.set(key, value);
  }
  public delete(key: K): boolean {
    return this.map.delete(key);
  }
  public keys(): K[] {
    return Array.from(this.map.keys());
  }
  public get size(): number {
    return this.map.size;
  }
}

// --- Provider ---------------------------------------------------------------

export interface OpsTreeProviderOptions {
  readonly client: () => ObservabilityClient;
  readonly maxOpsCached: number;
  readonly logger?: (msg: string) => void;
}

export class OpsTreeProvider
  implements vscode.TreeDataProvider<OpsTreeNode>
{
  private readonly _onDidChange = new vscode.EventEmitter<OpsTreeNode | void>();
  public readonly onDidChangeTreeData: vscode.Event<OpsTreeNode | void> =
    this._onDidChange.event;

  private opIds: string[] = [];
  private readonly detailCache: LruCache<string, TaskDetailResponse>;
  private refreshInFlight: Promise<void> | null = null;
  private readonly opts: OpsTreeProviderOptions;

  public constructor(opts: OpsTreeProviderOptions) {
    this.opts = opts;
    this.detailCache = new LruCache(opts.maxOpsCached);
  }

  public getTreeItem(element: OpsTreeNode): vscode.TreeItem {
    return element;
  }

  public async getChildren(element?: OpsTreeNode): Promise<OpsTreeNode[]> {
    if (element === undefined) {
      return this.opIds.map((id) => {
        const detail = this.detailCache.get(id);
        return new OpNode(
          id,
          detail?.closed ?? false,
          detail?.board_size ?? 0,
        );
      });
    }
    if (element instanceof OpNode) {
      const detail = this.detailCache.get(element.opId)
        ?? (await this.loadDetail(element.opId));
      if (detail === null) {
        return [];
      }
      return detail.tasks.map((t) => new TaskNode(element.opId, t));
    }
    return [];
  }

  /** Re-fetch the op list from the server. Coalesces concurrent calls. */
  public async refresh(): Promise<void> {
    if (this.refreshInFlight !== null) {
      return this.refreshInFlight;
    }
    const p = this.doRefresh().finally(() => {
      this.refreshInFlight = null;
    });
    this.refreshInFlight = p;
    return p;
  }

  private async doRefresh(): Promise<void> {
    try {
      const list = await this.opts.client().taskList();
      this.opIds = [...list.op_ids];
      // Drop cached details for evicted ops.
      const cachedIds = new Set(this.detailCache.keys());
      const liveIds = new Set(this.opIds);
      for (const cid of cachedIds) {
        if (!liveIds.has(cid)) {
          this.detailCache.delete(cid);
        }
      }
      this._onDidChange.fire();
    } catch (exc) {
      this.log(`refresh failed: ${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }

  private async loadDetail(opId: string): Promise<TaskDetailResponse | null> {
    try {
      const detail = await this.opts.client().taskDetail(opId);
      this.detailCache.set(opId, detail);
      return detail;
    } catch (exc) {
      this.log(`taskDetail(${opId}) failed: ${exc instanceof Error ? exc.message : String(exc)}`);
      return null;
    }
  }

  /**
   * Apply a stream event to local cache + fire a scoped tree refresh.
   * stream_lag frames trigger a hard refresh from GET endpoints.
   */
  public async applyStreamEvent(frame: StreamEventFrame): Promise<void> {
    if (isControlEvent(frame)) {
      if (frame.event_type === 'stream_lag') {
        this.log('[stream_lag] hard-refresh from GET endpoints');
        await this.refresh();
      }
      // heartbeat / replay_start / replay_end — no tree change.
      return;
    }
    if (isTaskEvent(frame)) {
      // Invalidate cached detail for this op; listener on getChildren
      // will re-fetch.
      this.detailCache.delete(frame.op_id);
      if (!this.opIds.includes(frame.op_id)) {
        this.opIds = [...this.opIds, frame.op_id];
      }
      this._onDidChange.fire();
    }
  }

  /** Public test-only snapshot of cached state. */
  public snapshot(): { readonly opIds: readonly string[]; readonly cacheSize: number } {
    return { opIds: [...this.opIds], cacheSize: this.detailCache.size };
  }

  public dispose(): void {
    this._onDidChange.dispose();
  }

  private log(msg: string): void {
    if (this.opts.logger !== undefined) {
      this.opts.logger(msg);
    }
  }
}
