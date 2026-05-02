/**
 * Q2 Slice 7 — Cross-panel correlation dispatcher.
 *
 * Single chokepoint that resolves ``(EntityKind, id)`` pairs to
 * panel commands. Used by:
 *
 *   * Renderer-side "open in X" links (every panel renderer
 *     emits ``{type: 'open_entity', kind: '<kind>', id: '<id>'}``
 *     messages → the panel's message handler dispatches via this
 *     linker).
 *   * Unified search (``jarvisObservability.findEntity`` quickpick).
 *
 * Authority discipline:
 *   * NEVER raises. Bad IDs fail validation upfront; missing
 *     panel handlers degrade to a warning notification.
 *   * No state — pure dispatcher. The linker holds references
 *     to the panels but never persists state.
 *   * Panels expose ``revealEntity(kind, id, context?)`` methods
 *     for kind-specific scoping (e.g., session_id scopes the
 *     temporal slider to that session). When a panel doesn't
 *     implement reveal for a given kind, the linker falls back
 *     to opening the panel without scoping.
 */

import * as vscode from 'vscode';
import {
  EntityKind, EntityRef, entityCommandId, entityKindLabel,
  isValidEntityId,
} from '../api/entityTypes';
import { ConfidencePolicyPanel } from './confidencePolicyPanel';
import { OpDetailPanel } from './opDetailPanel';
import { TemporalSliderPanel } from './temporalSliderPanel';
import { WorktreeTopologyPanel } from './worktreeTopologyPanel';

/**
 * Panels the linker can scope to. All optional — operators may
 * deploy without certain panels (e.g., a read-only build that
 * skips ConfidencePolicy).
 */
export interface PanelRefs {
  readonly opDetail?: OpDetailPanel;
  readonly temporalSlider?: TemporalSliderPanel;
  readonly worktreeTopology?: WorktreeTopologyPanel;
  readonly confidencePolicy?: ConfidencePolicyPanel;
}

export class CrossPanelLinker {
  public constructor(
    private readonly panels: PanelRefs,
    private readonly logger: (msg: string) => void,
  ) {}

  /**
   * Dispatch an entity reference to the correct panel.
   *
   * Returns ``true`` iff a panel was reached. Returns ``false``
   * when:
   *   * The id fails validation for the kind
   *   * No panel is available for that kind in this build
   *   * The dispatch itself raised (caught + logged)
   *
   * NEVER raises into the caller. Surfaces a warning toast on
   * validation failure or missing panel.
   */
  public async openEntity(ref: EntityRef): Promise<boolean> {
    if (!isValidEntityId(ref.kind, ref.id)) {
      this.logger(
        `crossPanelLinker: malformed ${ref.kind} id: ${ref.id}`,
      );
      vscode.window.showWarningMessage(
        `JARVIS: malformed ${entityKindLabel(ref.kind)} id ` +
        `"${ref.id}" — refusing to dispatch`,
      );
      return false;
    }
    try {
      switch (ref.kind) {
        case 'op_id':
          return await this._openOp(ref.id);
        case 'session_id':
          return await this._openSession(ref.id);
        case 'record_id':
          return await this._openRecord(ref);
        case 'graph_id':
          return await this._openGraph(ref.id);
        case 'unit_id':
          return await this._openUnit(ref);
        case 'proposal_id':
          return await this._openProposal(ref.id);
        default: {
          const _exhaustive: never = ref.kind;
          return _exhaustive;
        }
      }
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      this.logger(
        `crossPanelLinker.openEntity(${ref.kind}=${ref.id}) ` +
        `raised: ${msg}`,
      );
      return false;
    }
  }

  private async _openOp(opId: string): Promise<boolean> {
    if (this.panels.opDetail !== undefined) {
      await this.panels.opDetail.show(opId);
      return true;
    }
    // Fallback to the existing command (decoupled path)
    return await this._invokeCommand('op_id', opId);
  }

  private async _openSession(sessionId: string): Promise<boolean> {
    if (this.panels.temporalSlider !== undefined) {
      await this.panels.temporalSlider.revealEntity({
        kind: 'session_id', id: sessionId,
      });
      return true;
    }
    return await this._invokeCommand('session_id', sessionId);
  }

  private async _openRecord(ref: EntityRef): Promise<boolean> {
    // record_id needs a session_id context to be resolvable —
    // fail fast if missing.
    const sessionId = ref.context?.session_id;
    if (sessionId === undefined || sessionId === '') {
      this.logger(
        `crossPanelLinker: record_id ${ref.id} requires ` +
        `session_id context; cannot reveal`,
      );
      vscode.window.showWarningMessage(
        `JARVIS: cannot open Record ${ref.id} without a session context`,
      );
      return false;
    }
    if (this.panels.temporalSlider !== undefined) {
      await this.panels.temporalSlider.revealEntity({
        kind: 'record_id', id: ref.id,
        context: { session_id: sessionId },
      });
      return true;
    }
    return await this._invokeCommand('record_id', ref.id);
  }

  private async _openGraph(graphId: string): Promise<boolean> {
    if (this.panels.worktreeTopology !== undefined) {
      await this.panels.worktreeTopology.revealEntity({
        kind: 'graph_id', id: graphId,
      });
      return true;
    }
    return await this._invokeCommand('graph_id', graphId);
  }

  private async _openUnit(ref: EntityRef): Promise<boolean> {
    if (this.panels.worktreeTopology !== undefined) {
      await this.panels.worktreeTopology.revealEntity({
        kind: 'unit_id', id: ref.id,
        context: ref.context ?? {},
      });
      return true;
    }
    return await this._invokeCommand('unit_id', ref.id);
  }

  private async _openProposal(proposalId: string): Promise<boolean> {
    if (this.panels.confidencePolicy !== undefined) {
      await this.panels.confidencePolicy.revealEntity({
        kind: 'proposal_id', id: proposalId,
      });
      return true;
    }
    return await this._invokeCommand('proposal_id', proposalId);
  }

  /**
   * Decoupled fallback — dispatches via the command-palette
   * command id rather than the direct panel reference. Used
   * when the panel ref isn't injected (e.g., test contexts).
   */
  private async _invokeCommand(
    kind: EntityKind, id: string,
  ): Promise<boolean> {
    const commandId = entityCommandId(kind);
    if (commandId === null) {
      this.logger(
        `crossPanelLinker: no command for kind=${kind}`,
      );
      return false;
    }
    try {
      // showOp expects op_id; other commands take no args
      // (they open the bare panel + the operator scopes manually).
      if (kind === 'op_id') {
        await vscode.commands.executeCommand(commandId, id);
      } else {
        await vscode.commands.executeCommand(commandId);
      }
      return true;
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      this.logger(
        `crossPanelLinker: command ${commandId} raised: ${msg}`,
      );
      return false;
    }
  }
}
