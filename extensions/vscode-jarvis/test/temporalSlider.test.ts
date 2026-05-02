/**
 * Gap #1 Slice 2 — TemporalSliderPanel renderer regression suite.
 *
 * Renderers are pure functions of (state, nonce) so testable
 * without booting VS Code (mirrors existing renderers.ts +
 * worktreeTopologyRenderers.ts pattern). Covers:
 *   * Empty / partial state UX
 *   * Session list rendering + selection markers
 *   * Timeline ticks rendering + phase classification
 *   * Record fields + parents + children + counterfactuals
 *   * Verdict ribbon rendering with closed-vocabulary classes
 *   * CSP nonce wiring + script-src nonce-only + </script> escape
 *   * XSS surface — every dynamic field escaped
 *
 * Plus type-guard tests for isReplayEvent.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  TemporalSliderState,
  renderHtml,
  renderErrorHtml,
  escapeHtml,
} from '../src/panel/temporalSliderRenderers';
import {
  StreamEventFrame,
  isReplayEvent,
} from '../src/api/types';

function emptyState(): TemporalSliderState {
  return {
    sessions: null,
    selectedSessionId: null,
    dag: null,
    selectedRecordIndex: -1,
    record: null,
    replayHealth: null,
    replayVerdicts: null,
    anchorRecordId: null,
    diff: null,
  };
}

const baseSession = {
  session_id: 'bt-2026-05-02-100000',
  ok_outcome: true,
  bookmarked: false,
  pinned: false,
  has_replay: true,
  parse_error: false,
};

const baseDag = {
  schema_version: '1.0',
  session_id: 'bt-2026-05-02-100000',
  node_count: 4,
  edge_count: 3,
  record_ids: [
    'bt-2026-05-02-100000:classify:001',
    'bt-2026-05-02-100000:route:002',
    'bt-2026-05-02-100000:plan:003',
    'bt-2026-05-02-100000:generate:004',
  ],
};

const baseRecord = {
  schema_version: '1.0',
  record_id: 'bt-2026-05-02-100000:plan:003',
  record: {
    phase: 'plan', op_id: 'op-x', ts_ns: 12345,
    detail: 'Planned 4 candidate changes',
  },
  parents: ['bt-2026-05-02-100000:route:002'],
  children: ['bt-2026-05-02-100000:generate:004'],
  counterfactual_branches: [],
  subgraph_node_count: 4,
};

// --- Renderer happy-path ------------------------------------------------

test('renderHtml: empty state shows session-empty message', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: { schema_version: '1.0', sessions: [], count: 0 },
  }, 'n');
  assert.match(html, /no sessions found/);
  assert.match(html, /no session selected/);
});

test('renderHtml: session list renders with selection marker', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0',
      sessions: [
        baseSession,
        { ...baseSession, session_id: 'bt-other-session', has_replay: false },
      ],
      count: 2,
    },
    selectedSessionId: baseSession.session_id,
  }, 'n');
  assert.match(html, /class="session-item selected" data-session-id="bt-2026-05-02-100000"/);
  assert.match(html, /data-session-id="bt-other-session"/);
  // ✓ marker for ok_outcome=true
  assert.match(html, />✓</);
});

test('renderHtml: timeline ticks rendered with per-phase classes', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: baseDag,
    selectedRecordIndex: 2,
  }, 'n');
  // Each record_id has the canonical sid:phase:seq shape so ticks
  // get phase-classify / phase-route / phase-plan / phase-generate
  assert.match(html, /class="tick.*phase-classify.*"/);
  assert.match(html, /class="tick.*phase-route.*"/);
  // Selected tick (idx=2 = plan) gets the .selected class
  assert.match(html, /class="tick selected phase-plan"/);
  assert.match(html, /data-record-index="0"/);
  assert.match(html, /data-record-index="3"/);
});

test('renderHtml: scrubber range matches record_ids length', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: baseDag,
    selectedRecordIndex: 2,
  }, 'n');
  // 4 records → max="3", current value="2" (selectedIndex)
  assert.match(html, /id="scrubber" min="0" max="3" value="2"/);
  assert.match(html, /3 \/ 4/);  // "current / total" label
});

test('renderHtml: record pane renders fields + parents + children', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: baseDag,
    selectedRecordIndex: 2,
    record: baseRecord,
  }, 'n');
  // Record header
  assert.match(html, /<h3>bt-2026-05-02-100000:plan:003<\/h3>/);
  assert.match(html, /subgraph_node_count: 4/);
  // Fields rendered as table rows (sorted keys)
  assert.match(html, /<td class="k">phase<\/td>.*?plan/s);
  assert.match(html, /<td class="k">op_id<\/td>.*?op-x/s);
  // Parents + children
  assert.match(html, /parents:.*?bt-2026-05-02-100000:route:002/s);
  assert.match(html, /children:.*?bt-2026-05-02-100000:generate:004/s);
});

test('renderHtml: empty parents/children show "root"/"leaf"', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: baseDag,
    selectedRecordIndex: 0,
    record: { ...baseRecord, parents: [], children: [] },
  }, 'n');
  assert.match(html, /parents:.*?root/s);
  assert.match(html, /children:.*?leaf/s);
});

test('renderHtml: counterfactual branches rendered when present', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: baseDag,
    selectedRecordIndex: 2,
    record: {
      ...baseRecord,
      counterfactual_branches: [
        { branch_id: 'cf-1', verdict: 'diverged_better' },
      ],
    },
  }, 'n');
  assert.match(html, /Counterfactual branches/);
  assert.match(html, /branch_id/);
  assert.match(html, /diverged_better/);
});

test('renderHtml: verdicts ribbon renders verdict-kind class', () => {
  const html = renderHtml({
    ...emptyState(),
    replayVerdicts: {
      schema_version: '1.0',
      verdicts: [
        { verdict: 'equivalent', tightening: 'PASSED' },
        { verdict: 'diverged_better', tightening: 'PASSED' },
        { verdict: 'diverged_worse', tightening: 'PASSED' },
      ],
      count: 3,
      limit: 20,
    },
  }, 'n');
  assert.match(html, /verdict verdict-equivalent/);
  assert.match(html, /verdict verdict-diverged_better/);
  assert.match(html, /verdict verdict-diverged_worse/);
});

test('renderHtml: replayHealth header shows engine status', () => {
  const html = renderHtml({
    ...emptyState(),
    replayHealth: {
      schema_version: '1.0',
      enabled: true,
      engine_enabled: true,
      comparator_enabled: false,
      observer_enabled: true,
      history_path: '/x/r.jsonl',
      history_count: 7,
    },
  }, 'n');
  assert.match(html, /engine=on/);
  assert.match(html, /comparator=off/);
  assert.match(html, /observer=on/);
  assert.match(html, /history_count=7/);
});

test('renderHtml: replayHealth=null shows unreachable label', () => {
  const html = renderHtml(emptyState(), 'n');
  assert.match(html, /replay surface unreachable/);
});

// --- Security ------------------------------------------------------------

test('renderHtml: CSP nonce wired into script tag', () => {
  const html = renderHtml(emptyState(), 'nonce-test-xyz');
  assert.match(html, /script-src 'nonce-nonce-test-xyz'/);
  assert.match(html, /<script nonce="nonce-test-xyz">/);
});

test('renderHtml: CSP forbids unsafe-inline + unsafe-eval in script-src', () => {
  const html = renderHtml(emptyState(), 'n');
  const cspMatch = html.match(
    /Content-Security-Policy"\s+content="([^"]+)"/,
  );
  assert.ok(cspMatch);
  const csp = cspMatch![1];
  const scriptSrc = csp.split(';')
    .map((s) => s.trim())
    .find((s) => s.startsWith('script-src'));
  assert.ok(scriptSrc);
  assert.doesNotMatch(scriptSrc!, /unsafe-inline/i);
  assert.doesNotMatch(scriptSrc!, /unsafe-eval/i);
  assert.match(scriptSrc!, /'nonce-/);
});

test('renderHtml: dynamic session_id escaped in attributes', () => {
  const malicious = {
    ...baseSession,
    session_id: 'bt"><script>alert(1)</script>',
  };
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [malicious], count: 1,
    },
  }, 'n');
  assert.doesNotMatch(html, /bt"><script>alert\(1\)<\/script>/);
  assert.match(html, /bt&quot;&gt;&lt;script&gt;alert/);
});

test('renderHtml: record_id escape inside title + data attrs', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: {
      ...baseDag,
      record_ids: ['rid"><img src=x>'],
    },
    selectedRecordIndex: 0,
  }, 'n');
  assert.doesNotMatch(html, /rid"><img src=x>/);
  assert.match(html, /rid&quot;&gt;&lt;img/);
});

test('renderHtml: </script> escape in inlined RECORD_IDS JSON', () => {
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: {
      ...baseDag,
      record_ids: ['</script><script>alert(2)</script>'],
    },
    selectedRecordIndex: 0,
  }, 'n');
  // Find the inline TOPOLOGY-equivalent assignment
  const m = html.match(/const RECORD_IDS = (.*?);/s);
  assert.ok(m, 'RECORD_IDS assignment present');
  // Inside the JSON payload, </script> must be escaped
  assert.doesNotMatch(m![1], /<\/script>/i);
  assert.match(m![1], /<\\\/script>/i);
});

test('renderHtml: record field values truncated to 240 chars', () => {
  const longValue = 'x'.repeat(500);
  const html = renderHtml({
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: baseDag,
    selectedRecordIndex: 0,
    record: {
      ...baseRecord,
      record: { long_field: longValue },
    },
  }, 'n');
  assert.match(html, /x{240}…/);
  assert.doesNotMatch(html, /x{300}/);  // truncation worked
});

// --- Error renderer ------------------------------------------------------

test('renderErrorHtml: error message escaped + retry button present', () => {
  const html = renderErrorHtml(
    'fetch failed: <script>x</script>', 'n',
  );
  assert.match(html, /Failed to load Temporal Slider/);
  assert.match(html, /&lt;script&gt;x&lt;\/script&gt;/);
  assert.match(html, /id="retry"/);
});

// --- escapeHtml ----------------------------------------------------------

test('escapeHtml escapes all five entities', () => {
  assert.equal(
    escapeHtml('<a href="x">o\'k & ok</a>'),
    '&lt;a href=&quot;x&quot;&gt;o&#39;k &amp; ok&lt;/a&gt;',
  );
});

// --- isReplayEvent type guard --------------------------------------------

function mkFrame(eventType: string): StreamEventFrame {
  return {
    schema_version: '1.0',
    event_id: 'x',
    event_type: eventType as never,
    op_id: '',
    timestamp: '',
    payload: {},
  };
}

test('isReplayEvent recognizes replay_start and replay_end', () => {
  assert.equal(isReplayEvent(mkFrame('replay_start')), true);
  assert.equal(isReplayEvent(mkFrame('replay_end')), true);
});

test('isReplayEvent rejects worktree + task + heartbeat events', () => {
  assert.equal(isReplayEvent(mkFrame('worktree_topology_updated')), false);
  assert.equal(isReplayEvent(mkFrame('task_created')), false);
  assert.equal(isReplayEvent(mkFrame('heartbeat')), false);
});


// ============================================================================
// Q2 Slice 6 — Diff UI
// ============================================================================


function stateWithSelectedRecord(
  recordId: string,
): TemporalSliderState {
  return {
    ...emptyState(),
    sessions: {
      schema_version: '1.0', sessions: [baseSession], count: 1,
    },
    selectedSessionId: baseSession.session_id,
    dag: { ...baseDag, record_ids: [recordId, 'r-other'] },
    selectedRecordIndex: 0,
    record: { ...baseRecord, record_id: recordId },
  };
}


test('renderHtml: no anchor → "Pin as diff anchor" button', () => {
  const html = renderHtml(stateWithSelectedRecord('r-cur'), 'n');
  assert.match(html, /id="set-anchor"/);
  assert.match(html, /Pin as diff anchor/);
  // No diff section yet
  assert.doesNotMatch(html, /Diff vs anchor/);
});


test('renderHtml: anchor === current → identity badge', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-cur',
  }, 'n');
  assert.match(html, /class="anchor-pill"/);
  assert.match(html, /id="clear-anchor"/);
  // Diff section explains the identity case
  assert.match(html, /current record is the anchor/);
});


test('renderHtml: anchor set + diff loading → "computing…" UX', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
    diff: null,
  }, 'n');
  assert.match(html, /Diff vs anchor/);
  assert.match(html, /computing diff/);
  assert.match(html, /<code>r-anchor<\/code>/);
});


test('renderHtml: diff OK with changes renders rows by kind', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
    diff: {
      schema_version: 'dag_record_diff.1',
      outcome: 'ok',
      record_id_a: 'r-anchor', record_id_b: 'r-cur',
      changes: [
        { path: ['phase'], kind: 'modified',
          value_a_repr: "'plan'", value_b_repr: "'generate'" },
        { path: ['x'], kind: 'added',
          value_a_repr: '', value_b_repr: '42' },
        { path: ['old_field'], kind: 'removed',
          value_a_repr: '"gone"', value_b_repr: '' },
      ],
      fields_total: 8, fields_changed: 3, detail: '',
    },
  }, 'n');
  assert.match(html, /3 of 8 fields differ/);
  assert.match(html, /diff-row diff-modified/);
  assert.match(html, /diff-row diff-added/);
  assert.match(html, /diff-row diff-removed/);
  // Path rendered with .-separator
  assert.match(html, /<code>phase<\/code>/);
  // Anchor / current spelled out
  assert.match(html, /anchor:\s*<code>r-anchor<\/code>/);
  assert.match(html, /current:\s*<code>r-cur<\/code>/);
});


test('renderHtml: diff OK identical records → "records are identical"', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
    diff: {
      schema_version: 'dag_record_diff.1',
      outcome: 'ok',
      record_id_a: 'r-anchor', record_id_b: 'r-cur',
      changes: [],
      fields_total: 5, fields_changed: 0, detail: '',
    },
  }, 'n');
  assert.match(html, /records are identical/);
});


test('renderHtml: diff truncated → warning pill', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
    diff: {
      schema_version: 'dag_record_diff.1',
      outcome: 'truncated',
      record_id_a: 'r-anchor', record_id_b: 'r-cur',
      changes: new Array(10).fill(0).map((_, i) => ({
        path: [`k${i}`], kind: 'modified' as const,
        value_a_repr: 'a', value_b_repr: 'b',
      })),
      fields_total: 50, fields_changed: 10,
      detail: 'emitted 10 changes (cap 10); operator should narrow scope',
    },
  }, 'n');
  assert.match(html, /class="warn-pill"/);
  assert.match(html, /truncated/);
});


test('renderHtml: diff failed → warn box with detail', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
    diff: {
      schema_version: 'dag_record_diff.1',
      outcome: 'failed',
      record_id_a: 'r-anchor', record_id_b: 'r-cur',
      changes: [],
      fields_total: 0, fields_changed: 0,
      detail: 'compute_failed:RuntimeError',
    },
  }, 'n');
  assert.match(html, /class="warn"/);
  assert.match(html, /compute_failed:RuntimeError/);
});


test('renderHtml: diff empty outcome → "no fields"', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
    diff: {
      schema_version: 'dag_record_diff.1',
      outcome: 'empty',
      record_id_a: 'r-anchor', record_id_b: 'r-cur',
      changes: [], fields_total: 0, fields_changed: 0, detail: '',
    },
  }, 'n');
  assert.match(html, /no fields on either record/);
});


test('renderHtml: diff payload XSS-escaped in repr fields', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
    diff: {
      schema_version: 'dag_record_diff.1',
      outcome: 'ok',
      record_id_a: 'r-anchor', record_id_b: 'r-cur',
      changes: [
        { path: ['<bad>'], kind: 'modified',
          value_a_repr: '<script>alert(1)</script>',
          value_b_repr: '"<img src=x>"' },
      ],
      fields_total: 1, fields_changed: 1, detail: '',
    },
  }, 'n');
  // Raw injection strings must NOT appear
  assert.doesNotMatch(html, /<script>alert\(1\)<\/script>/);
  assert.doesNotMatch(html, /<img src=x>/);
  // Escaped forms present
  assert.match(html, /&lt;script&gt;alert/);
  assert.match(html, /&lt;bad&gt;/);
});


test('renderHtml: anchor is foreign record → "anchor: <code>...</code>"', () => {
  const html = renderHtml({
    ...stateWithSelectedRecord('r-cur'),
    anchorRecordId: 'r-anchor',
  }, 'n');
  // The anchor bar shows the foreign anchor id
  assert.match(html, /anchor:\s*<code>r-anchor<\/code>/);
  // Re-pin button visible to operator
  assert.match(html, /Re-pin to current/);
});
