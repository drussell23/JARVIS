/**
 * Worktree topology client + types regression suite.
 *
 * Tests the extended ObservabilityClient methods (worktreesList +
 * worktreeDetail), the type-guard helpers (isWorktreeEvent), and
 * the renderHtml output for safety + structure (no innerHTML on
 * data, escaped attributes, CSP nonce wired).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ObservabilityClient,
  ObservabilityError,
} from '../src/api/client';
import {
  StreamEventFrame,
  WorktreeTopologyProjection,
  isWorktreeEvent,
  isControlEvent,
  isTaskEvent,
} from '../src/api/types';
import { renderHtml } from '../src/panel/worktreeTopologyRenderers';

type StubFetch = (
  url: string, init?: RequestInit,
) => Promise<Response>;

function mkResponse(body: unknown, status = 200): Response {
  return new Response(
    typeof body === 'string' ? body : JSON.stringify(body),
    { status, headers: { 'Content-Type': 'application/json' } },
  );
}

const baseTopology: WorktreeTopologyProjection = {
  outcome: 'ok',
  graphs: [
    {
      graph_id: 'g1',
      op_id: 'op-1',
      planner_id: 'planner-x',
      plan_digest: 'abcdef0123456789',
      causal_trace_id: 'g1:abcdef012345',
      phase: 'running',
      concurrency_limit: 4,
      nodes: [
        {
          unit_id: 'a', repo: 'primary', goal: 'goal-a',
          target_files: ['file.py'],
          owned_paths: ['file.py'],
          dependency_ids: [],
          state: 'completed',
          barrier_id: '',
          has_worktree: true,
          worktree_path: '/x/.worktrees/unit-a',
          attempt_count: 1,
          schema_version: 'worktree_topology.1',
        },
        {
          unit_id: 'b', repo: 'primary', goal: 'goal-b',
          target_files: ['other.py'],
          owned_paths: ['other.py'],
          dependency_ids: ['a'],
          state: 'running',
          barrier_id: '',
          has_worktree: false,
          worktree_path: '',
          attempt_count: 0,
          schema_version: 'worktree_topology.1',
        },
      ],
      edges: [
        {
          from_unit_id: 'a', to_unit_id: 'b',
          edge_kind: 'dependency',
        },
      ],
      last_error: '',
      updated_at_ns: 1000,
      checksum: 'deadbeef',
      schema_version: 'worktree_topology.1',
    },
  ],
  summary: {
    total_graphs: 1,
    total_units: 2,
    units_by_state: { completed: 1, running: 1 },
    graphs_by_phase: { running: 1 },
    units_with_worktree: 1,
    orphan_worktree_count: 0,
    orphan_worktree_paths: [],
  },
  detail: '',
  captured_at_ns: 1000,
  schema_version: 'worktree_topology.1',
};

// --- Client ---------------------------------------------------------------

test('worktreesList() returns parsed body on 200', async () => {
  let capturedUrl = '';
  const fetchFn: StubFetch = async (url) => {
    capturedUrl = url;
    return mkResponse({
      schema_version: '1.0',
      topology: baseTopology,
    });
  };
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  const r = await c.worktreesList();
  assert.equal(capturedUrl, 'http://127.0.0.1:1234/observability/worktrees');
  assert.equal(r.topology.outcome, 'ok');
  assert.equal(r.topology.summary.total_units, 2);
});

test('worktreeDetail() URL-encodes graph_id', async () => {
  let capturedUrl = '';
  const fetchFn: StubFetch = async (url) => {
    capturedUrl = url;
    return mkResponse({
      schema_version: '1.0',
      graph: baseTopology.graphs[0],
    });
  };
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  await c.worktreeDetail('g-1.2:abc');
  assert.match(capturedUrl, /g-1\.2%3Aabc$/);
});

test('worktreeDetail() rejects malformed graph_id', async () => {
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: (async () => mkResponse({})) as unknown as typeof fetch,
  });
  await assert.rejects(
    () => c.worktreeDetail('has spaces!'),
    (err: unknown) =>
      err instanceof ObservabilityError &&
      err.reasonCode === 'client.malformed_graph_id',
  );
});

test('worktrees endpoints surface 503 from server as ObservabilityError', async () => {
  const fetchFn: StubFetch = async () => mkResponse({
    schema_version: '1.0',
    error: true,
    reason_code: 'ide_observability.worktrees_scheduler_not_wired',
  }, 503);
  const c = new ObservabilityClient({
    endpoint: 'http://127.0.0.1:1234',
    fetchFn: fetchFn as unknown as typeof fetch,
  });
  try {
    await c.worktreesList();
    assert.fail('expected throw');
  } catch (exc) {
    assert.ok(exc instanceof ObservabilityError);
    assert.equal((exc as ObservabilityError).status, 503);
    assert.equal(
      (exc as ObservabilityError).reasonCode,
      'ide_observability.worktrees_scheduler_not_wired',
    );
  }
});

// --- Type guards ----------------------------------------------------------

function mkFrame(eventType: string): StreamEventFrame {
  return {
    schema_version: '1.0',
    event_id: 'x', event_type: eventType as never,
    op_id: '', timestamp: '', payload: {},
  };
}

test('isWorktreeEvent recognizes both worktree types', () => {
  assert.equal(
    isWorktreeEvent(mkFrame('worktree_topology_updated')),
    true,
  );
  assert.equal(
    isWorktreeEvent(mkFrame('worktree_unit_state_changed')),
    true,
  );
});

test('isWorktreeEvent rejects task and control events', () => {
  assert.equal(isWorktreeEvent(mkFrame('task_created')), false);
  assert.equal(isWorktreeEvent(mkFrame('heartbeat')), false);
  assert.equal(isWorktreeEvent(mkFrame('replay_start')), false);
});

test('isTaskEvent + isControlEvent reject worktree events', () => {
  assert.equal(
    isTaskEvent(mkFrame('worktree_topology_updated')),
    false,
  );
  assert.equal(
    isControlEvent(mkFrame('worktree_topology_updated')),
    false,
  );
});

// --- renderHtml safety + structure ----------------------------------------

test('renderHtml stamps the supplied nonce in CSP and inline script tags', () => {
  const html = renderHtml(baseTopology, 'nonce-test-xyz');
  // CSP allowed only for matching nonce
  assert.match(
    html, /script-src 'nonce-nonce-test-xyz'/,
  );
  // Inline script tag carries the same nonce
  assert.match(html, /<script nonce="nonce-test-xyz">/);
});

test('renderHtml CSP forbids default-src and unsafe-inline scripts', () => {
  const html = renderHtml(baseTopology, 'n');
  assert.match(html, /default-src 'none'/);
  // CSP must NOT allow unsafe-inline INSIDE the script-src
  // directive. Style-src may use unsafe-inline (themed CSS); the
  // pin here is specifically that script-src is nonce-only.
  const cspMatch = html.match(
    /Content-Security-Policy"\s+content="([^"]+)"/,
  );
  assert.ok(cspMatch, 'CSP meta tag present');
  const csp = cspMatch![1];
  // Find script-src directive
  const scriptSrc = csp.split(';')
    .map((s) => s.trim())
    .find((s) => s.startsWith('script-src'));
  assert.ok(scriptSrc, 'script-src directive present');
  assert.doesNotMatch(scriptSrc!, /unsafe-inline/i);
  assert.doesNotMatch(scriptSrc!, /unsafe-eval/i);
  assert.match(scriptSrc!, /'nonce-/);
});

test('renderHtml escapes < > " in dynamic attribute values', () => {
  const malicious: WorktreeTopologyProjection = {
    ...baseTopology,
    graphs: [
      {
        ...baseTopology.graphs[0],
        graph_id: 'g"><script>alert(1)</script>',
        planner_id: 'p<plan',
        last_error: 'err"<>"end',
      },
    ],
  };
  const html = renderHtml(malicious, 'n');
  // The raw injection string MUST NOT appear unescaped
  assert.doesNotMatch(html, /g"><script>alert\(1\)<\/script>/);
  // Escaped form must be present
  assert.match(html, /g&quot;&gt;&lt;script&gt;alert/);
});

test('renderHtml escapes </script> sequences in JSON payload', () => {
  // A graph-level field containing </script> must be escaped so
  // the inline <script>...TOPOLOGY...</script> block can't be
  // broken out of by a doctored payload.
  const exploit: WorktreeTopologyProjection = {
    ...baseTopology,
    detail: '</script><script>alert(2)</script>',
  };
  const html = renderHtml(exploit, 'n');
  // The raw </script> must NOT appear inside the payload section
  // — it should be escaped to <\/script>.
  // Find the inline script block and check the payload portion.
  const m = html.match(/const TOPOLOGY = (.*?);/s);
  assert.ok(m, 'TOPOLOGY assignment present');
  assert.doesNotMatch(m![1], /<\/script>/i);
  assert.match(m![1], /<\\\/script>/i);
});

test('renderHtml omits orphan banner when count is zero', () => {
  const html = renderHtml(baseTopology, 'n');
  assert.doesNotMatch(html, /orphan worktree\(s\) on disk/);
});

test('renderHtml renders orphan banner when count > 0', () => {
  const withOrphans: WorktreeTopologyProjection = {
    ...baseTopology,
    summary: {
      ...baseTopology.summary,
      orphan_worktree_count: 2,
      orphan_worktree_paths: ['/x/unit-orphan1', '/x/unit-orphan2'],
    },
  };
  const html = renderHtml(withOrphans, 'n');
  assert.match(html, /2 orphan worktree\(s\)/);
  assert.match(html, /unit-orphan1/);
  assert.match(html, /unit-orphan2/);
});

test('renderHtml renders empty-state when no graphs', () => {
  const empty: WorktreeTopologyProjection = {
    ...baseTopology,
    outcome: 'empty',
    graphs: [],
    summary: {
      total_graphs: 0, total_units: 0,
      units_by_state: {}, graphs_by_phase: {},
      units_with_worktree: 0, orphan_worktree_count: 0,
      orphan_worktree_paths: [],
    },
  };
  const html = renderHtml(empty, 'n');
  assert.match(html, /no active execution graphs/);
  assert.match(html, /outcome=empty/);
});

test('renderHtml SVG container exists for each graph', () => {
  const html = renderHtml(baseTopology, 'n');
  assert.match(html, /<svg class="dag" id="dag-g1"/);
});

test('renderHtml shows last_error inline when present', () => {
  const errored: WorktreeTopologyProjection = {
    ...baseTopology,
    graphs: [
      {
        ...baseTopology.graphs[0],
        last_error: 'simulated_failure_class',
      },
    ],
  };
  const html = renderHtml(errored, 'n');
  assert.match(html, /simulated_failure_class/);
});

test('renderHtml summary aggregates rendered as pills', () => {
  const html = renderHtml(baseTopology, 'n');
  assert.match(html, /pill state-completed/);
  assert.match(html, /pill state-running/);
  assert.match(html, /pill phase-running/);
});
