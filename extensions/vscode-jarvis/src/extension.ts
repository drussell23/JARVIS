/**
 * Extension entry point. Wires the Tree View + SSE stream +
 * Op-detail webview + status bar + command palette.
 *
 * Everything here is async-safe: config changes tear down the stream
 * cleanly, re-instantiate with new settings, and re-announce to the
 * status bar. No fire-and-forget promises — all loops are owned by
 * disposables registered with the extension context.
 *
 * Authority invariant: this extension NEVER POSTs to the agent.
 * Every network call is either GET (Slice 1) or a long-polling
 * GET (SSE). The agent-side observability surface already refuses
 * non-GET on the observability routes; this client respects the
 * read-only posture on its own side too.
 */

import * as vscode from 'vscode';
import { ObservabilityClient } from './api/client';
import { PolicyClient } from './api/policyClient';
import { StreamConsumer, StreamState } from './api/stream';
import { StreamEventFrame } from './api/types';
import { ExtensionConfig, onConfigChange, readConfig } from './config';
import { Logger } from './logger';
import { ConfidencePolicyPanel } from './panel/confidencePolicyPanel';
import { OpDetailPanel } from './panel/opDetailPanel';
import { TemporalSliderPanel } from './panel/temporalSliderPanel';
import { WorktreeTopologyPanel } from './panel/worktreeTopologyPanel';
import { StatusBar } from './status/statusBar';
import { OpsTreeProvider } from './tree/opsProvider';

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const logger = new Logger('JARVIS Observability');
  context.subscriptions.push(logger);
  logger.info('activate — JARVIS Observability extension 0.1.0');

  let config = readConfig();
  const state: ActiveState = {
    client: () => buildClient(config, abortController.signal),
    stream: null,
    treeProvider: null,
    opDetailPanel: null,
    confidencePolicyPanel: null,
    worktreeTopologyPanel: null,
    temporalSliderPanel: null,
    pollTimer: null,
  };

  const abortController = new AbortController();

  const statusBar = new StatusBar();
  statusBar.setEndpoint(config.endpoint);
  context.subscriptions.push(statusBar);

  const treeProvider = new OpsTreeProvider({
    client: state.client,
    maxOpsCached: config.maxOpsCached,
    logger: (m) => logger.info(m),
  });
  state.treeProvider = treeProvider;
  context.subscriptions.push(treeProvider);
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider(
      'jarvisObservability.ops',
      treeProvider,
    ),
  );

  const opDetailPanel = new OpDetailPanel(state.client);
  state.opDetailPanel = opDetailPanel;
  context.subscriptions.push({ dispose: () => opDetailPanel.dispose() });

  // Gap #2 Slice 5b: interactive Confidence Policy panel.
  // Sibling to OpDetailPanel — uses a separate write client + its
  // own message-passing surface so the read panel stays HTML-only.
  const confidencePolicyPanel = new ConfidencePolicyPanel(
    () => buildPolicyClient(config, abortController.signal),
    (m) => logger.info(m),
  );
  state.confidencePolicyPanel = confidencePolicyPanel;
  context.subscriptions.push({
    dispose: () => confidencePolicyPanel.dispose(),
  });

  // Gap #3 Slice 4: read-only Worktree Topology panel.
  // Reuses the existing ObservabilityClient (same "1.0" schema)
  // for snapshot fetches; renders a force-directed SVG graph
  // entirely client-side (no library, no bundler, no supply-chain
  // surface).
  const worktreeTopologyPanel = new WorktreeTopologyPanel(
    state.client,
    (m) => logger.info(m),
  );
  state.worktreeTopologyPanel = worktreeTopologyPanel;
  context.subscriptions.push({
    dispose: () => worktreeTopologyPanel.dispose(),
  });

  // Gap #1 Slice 2: read-only Temporal Slider panel for
  // time-travel debugging across the CausalityDAG. Reuses the
  // existing ObservabilityClient (same "1.0" schema) for sessions
  // / DAG / replay surface fetches; SSE-driven refresh on
  // replay_start/end so the verdicts ribbon stays current.
  const temporalSliderPanel = new TemporalSliderPanel(
    state.client,
    (m) => logger.info(m),
  );
  state.temporalSliderPanel = temporalSliderPanel;
  context.subscriptions.push({
    dispose: () => temporalSliderPanel.dispose(),
  });

  // --- Commands ----------------------------------------------------------
  context.subscriptions.push(
    vscode.commands.registerCommand('jarvisObservability.connect', () => {
      connect();
    }),
    vscode.commands.registerCommand('jarvisObservability.disconnect', async () => {
      await disconnect();
    }),
    vscode.commands.registerCommand('jarvisObservability.refresh', async () => {
      await treeProvider.refresh();
    }),
    vscode.commands.registerCommand(
      'jarvisObservability.showOp',
      async (opId: string) => {
        if (typeof opId !== 'string' || opId === '') {
          return;
        }
        await opDetailPanel.show(opId);
      },
    ),
    vscode.commands.registerCommand('jarvisObservability.showLog', () => {
      logger.show();
    }),
    // Gap #2 Slice 5b: open the Confidence Policy panel.
    vscode.commands.registerCommand(
      'jarvisObservability.openConfidencePolicy',
      async () => {
        await confidencePolicyPanel.show();
      },
    ),
    // Gap #3 Slice 4: open the Worktree Topology panel.
    vscode.commands.registerCommand(
      'jarvisObservability.openWorktreeTopology',
      async () => {
        await worktreeTopologyPanel.show();
      },
    ),
    // Gap #1 Slice 2: open the Temporal Slider panel.
    vscode.commands.registerCommand(
      'jarvisObservability.openTemporalSlider',
      async () => {
        await temporalSliderPanel.show();
      },
    ),
  );

  // --- Config reactivity -------------------------------------------------
  context.subscriptions.push(
    onConfigChange(async (newCfg) => {
      const restartStream = shouldRestartStream(config, newCfg);
      config = newCfg;
      statusBar.setEndpoint(config.endpoint);
      if (restartStream && state.stream !== null) {
        logger.info('config changed — restarting stream');
        await disconnect();
        if (config.enabled) {
          connect();
        }
      } else if (!config.enabled && state.stream !== null) {
        await disconnect();
      } else if (config.enabled && state.stream === null) {
        connect();
      }
    }),
  );

  // --- Bootstrap ---------------------------------------------------------
  if (config.enabled) {
    connect();
    // Always do an initial refresh so the tree populates even if the
    // stream doesn't connect immediately.
    treeProvider.refresh().catch((exc) => logger.error('initial refresh', exc));
  }

  context.subscriptions.push({
    dispose: async () => {
      abortController.abort();
      await disconnect();
    },
  });

  // --- Connection helpers ------------------------------------------------

  function connect(): void {
    if (state.stream !== null && state.stream.isRunning()) {
      return;
    }
    const consumer = new StreamConsumer({
      endpoint: config.endpoint,
      opIdFilter: config.opIdFilter !== '' ? config.opIdFilter : undefined,
      autoReconnect: config.autoReconnect,
      reconnectMaxBackoffMs: config.reconnectMaxBackoffMs,
      logger: (m) => logger.info(m),
    });
    consumer.onState((s: StreamState) => {
      statusBar.setState(s);
      vscode.commands.executeCommand(
        'setContext',
        'jarvisObservability.connected',
        s === 'connected' || s === 'reconnecting' || s === 'connecting',
      );
    });
    consumer.onEvent(async (frame: StreamEventFrame) => {
      try {
        await treeProvider.applyStreamEvent(frame);
        // Refresh the open op-detail panel when its op gets an event.
        if (opDetailPanel.isShowing(frame.op_id)) {
          await opDetailPanel.refresh();
        }
        // Slice 5b: SSE-driven refresh for the policy panel.
        // The panel itself filters non-confidence_policy_* events.
        if (confidencePolicyPanel.isOpen()) {
          await confidencePolicyPanel.onStreamEvent(frame);
        }
        // Gap #3 Slice 4: SSE-driven refresh for the worktree
        // topology panel. The panel filters non-worktree_* events.
        if (worktreeTopologyPanel.isOpen()) {
          await worktreeTopologyPanel.onStreamEvent(frame);
        }
        // Gap #1 Slice 2: SSE-driven refresh for the temporal
        // slider panel on replay_start / replay_end. The panel
        // filters other event types.
        if (temporalSliderPanel.isOpen()) {
          await temporalSliderPanel.onStreamEvent(frame);
        }
      } catch (exc) {
        logger.error(`applyStreamEvent(${frame.event_type})`, exc);
      }
    });
    state.stream = consumer;
    consumer.start();
    startPollingFallback();
  }

  async function disconnect(): Promise<void> {
    stopPollingFallback();
    const consumer = state.stream;
    state.stream = null;
    if (consumer !== null) {
      await consumer.stop();
    }
    statusBar.setState('disconnected');
    await vscode.commands.executeCommand(
      'setContext',
      'jarvisObservability.connected',
      false,
    );
  }

  function startPollingFallback(): void {
    stopPollingFallback();
    state.pollTimer = setInterval(() => {
      const s = statusBar.getState();
      if (s === 'error' || s === 'reconnecting' || s === 'disconnected') {
        treeProvider
          .refresh()
          .catch((exc) => logger.error('poll-fallback refresh', exc));
      }
    }, config.pollIntervalMs);
  }

  function stopPollingFallback(): void {
    if (state.pollTimer !== null) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }
}

