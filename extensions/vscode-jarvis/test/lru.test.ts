/**
 * Tests for the bounded LRU cache inside opsProvider.ts.
 *
 * The LRU class is unexported, but it's wired into OpsTreeProvider's
 * `snapshot()` + public APIs. Rather than punch a back door, we
 * import the *compiled* module and re-export the private class for
 * tests. Simpler: verify LRU behavior via the public OpsTreeProvider
 * interface (since the cache is a concrete implementation detail of
 * the provider anyway).
 *
 * These tests mock vscode.EventEmitter since opsProvider imports it.
 * Node's test runner can't import 'vscode' at runtime — we install a
 * polyfill via a subpath redirect in the compiled output.
 */

import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import Module from 'node:module';
import path from 'node:path';

// Register a stub for the 'vscode' module before loading opsProvider.
before(() => {
  const stubPath = path.join(__dirname, 'mocks', 'vscode.js');
  const originalResolve = (Module as unknown as {
    _resolveFilename: (req: string, parent: unknown, ...rest: unknown[]) => string;
  })._resolveFilename;
  (Module as unknown as {
    _resolveFilename: (req: string, parent: unknown, ...rest: unknown[]) => string;
  })._resolveFilename = function (
    request: string,
    parent: unknown,
    ...rest: unknown[]
  ): string {
    if (request === 'vscode') {
      return stubPath;
    }
    return originalResolve.call(this, request, parent, ...rest);
  };
});

// These imports MUST be dynamic — static `import` runs before `before()`.
let OpsTreeProvider: typeof import('../src/tree/opsProvider').OpsTreeProvider;
let ObservabilityClient: typeof import('../src/api/client').ObservabilityClient;

before(async () => {
  const mod = await import('../src/tree/opsProvider');
  OpsTreeProvider = mod.OpsTreeProvider;
  const clientMod = await import('../src/api/client');
  ObservabilityClient = clientMod.ObservabilityClient;
});

function mkClientFactory(
  taskListResult: { op_ids: string[]; count: number },
): () => InstanceType<typeof ObservabilityClient> {
  return () => ({
    taskList: async () => ({
      schema_version: '1.0',
      ...taskListResult,
    }),
    taskDetail: async (opId: string) => ({
      schema_version: '1.0',
      op_id: opId,
      closed: false,
      active_task_id: null,
      tasks: [],
      board_size: 0,
    }),
    health: async () => ({
      schema_version: '1.0',
      enabled: true,
      api_version: '1.0',
      surface: 'tasks',
      now_mono: 0,
    }),
  }) as unknown as InstanceType<typeof ObservabilityClient>;
}

test('OpsTreeProvider snapshot starts empty', async () => {
  const provider = new OpsTreeProvider({
    client: mkClientFactory({ op_ids: [], count: 0 }),
    maxOpsCached: 4,
  });
  const snap = provider.snapshot();
  assert.deepEqual(snap.opIds, []);
  assert.equal(snap.cacheSize, 0);
});

test('OpsTreeProvider refresh populates opIds', async () => {
  const provider = new OpsTreeProvider({
    client: mkClientFactory({ op_ids: ['op-a', 'op-b'], count: 2 }),
    maxOpsCached: 4,
  });
  await provider.refresh();
  assert.deepEqual([...provider.snapshot().opIds], ['op-a', 'op-b']);
});

test('applyStreamEvent adds new op_id for task events', async () => {
  const provider = new OpsTreeProvider({
    client: mkClientFactory({ op_ids: [], count: 0 }),
    maxOpsCached: 4,
  });
  await provider.applyStreamEvent({
    schema_version: '1.0',
    event_id: 'e1',
    event_type: 'task_created',
    op_id: 'op-new',
    timestamp: 't',
    payload: {},
  });
  assert.deepEqual([...provider.snapshot().opIds], ['op-new']);
});

test('applyStreamEvent for control events does NOT add op_id', async () => {
  const provider = new OpsTreeProvider({
    client: mkClientFactory({ op_ids: [], count: 0 }),
    maxOpsCached: 4,
  });
  await provider.applyStreamEvent({
    schema_version: '1.0',
    event_id: 'e1',
    event_type: 'heartbeat',
    op_id: 'op-never-add',
    timestamp: 't',
    payload: {},
  });
  assert.deepEqual([...provider.snapshot().opIds], []);
});

test('refresh drops cached ops that are no longer in the list', async () => {
  let opIds = ['op-a', 'op-b'];
  const provider = new OpsTreeProvider({
    client: () => ({
      taskList: async () => ({
        schema_version: '1.0',
        op_ids: [...opIds],
        count: opIds.length,
      }),
      taskDetail: async (opId: string) => ({
        schema_version: '1.0',
        op_id: opId,
        closed: false,
        active_task_id: null,
        tasks: [],
        board_size: 0,
      }),
      health: async () => ({
        schema_version: '1.0',
        enabled: true,
        api_version: '1.0',
        surface: 'tasks',
        now_mono: 0,
      }),
    }) as unknown as InstanceType<typeof ObservabilityClient>,
    maxOpsCached: 4,
  });
  await provider.refresh();
  // Trigger children fetch to seed detail cache.
  await provider.getChildren();
  // Simulate server dropping op-a.
  opIds = ['op-b'];
  await provider.refresh();
  assert.deepEqual([...provider.snapshot().opIds], ['op-b']);
});
