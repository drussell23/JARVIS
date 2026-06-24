/**
 * authFsm tests -- proves the machine permits ONLY the legal transitions.
 *
 * The whole point of the FSM is that illegal sequences are structurally
 * impossible: you cannot SUBMIT/POST before CAPTURE_DONE, you cannot
 * re-capture while PROCESSING, and a REJECTED nonce is discarded on RESET
 * (the orchestrator must fetch a FRESH challenge). XState drops events no
 * state declares, so an illegal event is a guaranteed no-op.
 */

import { describe, expect, test } from 'vitest';
import { createActor } from 'xstate';
import { authMachine } from '../lib/authFsm';
import type { AuthEvent } from '../lib/authFsm';
import type { ElevationChallenge, ElevationVerdict } from '../lib/types';

const CHALLENGE: ElevationChallenge = {
  nonce: 'a'.repeat(64),
  phrase: 'sovereign falcon nine',
  pr_id: 'pr-1',
  ast_mutation_id: 'mut-1',
  blast_radius_hash: 'deadbeef',
  issued_at: 1000,
  ttl_s: 90,
};

const AUDIO = { audioB64: 'QUJD', sampleRate: 16000 } as const;

function okVerdict(): ElevationVerdict {
  return {
    decision: 'AUTHORIZED',
    reason: 'ok',
    ecapa_score: 0.91,
    antispoof_ok: true,
    freshness_ok: true,
    pr_id: 'pr-1',
    ast_mutation_id: 'mut-1',
    target_repo: 'jarvis',
  };
}

function rejectVerdict(reason: string, repo = 'jarvis'): ElevationVerdict {
  return {
    decision: 'REJECTED',
    reason,
    ecapa_score: 0.4,
    antispoof_ok: false,
    freshness_ok: true,
    pr_id: 'pr-1',
    ast_mutation_id: 'mut-1',
    target_repo: repo,
  };
}

function start() {
  const actor = createActor(authMachine);
  actor.start();
  return actor;
}

const OPEN: AuthEvent = {
  type: 'OPEN',
  prId: 'pr-1',
  astMutationId: 'mut-1',
  blastRadiusHash: 'deadbeef',
  challenge: CHALLENGE,
};

describe('authFsm legal happy path', () => {
  test('IDLE -> AWAITING -> CAPTURING -> PROCESSING -> AUTHORIZED', () => {
    const actor = start();
    expect(actor.getSnapshot().value).toBe('IDLE');

    actor.send(OPEN);
    expect(actor.getSnapshot().value).toBe('AWAITING_MIC_PERMISSION');
    expect(actor.getSnapshot().context.challenge?.nonce).toBe(CHALLENGE.nonce);

    actor.send({ type: 'MIC_GRANTED' });
    expect(actor.getSnapshot().value).toBe('CAPTURING_AUDIO');

    actor.send({ type: 'CAPTURE_DONE', audio: AUDIO });
    expect(actor.getSnapshot().value).toBe('PROCESSING_EMBEDDING');
    expect(actor.getSnapshot().context.audio).toEqual(AUDIO);

    actor.send({ type: 'RESULT_OK', verdict: okVerdict() });
    expect(actor.getSnapshot().value).toBe('AUTHORIZED');
    expect(actor.getSnapshot().context.verdict?.decision).toBe('AUTHORIZED');
  });
});

