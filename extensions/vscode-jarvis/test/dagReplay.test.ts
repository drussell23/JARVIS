/**
 * Gap #1 Slice 1 — DAG / Replay / Sessions client tests.
 *
 * Stub-fetch isolated. Verifies URL construction + query
 * encoding + ID validation + server-error surfacing for the
 * 7 new ObservabilityClient methods.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ObservabilityClient,
  ObservabilityError,
} from '../src/api/client';

type StubFetch = (
  url: string, init?: RequestInit,
) => Promise<Response>;

function mkResponse(body: unknown, status = 200): Response {
  return new Response(
    typeof body === 'string' ? body : JSON.stringify(body),
    { status, headers: { 'Content-Type': 'application/json' } },
  );
}

function mkClient(fetchFn: StubFetch): ObservabilityClient {
  return new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
}

// --- sessionList ---------------------------------------------------------

test('sessionList() bare URL when no opts', async () => {
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0', sessions: [], count: 0,
    });
  });
  await c.sessionList();
  assert.equal(captured, 'http://127.0.0.1:1234/observability/sessions');
});

test('sessionList() encodes filter params', async () => {
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0', sessions: [], count: 0,
    });
  });
  await c.sessionList({
    limit: 50, ok: true, bookmarked: false,
    hasReplay: true, prefix: 'bt-2026',
  });
  assert.match(captured, /limit=50/);
  assert.match(captured, /ok=true/);
  assert.match(captured, /bookmarked=false/);
  assert.match(captured, /has_replay=true/);
  assert.match(captured, /prefix=bt-2026/);
});

test('sessionList() rejects out-of-range limit', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.sessionList({ limit: 9999 }),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_limit',
  );
});

test('sessionList() rejects malformed prefix', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.sessionList({ prefix: 'bad prefix!' }),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_prefix',
  );
});

// --- sessionDetail -------------------------------------------------------

test('sessionDetail() encodes session_id with colons + dots', async () => {
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0',
      session: { session_id: 'bt-2026-05-02:abc' },
    });
  });
  await c.sessionDetail('bt-2026-05-02:abc');
  assert.match(captured, /bt-2026-05-02%3Aabc$/);
});

test('sessionDetail() rejects malformed id', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.sessionDetail('has spaces!'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_session_id',
  );
});

// --- dagSession ----------------------------------------------------------

test('dagSession() returns parsed body', async () => {
  const c = mkClient(async () =>
    mkResponse({
      schema_version: '1.0',
      session_id: 'bt-x', node_count: 5, edge_count: 4,
      record_ids: ['r-0', 'r-1', 'r-2', 'r-3', 'r-4'],
    }),
  );
  const r = await c.dagSession('bt-x');
  assert.equal(r.node_count, 5);
  assert.equal(r.record_ids.length, 5);
});

test('dagSession() rejects malformed session_id', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.dagSession('has spaces'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_session_id',
  );
});

// --- dagRecord -----------------------------------------------------------

test('dagRecord() encodes both ids', async () => {
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0',
      record_id: 'r:phase:001',
      record: {}, parents: [], children: [],
      counterfactual_branches: [], subgraph_node_count: 0,
    });
  });
  await c.dagRecord('bt-x:test', 'r:phase:001');
  assert.match(captured, /bt-x%3Atest/);
  assert.match(captured, /r%3Aphase%3A001$/);
});

test('dagRecord() rejects malformed record_id', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.dagRecord('bt-x', 'bad space'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_record_id',
  );
});

test('dagRecord() rejects malformed session_id before checking record_id', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.dagRecord('has space', 'r-1'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_session_id',
  );
});

test('dagRecord() accepts long composite record_id (256-char window)', async () => {
  // Substrate's _RECORD_ID_RE is wider than _SESSION_ID_RE for
  // phase-capture composite ids.
  const longRec = 'r-' + 'a'.repeat(200);
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0',
      record_id: longRec,
      record: {}, parents: [], children: [],
      counterfactual_branches: [], subgraph_node_count: 0,
    });
  });
  await c.dagRecord('bt-x', longRec);
  assert.ok(captured.endsWith(encodeURIComponent(longRec)));
});

// --- replay surface ------------------------------------------------------

test('replayHealth() returns parsed body', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    enabled: true, engine_enabled: true,
    comparator_enabled: true, observer_enabled: true,
    history_path: '/x/replay.jsonl', history_count: 3,
  }));
  const r = await c.replayHealth();
  assert.equal(r.enabled, true);
  assert.equal(r.history_count, 3);
});

test('replayBaseline() returns parsed body', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    outcome: 'baseline_ok',
    tightening: 'PASSED',
    stats: { samples: 10 },
    detail: '',
  }));
  const r = await c.replayBaseline();
  assert.equal(r.outcome, 'baseline_ok');
  assert.equal(r.tightening, 'PASSED');
});

test('replayVerdicts() default no limit', async () => {
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0', verdicts: [], count: 0, limit: 50,
    });
  });
  await c.replayVerdicts();
  assert.equal(captured, 'http://127.0.0.1:1234/observability/replay/verdicts');
});

test('replayVerdicts() encodes limit', async () => {
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0', verdicts: [], count: 0, limit: 10,
    });
  });
  await c.replayVerdicts({ limit: 10 });
  assert.match(captured, /limit=10$/);
});

test('replayVerdicts() rejects out-of-range limit', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.replayVerdicts({ limit: 999 }),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_limit',
  );
});

// --- Server error surfacing ----------------------------------------------

test('dagSession() surfaces 403 reason_code', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    error: true,
    reason_code: 'dag_navigation.disabled',
  }, 403));
  try {
    await c.dagSession('bt-x');
    assert.fail('expected throw');
  } catch (exc) {
    assert.ok(exc instanceof ObservabilityError);
    assert.equal((exc as ObservabilityError).status, 403);
    assert.equal(
      (exc as ObservabilityError).reasonCode,
      'dag_navigation.disabled',
    );
  }
});

test('dagRecord() surfaces 404 reason_code', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    error: true,
    reason_code: 'dag_navigation.not_found',
  }, 404));
  try {
    await c.dagRecord('bt-x', 'r-missing');
    assert.fail('expected throw');
  } catch (exc) {
    assert.ok(exc instanceof ObservabilityError);
    assert.equal((exc as ObservabilityError).status, 404);
  }
});

test('replayHealth() surfaces 403 replay_disabled', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    error: true,
    reason_code: 'ide_observability.replay_disabled',
  }, 403));
  await assert.rejects(
    () => c.replayHealth(),
    (err: unknown) =>
      err instanceof ObservabilityError && err.status === 403,
  );
});

// --- Q2 Slice 6 — dagDiff -------------------------------------------------

test('dagDiff() encodes both record ids', async () => {
  let captured = '';
  const c = mkClient(async (url) => {
    captured = url;
    return mkResponse({
      schema_version: '1.0',
      outcome: 'ok',
      record_id_a: 'r:phase:001',
      record_id_b: 'r:phase:002',
      changes: [],
      fields_total: 5, fields_changed: 0, detail: '',
    });
  });
  await c.dagDiff('bt-x:test', 'r:phase:001', 'r:phase:002');
  assert.match(captured, /bt-x%3Atest/);
  assert.match(captured, /r%3Aphase%3A001/);
  assert.match(captured, /r%3Aphase%3A002$/);
  assert.match(captured, /\/diff\//);
});

test('dagDiff() returns parsed body', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    outcome: 'ok',
    record_id_a: 'r1', record_id_b: 'r2',
    changes: [
      { path: ['phase'], kind: 'modified',
        value_a_repr: "'plan'", value_b_repr: "'generate'" },
    ],
    fields_total: 4, fields_changed: 1, detail: '',
  }));
  const r = await c.dagDiff('bt-x', 'r1', 'r2');
  assert.equal(r.outcome, 'ok');
  assert.equal(r.changes.length, 1);
  assert.equal(r.changes[0].kind, 'modified');
  assert.deepEqual([...r.changes[0].path], ['phase']);
});

test('dagDiff() rejects malformed session_id', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.dagDiff('has space', 'r1', 'r2'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_session_id',
  );
});

test('dagDiff() rejects malformed record_id_a', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.dagDiff('bt-x', 'bad space', 'r2'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_record_id',
  );
});

test('dagDiff() rejects malformed record_id_b', async () => {
  const c = mkClient(async () => mkResponse({}));
  await assert.rejects(
    () => c.dagDiff('bt-x', 'r1', 'bad space'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_record_id',
  );
});

test('dagDiff() surfaces 404 not_found', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    error: true,
    reason_code: 'dag_navigation.not_found',
    missing: 'r1',
  }, 404));
  try {
    await c.dagDiff('bt-x', 'r1', 'r2');
    assert.fail('expected throw');
  } catch (exc) {
    assert.ok(exc instanceof ObservabilityError);
    assert.equal((exc as ObservabilityError).status, 404);
    assert.equal(
      (exc as ObservabilityError).reasonCode,
      'dag_navigation.not_found',
    );
  }
});

test('dagDiff() surfaces truncated outcome cleanly', async () => {
  const c = mkClient(async () => mkResponse({
    schema_version: '1.0',
    outcome: 'truncated',
    record_id_a: 'r1', record_id_b: 'r2',
    changes: new Array(10).fill(0).map((_, i) => ({
      path: [`k${i}`], kind: 'modified',
      value_a_repr: 'a', value_b_repr: 'b',
    })),
    fields_total: 50, fields_changed: 10,
    detail: 'emitted 10 changes (cap 10); operator should narrow scope',
  }));
  const r = await c.dagDiff('bt-x', 'r1', 'r2');
  assert.equal(r.outcome, 'truncated');
  assert.equal(r.changes.length, 10);
  assert.match(r.detail, /narrow scope/);
});
