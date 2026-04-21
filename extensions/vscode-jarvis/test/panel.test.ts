/**
 * OpDetailPanel rendering tests — the HTML builders are pure
 * functions so we can test them without booting VS Code.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  escapeHtml,
  renderErrorHtml,
  renderHtml,
} from '../src/panel/opDetailPanel';
import { TaskDetailResponse } from '../src/api/types';

const sampleDetail: TaskDetailResponse = {
  schema_version: '1.0',
  op_id: 'op-abc',
  closed: false,
  active_task_id: 'task-op-abc-0002',
  board_size: 3,
  tasks: [
    {
      task_id: 'task-op-abc-0001',
      state: 'completed',
      title: 'First task',
      body: 'Some body text',
      sequence: 1,
      cancel_reason: '',
    },
    {
      task_id: 'task-op-abc-0002',
      state: 'in_progress',
      title: 'Second task',
      body: '',
      sequence: 2,
      cancel_reason: '',
    },
    {
      task_id: 'task-op-abc-0003',
      state: 'cancelled',
      title: 'Third task',
      body: 'will not finish',
      sequence: 3,
      cancel_reason: 'user abort',
    },
  ],
};

test('renderHtml includes the op_id and all tasks', () => {
  const html = renderHtml(sampleDetail);
  assert.match(html, /op-abc/);
  assert.match(html, /task-op-abc-0001/);
  assert.match(html, /task-op-abc-0002/);
  assert.match(html, /task-op-abc-0003/);
});

test('renderHtml includes state-specific chip classes', () => {
  const html = renderHtml(sampleDetail);
  assert.match(html, /state-chip state-completed/);
  assert.match(html, /state-chip state-in_progress/);
  assert.match(html, /state-chip state-cancelled/);
});

test('renderHtml escapes HTML in titles to prevent injection', () => {
  const detail: TaskDetailResponse = {
    ...sampleDetail,
    tasks: [
      {
        task_id: 'task-x-0001',
        state: 'pending',
        title: '<script>alert(1)</script>',
        body: '"&\'',
        sequence: 1,
        cancel_reason: '',
      },
    ],
  };
  const html = renderHtml(detail);
  assert.ok(!html.includes('<script>alert(1)</script>'));
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});

test('renderHtml has a strict Content-Security-Policy meta tag', () => {
  const html = renderHtml(sampleDetail);
  assert.match(html, /Content-Security-Policy/);
  assert.match(html, /default-src 'none'/);
  // Scripts must NOT be allowed.
  assert.ok(!/script-src/.test(html) || /script-src 'none'/.test(html));
});

test('renderHtml shows LIVE badge when not closed and CLOSED when closed', () => {
  assert.match(renderHtml(sampleDetail), /LIVE/);
  assert.match(
    renderHtml({ ...sampleDetail, closed: true }),
    /CLOSED/,
  );
});

test('renderHtml shows empty state for zero-task board', () => {
  const html = renderHtml({
    ...sampleDetail,
    tasks: [],
    board_size: 0,
    active_task_id: null,
  });
  assert.match(html, /No tasks yet/);
});

test('renderHtml shows cancel_reason when present', () => {
  const html = renderHtml(sampleDetail);
  assert.match(html, /reason: user abort/);
});

test('renderErrorHtml escapes the message', () => {
  const html = renderErrorHtml('op-x', '<bad>&err');
  assert.match(html, /&lt;bad&gt;&amp;err/);
  assert.ok(!html.includes('<bad>'));
});

test('escapeHtml covers all five canonical entities', () => {
  assert.equal(escapeHtml(`<>&"'`), '&lt;&gt;&amp;&quot;&#39;');
});