describe('authFsm forbids illegal transitions', () => {
  test('SUBMIT is impossible before CAPTURE_DONE (no-op in early states)', () => {
    const actor = start();
    // From IDLE.
    actor.send({ type: 'SUBMIT' });
    expect(actor.getSnapshot().value).toBe('IDLE');

    actor.send(OPEN);
    // From AWAITING_MIC_PERMISSION -- still no captured audio.
    actor.send({ type: 'SUBMIT' });
    expect(actor.getSnapshot().value).toBe('AWAITING_MIC_PERMISSION');

    actor.send({ type: 'MIC_GRANTED' });
    // From CAPTURING_AUDIO -- still no captured audio.
    actor.send({ type: 'SUBMIT' });
    expect(actor.getSnapshot().value).toBe('CAPTURING_AUDIO');
  });

  test('RESULT_OK before capture is a no-op (cannot skip to AUTHORIZED)', () => {
    const actor = start();
    actor.send(OPEN);
    actor.send({ type: 'RESULT_OK', verdict: okVerdict() });
    // Still awaiting mic -- the verdict event was dropped.
    expect(actor.getSnapshot().value).toBe('AWAITING_MIC_PERMISSION');
    expect(actor.getSnapshot().context.verdict).toBeNull();
  });

  test('re-capture during PROCESSING is impossible', () => {
    const actor = start();
    actor.send(OPEN);
    actor.send({ type: 'MIC_GRANTED' });
    actor.send({ type: 'CAPTURE_DONE', audio: AUDIO });
    expect(actor.getSnapshot().value).toBe('PROCESSING_EMBEDDING');

    // A second CAPTURE_DONE while PROCESSING is not declared -> no-op.
    const overwrite = { audioB64: 'WFla', sampleRate: 16000 } as const;
    actor.send({ type: 'CAPTURE_DONE', audio: overwrite });
    expect(actor.getSnapshot().value).toBe('PROCESSING_EMBEDDING');
    // The original captured buffer is preserved (no re-capture).
    expect(actor.getSnapshot().context.audio).toEqual(AUDIO);

    // MIC_GRANTED while PROCESSING is also a no-op.
    actor.send({ type: 'MIC_GRANTED' });
    expect(actor.getSnapshot().value).toBe('PROCESSING_EMBEDDING');
  });

  test('MIC_GRANTED from IDLE is a no-op (must OPEN first)', () => {
    const actor = start();
    actor.send({ type: 'MIC_GRANTED' });
    expect(actor.getSnapshot().value).toBe('IDLE');
  });
});

describe('authFsm error + reject paths', () => {
  test('MIC_DENIED routes to ERROR with a message', () => {
    const actor = start();
    actor.send(OPEN);
    actor.send({ type: 'MIC_DENIED', message: 'permission denied' });
    expect(actor.getSnapshot().value).toBe('ERROR');
    expect(actor.getSnapshot().context.errorMessage).toBe('permission denied');
  });

  test('RESULT_REJECTED routes to REJECTED and carries the verdict', () => {
    const actor = start();
    actor.send(OPEN);
    actor.send({ type: 'MIC_GRANTED' });
    actor.send({ type: 'CAPTURE_DONE', audio: AUDIO });
    actor.send({
      type: 'RESULT_REJECTED',
      verdict: rejectVerdict('voiceprint_mismatch'),
    });
    expect(actor.getSnapshot().value).toBe('REJECTED');
    expect(actor.getSnapshot().context.verdict?.reason).toBe(
      'voiceprint_mismatch',
    );
  });
});

describe('authFsm single-use nonce on RESET', () => {
  test('REJECTED -> RESET clears the consumed nonce (forces fresh challenge)', () => {
    const actor = start();
    actor.send(OPEN);
    actor.send({ type: 'MIC_GRANTED' });
    actor.send({ type: 'CAPTURE_DONE', audio: AUDIO });
    actor.send({
      type: 'RESULT_REJECTED',
      verdict: rejectVerdict('antispoof_fail'),
    });
    expect(actor.getSnapshot().value).toBe('REJECTED');

    actor.send({ type: 'RESET' });
    const snap = actor.getSnapshot();
    expect(snap.value).toBe('IDLE');
    // The consumed nonce + everything else is discarded -- a re-OPEN MUST
    // carry a brand-new challenge (we never resend a spent nonce).
    expect(snap.context.challenge).toBeNull();
    expect(snap.context.audio).toBeNull();
    expect(snap.context.verdict).toBeNull();
  });

  test('AUTHORIZED -> RESET returns to IDLE for a fresh challenge', () => {
    const actor = start();
    actor.send(OPEN);
    actor.send({ type: 'MIC_GRANTED' });
    actor.send({ type: 'CAPTURE_DONE', audio: AUDIO });
    actor.send({ type: 'RESULT_OK', verdict: okVerdict() });
    expect(actor.getSnapshot().value).toBe('AUTHORIZED');
    actor.send({ type: 'RESET' });
    expect(actor.getSnapshot().value).toBe('IDLE');
    expect(actor.getSnapshot().context.challenge).toBeNull();
  });
});