export function deactivate(): void {
  // No-op — disposables registered on ctx.subscriptions handle cleanup.
}

// --- Helpers ---------------------------------------------------------------

interface ActiveState {
  readonly client: () => ObservabilityClient;
  stream: StreamConsumer | null;
  treeProvider: OpsTreeProvider | null;
  opDetailPanel: OpDetailPanel | null;
  confidencePolicyPanel: ConfidencePolicyPanel | null;
  worktreeTopologyPanel: WorktreeTopologyPanel | null;
  temporalSliderPanel: TemporalSliderPanel | null;
  pollTimer: NodeJS.Timeout | null;
}

function buildClient(cfg: ExtensionConfig, signal: AbortSignal): ObservabilityClient {
  return new ObservabilityClient({
    endpoint: cfg.endpoint,
    signal,
  });
}

function buildPolicyClient(
  cfg: ExtensionConfig, signal: AbortSignal,
): PolicyClient {
  return new PolicyClient({ endpoint: cfg.endpoint, signal });
}

function shouldRestartStream(a: ExtensionConfig, b: ExtensionConfig): boolean {
  return (
    a.endpoint !== b.endpoint ||
    a.opIdFilter !== b.opIdFilter ||
    a.autoReconnect !== b.autoReconnect ||
    a.reconnectMaxBackoffMs !== b.reconnectMaxBackoffMs
  );
}
