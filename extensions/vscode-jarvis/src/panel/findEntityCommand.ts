/**
 * Q2 Slice 7 — Unified search command.
 *
 * ``jarvisObservability.findEntity`` opens a QuickPick fanning
 * across the existing GET endpoints to locate any entity:
 *
 *   * ``/observability/tasks``     → Op IDs
 *   * ``/observability/sessions``  → Session IDs
 *   * ``/observability/worktrees`` → Graph IDs + Unit IDs
 *   * ``/policy/confidence``       → Proposal IDs
 *
 * Selecting an item dispatches via ``CrossPanelLinker`` to the
 * panel that owns that kind. Failures from any single endpoint
 * degrade to "skipped" — the QuickPick still shows results from
 * reachable endpoints.
 *
 * Authority discipline:
 *   * Pure read — no POST, no state mutation.
 *   * NEVER raises into VS Code's command surface; failures are
 *     logged + surfaced as inline QuickPick rows.
 */

import * as vscode from 'vscode';
import { ObservabilityClient } from '../api/client';
import { PolicyClient } from '../api/policyClient';
import {
  EntityKind, EntityRef, entityKindLabel,
} from '../api/entityTypes';
import { CrossPanelLinker } from './crossPanelLinker';


interface QuickPickEntity extends vscode.QuickPickItem {
  readonly entityRef: EntityRef;
}


/**
 * Fan out across the GET endpoints + populate a QuickPick.
 * Uses ``Promise.allSettled`` so a slow / failing endpoint
 * doesn't block the rest. NEVER raises.
 */
async function _gatherEntities(
  observability: ObservabilityClient,
  policy: PolicyClient,
  logger: (msg: string) => void,
): Promise<QuickPickEntity[]> {
  const entities: QuickPickEntity[] = [];

  const tasksP = observability.taskList().catch((exc) => {
    logger(`findEntity: tasks fetch failed: ${
      exc instanceof Error ? exc.message : String(exc)
    }`);
    return null;
  });
  const sessionsP = observability.sessionList({ limit: 100 }).catch((exc) => {
    logger(`findEntity: sessions fetch failed: ${
      exc instanceof Error ? exc.message : String(exc)
    }`);
    return null;
  });
  const worktreesP = observability.worktreesList().catch((exc) => {
    logger(`findEntity: worktrees fetch failed: ${
      exc instanceof Error ? exc.message : String(exc)
    }`);
    return null;
  });
  const policyP = policy.snapshot().catch((exc) => {
    logger(`findEntity: policy snapshot fetch failed: ${
      exc instanceof Error ? exc.message : String(exc)
    }`);
    return null;
  });

  const [tasks, sessions, worktrees, policySnapshot] = await Promise.all([
    tasksP, sessionsP, worktreesP, policyP,
  ]);

  // Op IDs
  if (tasks !== null) {
    for (const opId of tasks.op_ids) {
      entities.push(_mkItem('op_id', opId, 'Op'));
    }
  }
  // Session IDs
  if (sessions !== null) {
    for (const s of sessions.sessions) {
      const ok = s.ok_outcome === true ? '✓' :
                 s.ok_outcome === false ? '✗' : '';
      const flags: string[] = [];
      if (s.bookmarked === true) flags.push('★');
      if (s.has_replay === true) flags.push('⏵');
      const detail = [ok, flags.join(' ')].filter(Boolean).join(' ');
      entities.push(_mkItem(
        'session_id', String(s.session_id), 'Session', detail,
      ));
    }
  }
  // Graph + Unit IDs (from worktree topology)
  if (worktrees !== null) {
    for (const g of worktrees.topology.graphs) {
      entities.push(_mkItem(
        'graph_id', g.graph_id, 'Graph',
        `${g.phase} • op=${g.op_id}`,
      ));
      for (const node of g.nodes) {
        entities.push(_mkItem(
          'unit_id', node.unit_id, 'Unit',
          `${node.state} • repo=${node.repo}`,
          { graph_id: g.graph_id },
        ));
      }
    }
  }
  // Proposal IDs (confidence policy)
  if (policySnapshot !== null) {
    for (const p of policySnapshot.proposals.items) {
      entities.push(_mkItem(
        'proposal_id', p.proposal_id, 'Proposal',
        `${p.status} • ${p.kind}`,
      ));
    }
  }

  return entities;
}


function _mkItem(
  kind: EntityKind, id: string, label: string,
  detail: string = '',
  context?: Record<string, string>,
): QuickPickEntity {
  return {
    label: `$(symbol-key) ${id}`,
    description: `[${label}]`,
    detail,
    entityRef: { kind, id, context },
  };
}


export async function findEntityCommand(
  observability: ObservabilityClient,
  policy: PolicyClient,
  linker: CrossPanelLinker,
  logger: (msg: string) => void,
): Promise<void> {
  const qp = vscode.window.createQuickPick<QuickPickEntity>();
  qp.busy = true;
  qp.placeholder = 'Loading entities from agent…';
  qp.matchOnDescription = true;
  qp.matchOnDetail = true;
  qp.show();

  try {
    const entities = await _gatherEntities(
      observability, policy, logger,
    );
    qp.busy = false;
    if (entities.length === 0) {
      qp.placeholder = (
        'No entities found — agent may be unreachable or all ' +
        'panels disabled. Check JARVIS Output channel.'
      );
      return;
    }
    // Sort: kind alphabetical, then id alphabetical
    entities.sort((a, b) => {
      const kindCmp = (a.entityRef.kind).localeCompare(
        b.entityRef.kind,
      );
      if (kindCmp !== 0) return kindCmp;
      return a.entityRef.id.localeCompare(b.entityRef.id);
    });
    qp.items = entities;
    qp.placeholder = (
      `Found ${entities.length} entities — type to filter, ` +
      `enter to open`
    );

    // Resolve on selection
    await new Promise<void>((resolve) => {
      qp.onDidAccept(async () => {
        const selected = qp.selectedItems[0];
        qp.hide();
        if (selected !== undefined) {
          await linker.openEntity(selected.entityRef);
        }
        resolve();
      });
      qp.onDidHide(() => resolve());
    });
  } finally {
    qp.dispose();
  }
}


/**
 * Composite-aware kind label for QuickPick consumption — exposed
 * so command-palette + status-bar surfaces can render the same
 * "Op" / "Session" / etc. label.
 */
export function quickPickLabel(kind: EntityKind): string {
  return entityKindLabel(kind);
}
