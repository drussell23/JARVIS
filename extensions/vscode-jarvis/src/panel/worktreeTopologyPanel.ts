/**
 * Worktree Topology panel — read-only graph webview for the
 * Gap #3 Slice 4 L3 worktree topology view.
 *
 * Distinct from ``OpDetailPanel`` (pure HTML, no scripts):
 * the topology panel runs a small force-directed layout in the
 * webview script + an SSE-driven refresh hook, so it needs
 * ``enableScripts: true`` with strict-CSP-and-nonce discipline
 * (mirror of ``ConfidencePolicyPanel``).
 *
 * Authority discipline:
 *   * READ-ONLY — the panel never POSTs. The only inbound message
 *     types are ``refresh`` (operator-initiated) and ``open_op``
 *     (cross-link to the op-detail panel for a graph's op_id).
 *     No write surface; no policy decisions.
 *   * Strict CSP with nonce; no eval; no external script sources.
 *   * Webview script never makes network calls — every refresh
 *     routes through the extension via ``postMessage`` →
 *     ``ObservabilityClient.worktreesList``.
 *   * Webview script uses ``textContent`` + DOM construction
 *     ONLY for dynamic content. ``innerHTML`` is reserved for
 *     fully-static template strings. Even though topology data
 *     is server-trusted, this discipline removes XSS surface by
 *     construction (no taint flow from data to HTML).
 *
 * Layout strategy:
 *   * Vanilla SVG, no library. Force-directed simulation runs
 *     entirely in the webview script — no tree-shaking concerns,
 *     no bundler, no supply-chain surface.
 *   * Determinism is intentionally NOT a goal: a force-directed
 *     layout converges to *some* aesthetically-pleasing
 *     embedding; small node counts (~10-50 per graph) settle in
 *     <80 ticks.
 */

import * as vscode from 'vscode';
import { ObservabilityClient } from '../api/client';
import {
  StreamEventFrame,
  isWorktreeEvent,
} from '../api/types';
import { EntityRef } from '../api/entityTypes';
import {
  renderErrorHtml,
  renderHtml,
} from './worktreeTopologyRenderers';

interface MessageFromWebview {
  readonly type: 'refresh' | 'open_op';
  readonly payload?: unknown;
}

export class WorktreeTopologyPanel {
  private static readonly viewType =
    'jarvisObservability.worktreeTopology';

  private panel: vscode.WebviewPanel | null = null;
  private nonce: string = '';

  public constructor(
    private readonly getClient: () => ObservabilityClient,
    private readonly logger: (msg: string) => void,
  ) {}

  public async show(): Promise<void> {
    if (this.panel === null) {
      this.nonce = generateNonce();
      this.panel = vscode.window.createWebviewPanel(
        WorktreeTopologyPanel.viewType,
        'JARVIS Worktree Topology',
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
              `worktreeTopology message handler raised: ${
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
   * Q2 Slice 7 — cross-panel entity reveal. Refreshes the panel
   * and instructs the webview to scroll to the matching graph
   * card or unit node via a postMessage signal.
   *
   * Both ``graph_id`` and ``unit_id`` are accepted; for unit_id
   * the renderer will scroll to the graph card containing it
   * (substrate guarantees unit_id uniqueness within a graph but
   * not across graphs, so caller-supplied ``context.graph_id``
   * disambiguates when needed).
   *
   * NEVER raises. The webview's reveal handling is best-effort
   * (no-op when the entity isn't in the current snapshot).
   */
  public async revealEntity(ref: EntityRef): Promise<void> {
    await this.show();
    // The panel currently re-renders on every refresh; the
    // webview script can listen for a "reveal" message and
    // scrollIntoView the matching card. We send the post-render
    // ping after the refresh promise resolves.
    if (this.panel === null) return;
    const payload = {
      kind: ref.kind, id: ref.id,
      context: ref.context ?? {},
    };
    try {
      this.panel.webview.postMessage({
        type: 'reveal_entity', payload,
      });
    } catch (exc) {
      this.logger(
        `worktreeTopology.revealEntity postMessage failed: ${
          exc instanceof Error ? exc.message : String(exc)
        }`,
      );
    }
  }

  /**
   * Fetch + render the latest topology snapshot. Best-effort —
   * fetch errors render an inline banner rather than dropping
   * the panel.
   */
  public async refresh(): Promise<void> {
    if (this.panel === null) {
      return;
    }
    try {
      const response = await this.getClient().worktreesList();
      this.panel.webview.html = renderHtml(
        response.topology, this.nonce,
      );
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      this.panel.webview.html = renderErrorHtml(msg, this.nonce);
    }
  }

  /**
   * SSE event hook — when a worktree_* event arrives, refresh
   * the panel without polling. Idempotent: if the panel isn't
   * open or the event is unrelated, it's a no-op.
   */
  public async onStreamEvent(
    frame: StreamEventFrame,
  ): Promise<void> {
    if (this.panel === null) {
      return;
    }
    if (!isWorktreeEvent(frame)) {
      return;
    }
    await this.refresh();
  }

  private async handleMessage(msg: MessageFromWebview): Promise<void> {
    if (this.panel === null) {
      return;
    }
    switch (msg.type) {
      case 'refresh':
        await this.refresh();
        return;
      case 'open_op': {
        const payload = msg.payload as { op_id?: string } | undefined;
        const opId = payload?.op_id;
        if (typeof opId === 'string' && opId !== '') {
          await vscode.commands.executeCommand(
            'jarvisObservability.showOp', opId,
          );
        }
        return;
      }
      default:
        this.logger(
          `worktreeTopology: unknown message type=${
            (msg as { type?: string }).type ?? '(none)'
          }`,
        );
    }
  }
}



// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------


function generateNonce(): string {
  const crypto = require("crypto") as typeof import("crypto");
  return crypto.randomBytes(16).toString("hex");
}
