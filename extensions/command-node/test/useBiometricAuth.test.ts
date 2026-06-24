/**
 * useBiometricAuth tests -- the orchestration of FSM <-> API <-> Web Audio.
 *
 * The client (BiometricAuthClient) + the capture fn are injected, so the
 * full happy-path (-> AUTHORIZED), the REJECTED path (reason surfaced +
 * fresh-challenge retry), the Immutable Orange special verdict, and the
 * mic-denied -> ERROR path are all exercised without a backend or hardware.
 */

import { describe, expect, test, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { useBiometricAuth } from '../hooks/useBiometricAuth';
import { AuthDisabledError, BiometricAuthClient } from '../lib/api';
import { AudioCaptureError } from '../lib/audioCapture';
import type {
  AuthorizeRequest,
  ElevationChallenge,
  ElevationVerdict,
} from '../lib/types';

function challenge(nonce: string): ElevationChallenge {
  return {
    nonce,
    phrase: 'sovereign falcon nine',
    pr_id: 'pr-1',
    ast_mutation_id: 'mut-1',
    blast_radius_hash: 'deadbeef',
    issued_at: 1000,
    ttl_s: 90,
  };
}

function verdict(
  decision: 'AUTHORIZED' | 'REJECTED',
  reason: string,
  repo = 'jarvis',
): ElevationVerdict {
  return {
    decision,
    reason,
    ecapa_score: decision === 'AUTHORIZED' ? 0.92 : 0.41,
    antispoof_ok: decision === 'AUTHORIZED',
    freshness_ok: true,
    pr_id: 'pr-1',
    ast_mutation_id: 'mut-1',
    target_repo: repo,
  };
}

/** Build a stub client with scriptable challenge + authorize behavior. */
function stubClient(opts: {
  challenges?: ElevationChallenge[];
  challengeError?: Error;
  authorize: (req: AuthorizeRequest) => Promise<ElevationVerdict>;
}): { client: BiometricAuthClient; authorizeSpy: ReturnType<typeof vi.fn> } {
  const queue = [...(opts.challenges ?? [])];
  const challengeFn = vi.fn(async () => {
    if (opts.challengeError) {
      throw opts.challengeError;
    }
    const next = queue.shift();
    if (next === undefined) {
      throw new Error('no challenge queued');
    }
    return next;
  });
  const authorizeSpy = vi.fn(opts.authorize);
  const client = {
    challenge: challengeFn,
    authorize: authorizeSpy,
  } as unknown as BiometricAuthClient;
  return { client, authorizeSpy };
}

const TARGET = {
  prId: 'pr-1',
  astMutationId: 'mut-1',
  blastRadiusHash: 'deadbeef',
};

const okCapture = vi.fn(async () => ({
  audioB64: 'QUJD',
  sampleRate: 16000,
}));

describe('useBiometricAuth happy path', () => {
  test('open -> challenge -> capture -> POST -> AUTHORIZED', async () => {
    const { client, authorizeSpy } = stubClient({
      challenges: [challenge('n1')],
      authorize: async () => verdict('AUTHORIZED', 'ok'),
    });
    const capture = vi.fn(async () => ({ audioB64: 'QUJD', sampleRate: 16000 }));

    const { result } = renderHook(() =>
      useBiometricAuth({ endpoint: 'http://x', client, captureFn: capture }),
    );

    await act(async () => {
      await result.current.open(TARGET);
    });

    await waitFor(() => expect(result.current.state).toBe('AUTHORIZED'));
    expect(result.current.phrase).toBe('sovereign falcon nine');
    expect(result.current.verdict?.decision).toBe('AUTHORIZED');
    // The POST carried the freshly-issued nonce.
    expect(authorizeSpy).toHaveBeenCalledTimes(1);
    expect(authorizeSpy.mock.calls[0]![0].nonce).toBe('n1');
    expect(capture).toHaveBeenCalledTimes(1);
  });
});

describe('useBiometricAuth REJECTED + fresh-challenge retry', () => {
  test('REJECTED surfaces the reason; retry re-fetches a FRESH nonce', async () => {
    const { client, authorizeSpy } = stubClient({
      challenges: [challenge('n1'), challenge('n2')],
      authorize: async () => verdict('REJECTED', 'voiceprint_mismatch'),
    });

    const { result } = renderHook(() =>
      useBiometricAuth({ endpoint: 'http://x', client, captureFn: okCapture }),
    );

    await act(async () => {
      await result.current.open(TARGET);
    });
    await waitFor(() => expect(result.current.state).toBe('REJECTED'));
    expect(result.current.verdict?.reason).toBe('voiceprint_mismatch');
    expect(result.current.immutableOrange).toBe(false);

    // Retry MUST issue a brand-new challenge (never resend the spent nonce).
    await act(async () => {
      await result.current.retry();
    });
    await waitFor(() => expect(result.current.state).toBe('REJECTED'));
    expect(authorizeSpy).toHaveBeenCalledTimes(2);
    expect(authorizeSpy.mock.calls[0]![0].nonce).toBe('n1');
    expect(authorizeSpy.mock.calls[1]![0].nonce).toBe('n2');
  });
});

describe('useBiometricAuth Immutable Orange', () => {
  test('immutable_orange reject is flagged distinctly (not a mismatch)', async () => {
    const { client } = stubClient({
      challenges: [challenge('n1')],
      authorize: async () =>
        verdict(
          'REJECTED',
          'immutable_orange:mind_nerves_never_auto_merge',
          'prime',
        ),
    });

    const { result } = renderHook(() =>
      useBiometricAuth({ endpoint: 'http://x', client, captureFn: okCapture }),
    );

    await act(async () => {
      await result.current.open(TARGET);
    });
    await waitFor(() => expect(result.current.state).toBe('REJECTED'));
    expect(result.current.immutableOrange).toBe(true);
    expect(result.current.verdict?.target_repo).toBe('prime');
  });
});

describe('useBiometricAuth failure paths', () => {
  test('mic denied -> ERROR with a message, never authorizes', async () => {
    const { client, authorizeSpy } = stubClient({
      challenges: [challenge('n1')],
      authorize: async () => verdict('AUTHORIZED', 'ok'),
    });
    const denied = vi.fn(async () => {
      throw new AudioCaptureError('permission denied', 'permission_denied');
    });

    const { result } = renderHook(() =>
      useBiometricAuth({ endpoint: 'http://x', client, captureFn: denied }),
    );

    await act(async () => {
      await result.current.open(TARGET);
    });
    await waitFor(() => expect(result.current.state).toBe('ERROR'));
    expect(result.current.errorMessage).toContain('permission denied');
    // Fail-CLOSED: the POST was never sent.
    expect(authorizeSpy).not.toHaveBeenCalled();
  });

  test('gated-off backend (404 challenge) surfaces disabled, stays IDLE', async () => {
    const { client, authorizeSpy } = stubClient({
      challengeError: new AuthDisabledError(),
      authorize: async () => verdict('AUTHORIZED', 'ok'),
    });

    const { result } = renderHook(() =>
      useBiometricAuth({ endpoint: 'http://x', client, captureFn: okCapture }),
    );

    await act(async () => {
      await result.current.open(TARGET);
    });
    await waitFor(() => expect(result.current.disabled).toBe(true));
    expect(result.current.state).toBe('IDLE');
    expect(authorizeSpy).not.toHaveBeenCalled();
  });
});
