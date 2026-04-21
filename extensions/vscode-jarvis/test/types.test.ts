/**
 * Pure-logic tests for src/api/types.ts.
 *
 * No vscode module needed — these tests run under plain Node.js via
 * `node --test` on the compiled output under dist-test/.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  SUPPORTED_SCHEMA_VERSION,
  StreamEventFrame,
  isControlEvent,
  isSupportedSchema,
  isTaskEvent,
} from '../src/api/types';

test('SUPPORTED_SCHEMA_VERSION is 1.0', () => {
  assert.equal(SUPPORTED_SCHEMA_VERSION, '1.0');
});

test('isSupportedSchema returns true on matching version', () => {
  assert.equal(isSupportedSchema({ schema_version: '1.0' }), true);
});

test('isSupportedSchema returns false on mismatched version', () => {
  assert.equal(isSupportedSchema({ schema_version: '2.0' }), false);
});

test('isSupportedSchema returns false on null/undefined', () => {
  assert.equal(isSupportedSchema(null), false);
  assert.equal(isSupportedSchema(undefined), false);
});

function makeFrame(event_type: string): StreamEventFrame {
  return {
    schema_version: '1.0',
    event_id: 'e1',
    event_type: event_type as StreamEventFrame['event_type'],
    op_id: 'op-x',
    timestamp: 't',
    payload: {},
  };
}

test('isTaskEvent recognizes all six task transitions', () => {
  for (const t of [
    'task_created',
    'task_started',
    'task_updated',
    'task_completed',
    'task_cancelled',
    'board_closed',
  ]) {
    assert.equal(isTaskEvent(makeFrame(t)), true, `expected task event: ${t}`);
  }
});

test('isTaskEvent rejects control events', () => {
  for (const t of ['heartbeat', 'stream_lag', 'replay_start', 'replay_end']) {
    assert.equal(
      isTaskEvent(makeFrame(t)),
      false,
      `expected NOT task event: ${t}`,
    );
  }
});

test('isControlEvent recognizes all four control types', () => {
  for (const t of ['heartbeat', 'stream_lag', 'replay_start', 'replay_end']) {
    assert.equal(
      isControlEvent(makeFrame(t)),
      true,
      `expected control event: ${t}`,
    );
  }
});

test('isControlEvent rejects task events', () => {
  assert.equal(isControlEvent(makeFrame('task_created')), false);
});
