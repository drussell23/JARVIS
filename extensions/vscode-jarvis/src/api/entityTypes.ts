/**
 * Q2 Slice 7 — Entity-kind closed taxonomy + cross-panel
 * correlation primitive.
 *
 * Each panel deals in a different entity kind:
 *
 *   * ``op_id``       — OpDetailPanel scope
 *   * ``session_id``  — TemporalSliderPanel scope (filters DAG)
 *   * ``record_id``   — TemporalSliderPanel scope (anchors timeline)
 *   * ``graph_id``    — WorktreeTopologyPanel scope (highlights card)
 *   * ``unit_id``     — WorktreeTopologyPanel scope (highlights node)
 *   * ``proposal_id`` — ConfidencePolicyPanel scope (highlights row)
 *
 * The CrossPanelLinker resolves ``(kind, id)`` → panel command
 * dispatch. Each kind has its own validator regex mirrored from
 * the agent-side substrate so malformed IDs fail fast client-
 * side without round-tripping.
 *
 * Authority discipline:
 *   * Pure types + pure validators. No vscode imports — usable
 *     from any context (renderers, panels, tests).
 *   * NEVER raises; validators return ``boolean``.
 *   * Closed taxonomy so consumers handling ``EntityKind`` are
 *     exhaustive at compile time (TypeScript narrows ``never``
 *     in the default branch of a switch).
 */

export type EntityKind =
  | 'op_id'
  | 'session_id'
  | 'record_id'
  | 'graph_id'
  | 'unit_id'
  | 'proposal_id';

export const ALL_ENTITY_KINDS: ReadonlyArray<EntityKind> = [
  'op_id', 'session_id', 'record_id',
  'graph_id', 'unit_id', 'proposal_id',
];

/**
 * A typed reference to one entity. The renderer / panel code
 * that surfaces an ID should always wrap it in an EntityRef so
 * the link can be re-resolved to a panel command without
 * re-parsing the wire shape.
 */
export interface EntityRef {
  readonly kind: EntityKind;
  readonly id: string;
  /** Optional context (e.g., the session_id when kind is record_id). */
  readonly context?: Record<string, string>;
}

// --- Validator regexes (mirror agent-side substrate) ---------------------

// op_id: agent-side _OP_ID_RE accepts [A-Za-z0-9_-]{1,128}
const OP_ID_RE = /^[A-Za-z0-9_\-]{1,128}$/;

// session_id / graph_id: _SESSION_ID_RE accepts colons + dots too
// (timestamp formats and barrier-derived ids).
const WIDE_ID_RE = /^[A-Za-z0-9_\-:.]{1,128}$/;

// record_id: agent-side _RECORD_ID_RE is the wider 256-char form
// (phase-capture composite ids include phase + ordinal segments).
const RECORD_ID_RE = /^[A-Za-z0-9_\-:.]{1,256}$/;

// unit_id: scheduler convention is alphanumeric + dash + underscore.
const UNIT_ID_RE = /^[A-Za-z0-9_\-]{1,128}$/;

// proposal_id: ide_policy_router's _PROPOSAL_ID_RE matches the wide
// form (allows colons + dots so prefixes like ``conf-:proposal_id``
// stay valid).
const PROPOSAL_ID_RE = /^[A-Za-z0-9_\-:.]{1,128}$/;

const VALIDATORS: Record<EntityKind, RegExp> = {
  op_id: OP_ID_RE,
  session_id: WIDE_ID_RE,
  record_id: RECORD_ID_RE,
  graph_id: WIDE_ID_RE,
  unit_id: UNIT_ID_RE,
  proposal_id: PROPOSAL_ID_RE,
};

/** Returns true iff ``id`` is a valid identifier for ``kind``. */
export function isValidEntityId(
  kind: EntityKind, id: string,
): boolean {
  if (typeof id !== 'string') return false;
  const re = VALIDATORS[kind];
  if (re === undefined) return false;
  return re.test(id);
}

/** Construct an EntityRef with validation. Returns null on invalid. */
export function entityRef(
  kind: EntityKind, id: string,
  context?: Record<string, string>,
): EntityRef | null {
  if (!isValidEntityId(kind, id)) return null;
  return { kind, id, context };
}

/**
 * Heuristic kind inference from a bare ID string. Used by the
 * unified-search QuickPick when fanning across multiple GET
 * endpoints — each endpoint returns IDs of a known kind so the
 * caller can stamp the kind explicitly. The heuristic here is a
 * fallback for free-text input.
 *
 * Returns the FIRST kind whose validator accepts the id, in a
 * specificity-preferred order (op_id is the narrowest character
 * class and gets first try). Returns ``null`` when no validator
 * matches.
 */
export function inferEntityKind(id: string): EntityKind | null {
  if (typeof id !== 'string' || id === '') return null;
  // Order by specificity: narrowest character class first
  const ordered: ReadonlyArray<EntityKind> = [
    'op_id', 'unit_id', 'session_id', 'graph_id',
    'proposal_id', 'record_id',
  ];
  for (const kind of ordered) {
    if (isValidEntityId(kind, id)) return kind;
  }
  return null;
}

/**
 * Human-readable label for an entity kind — used in QuickPick
 * categories and panel-link badges.
 */
export function entityKindLabel(kind: EntityKind): string {
  switch (kind) {
    case 'op_id':       return 'Op';
    case 'session_id':  return 'Session';
    case 'record_id':   return 'Record';
    case 'graph_id':    return 'Graph';
    case 'unit_id':     return 'Unit';
    case 'proposal_id': return 'Proposal';
    default: {
      // Compile-time exhaustiveness check
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}

/**
 * VS Code command id for the panel that handles an entity kind.
 * Returns ``null`` when no panel handles this kind (defensive —
 * the closed taxonomy means every kind is mapped).
 */
export function entityCommandId(kind: EntityKind): string | null {
  switch (kind) {
    case 'op_id':       return 'jarvisObservability.showOp';
    case 'session_id':  return 'jarvisObservability.openTemporalSlider';
    case 'record_id':   return 'jarvisObservability.openTemporalSlider';
    case 'graph_id':    return 'jarvisObservability.openWorktreeTopology';
    case 'unit_id':     return 'jarvisObservability.openWorktreeTopology';
    case 'proposal_id': return 'jarvisObservability.openConfidencePolicy';
    default: {
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}
