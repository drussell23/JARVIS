/**
 * Q2 Slice 7 — Cross-panel correlation + unified search tests.
 *
 * Covers:
 *   * EntityKind closed taxonomy
 *   * Validators (op_id / session_id / record_id / graph_id /
 *     unit_id / proposal_id) — accept/reject matrix
 *   * inferEntityKind specificity ordering
 *   * entityKindLabel exhaustiveness
 *   * entityCommandId mapping
 *   * entityRef constructor — null on invalid id
 *
 * The CrossPanelLinker + findEntityCommand are skipped here
 * because they require vscode runtime (panel refs + QuickPick);
 * those wire through the existing manual + integration testing
 * channels.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ALL_ENTITY_KINDS,
  EntityKind,
  entityCommandId,
  entityKindLabel,
  entityRef,
  inferEntityKind,
  isValidEntityId,
} from '../src/api/entityTypes';


// --- Closed taxonomy -----------------------------------------------------

test('ALL_ENTITY_KINDS covers six kinds', () => {
  assert.equal(ALL_ENTITY_KINDS.length, 6);
  const expected: ReadonlyArray<EntityKind> = [
    'op_id', 'session_id', 'record_id',
    'graph_id', 'unit_id', 'proposal_id',
  ];
  for (const kind of expected) {
    assert.ok(
      ALL_ENTITY_KINDS.includes(kind),
      `${kind} missing from ALL_ENTITY_KINDS`,
    );
  }
});


// --- Validators ----------------------------------------------------------

test('isValidEntityId: op_id accepts alphanumeric + underscore + hyphen', () => {
  assert.equal(isValidEntityId('op_id', 'op-123'), true);
  assert.equal(isValidEntityId('op_id', 'op_xyz'), true);
  assert.equal(isValidEntityId('op_id', 'OP-MIXED-Case-123'), true);
});

test('isValidEntityId: op_id rejects colons + dots + spaces', () => {
  assert.equal(isValidEntityId('op_id', 'op:1.2'), false);
  assert.equal(isValidEntityId('op_id', 'op 1'), false);
  assert.equal(isValidEntityId('op_id', 'op/x'), false);
});

test('isValidEntityId: op_id rejects empty + over-128-chars', () => {
  assert.equal(isValidEntityId('op_id', ''), false);
  assert.equal(isValidEntityId('op_id', 'a'.repeat(129)), false);
  assert.equal(isValidEntityId('op_id', 'a'.repeat(128)), true);
});

test('isValidEntityId: session_id + graph_id accept colons + dots', () => {
  // bt-2026-05-02-100000:abc.def — timestamp + barrier-derived
  assert.equal(
    isValidEntityId('session_id', 'bt-2026-05-02-100000:abc.def'),
    true,
  );
  assert.equal(
    isValidEntityId('graph_id', 'g1:phase-a:001'),
    true,
  );
});

test('isValidEntityId: record_id accepts wider 256-char form', () => {
  // Phase-capture composite ids can be long
  const longRec = 'r-' + 'a'.repeat(200);
  assert.equal(isValidEntityId('record_id', longRec), true);
  assert.equal(
    isValidEntityId('record_id', 'a'.repeat(256)),
    true,
  );
  assert.equal(
    isValidEntityId('record_id', 'a'.repeat(257)),
    false,
  );
});

test('isValidEntityId: rejects non-string', () => {
  assert.equal(isValidEntityId('op_id', 42 as never), false);
  assert.equal(isValidEntityId('op_id', null as never), false);
  assert.equal(isValidEntityId('op_id', undefined as never), false);
});


// --- inferEntityKind -----------------------------------------------------

test('inferEntityKind: narrow op_id wins for plain alphanumeric', () => {
  // "op-123" matches op_id (narrowest) and session_id (wider) —
  // specificity ordering picks op_id.
  assert.equal(inferEntityKind('op-123'), 'op_id');
});

test('inferEntityKind: id with colons → session_id (op_id rejects)', () => {
  assert.equal(
    inferEntityKind('bt-2026-05-02-100000:abc'),
    'session_id',
  );
});

test('inferEntityKind: id over 128-but-under-256 chars → record_id', () => {
  const longId = 'r-' + 'a'.repeat(200);
  // op_id: <= 128, rejects. session_id: <= 128, rejects.
  // record_id: <= 256, accepts.
  assert.equal(inferEntityKind(longId), 'record_id');
});

test('inferEntityKind: empty string → null', () => {
  assert.equal(inferEntityKind(''), null);
});

test('inferEntityKind: non-string → null', () => {
  assert.equal(inferEntityKind(42 as never), null);
});

test('inferEntityKind: id with forbidden chars → null', () => {
  // "op@1" has @ which no validator accepts
  assert.equal(inferEntityKind('op@1'), null);
});


// --- entityKindLabel + entityCommandId -----------------------------------

test('entityKindLabel covers every kind', () => {
  for (const kind of ALL_ENTITY_KINDS) {
    const label = entityKindLabel(kind);
    assert.ok(label.length > 0, `${kind} has empty label`);
  }
});

test('entityKindLabel canonical strings', () => {
  assert.equal(entityKindLabel('op_id'), 'Op');
  assert.equal(entityKindLabel('session_id'), 'Session');
  assert.equal(entityKindLabel('record_id'), 'Record');
  assert.equal(entityKindLabel('graph_id'), 'Graph');
  assert.equal(entityKindLabel('unit_id'), 'Unit');
  assert.equal(entityKindLabel('proposal_id'), 'Proposal');
});

test('entityCommandId maps every kind to a real command', () => {
  for (const kind of ALL_ENTITY_KINDS) {
    const cmd = entityCommandId(kind);
    assert.ok(
      cmd !== null && cmd.startsWith('jarvisObservability.'),
      `${kind} → ${cmd} not a JARVIS command`,
    );
  }
});

test('entityCommandId routes by ownership', () => {
  // op_id → showOp (existing OpDetailPanel)
  assert.equal(entityCommandId('op_id'), 'jarvisObservability.showOp');
  // session_id + record_id → temporal slider
  assert.equal(
    entityCommandId('session_id'),
    'jarvisObservability.openTemporalSlider',
  );
  assert.equal(
    entityCommandId('record_id'),
    'jarvisObservability.openTemporalSlider',
  );
  // graph_id + unit_id → worktree topology
  assert.equal(
    entityCommandId('graph_id'),
    'jarvisObservability.openWorktreeTopology',
  );
  assert.equal(
    entityCommandId('unit_id'),
    'jarvisObservability.openWorktreeTopology',
  );
  // proposal_id → confidence policy
  assert.equal(
    entityCommandId('proposal_id'),
    'jarvisObservability.openConfidencePolicy',
  );
});


// --- entityRef constructor ----------------------------------------------

test('entityRef: returns null on invalid id', () => {
  assert.equal(entityRef('op_id', ''), null);
  assert.equal(entityRef('op_id', 'has space'), null);
  assert.equal(entityRef('op_id', 'a'.repeat(129)), null);
});

test('entityRef: returns shaped ref on valid id', () => {
  const ref = entityRef('op_id', 'op-123');
  assert.ok(ref !== null);
  assert.equal(ref!.kind, 'op_id');
  assert.equal(ref!.id, 'op-123');
});

test('entityRef: passes context through', () => {
  const ref = entityRef(
    'record_id', 'r-abc',
    { session_id: 'bt-2026-05-02:test' },
  );
  assert.ok(ref !== null);
  assert.equal(ref!.context?.session_id, 'bt-2026-05-02:test');
});

test('entityRef: omits context when undefined', () => {
  const ref = entityRef('op_id', 'op-1');
  assert.ok(ref !== null);
  assert.equal(ref!.context, undefined);
});
