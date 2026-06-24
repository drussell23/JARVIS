'use client';

/**
 * useBiometricAuth -- orchestrates the biometric AuthorizeElevation flow:
 * the strict FSM (authFsm) <-> the write-path API (BiometricAuthClient) <->
 * Web Audio capture (captureAuthAudio).
 *
 * The hook is the ONLY place these three are wired together. It drives the
 * machine; the machine is the authority on what is legal at any instant
 * (e.g. it is structurally impossible to POST before CAPTURE_DONE). The
 * frontend NEVER decides authorization -- it captures + transmits and
 * reflects the backend verdict.
 *
 * Flow:
 *   open(pr_id, ast_mutation_id, blast_radius_hash)
 *     -> GET a FRESH single-use challenge
 *     -> OPEN the machine (-> AWAITING_MIC_PERMISSION)
 *     -> request mic + capture audio (-> CAPTURING_AUDIO -> PROCESSING)
 *     -> POST {pr_id, nonce, ast_mutation_id, audio_b64, sample_rate}
 *     -> AUTHORIZED | REJECTED (reflecting the backend verdict)
 *
 * Single-use nonce: a REJECTED -> reset() discards the consumed nonce; the
 * NEXT open() re-fetches a brand-new challenge. We never resend a spent
 * nonce. retry() is sugar for reset()-then-open() with the same PR.
 *
 * Fail-CLOSED: any error in challenge / mic / capture / POST routes the
 * machine to ERROR (or REJECTED for a backend 403 verdict). A gated-off
 * backend (404 challenge) surfaces as a distinct `disabled` flag so the
 * caller can show "biometric auth disabled" instead of crashing.
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import { useMachine } from '@xstate/react';
import { authMachine } from '../lib/authFsm';
import type { AuthStateValue } from '../lib/authFsm';
import {
  AuthDisabledError,
  BiometricAuthClient,
} from '../lib/api';
import {
  captureAuthAudio,
  type CaptureOptions,
  type CaptureResult,
} from '../lib/audioCapture';
import type { ElevationVerdict } from '../lib/types';
import { isImmutableOrange } from '../lib/types';

export interface UseBiometricAuthOptions {
  /** Base URL of the EventChannelServer (write-path host). */
  readonly endpoint: string;
  /** Test injection -- a pre-built client (overrides endpoint). */
  readonly client?: BiometricAuthClient;
  /** Test injection -- a capture fn (overrides the real Web Audio path). */
  readonly captureFn?: (opts?: CaptureOptions) => Promise<CaptureResult>;
  /** Capture duration override (ms). */
  readonly captureMs?: number;
}

/** The target a challenge is bound to (carried so retry() can re-open). */
interface AuthTarget {
  readonly prId: string;
  readonly astMutationId: string;
  readonly blastRadiusHash: string;
}

export interface BiometricAuthApi {
  /** Current FSM state name (drives the modal's locked UI). */
  readonly state: AuthStateValue;
  /** The backend verdict, once received (null otherwise). */
  readonly verdict: ElevationVerdict | null;
  /** The challenge phrase the operator must speak (null until fetched). */
  readonly phrase: string | null;
  /** A human-readable error for the ERROR state. */
  readonly errorMessage: string | null;
  /** True when the write-path backend is gated off (404 challenge). */
  readonly disabled: boolean;
  /** True when the rejection is the Immutable Orange law, not a mismatch. */
  readonly immutableOrange: boolean;
  /** Open the flow for a PR: fetch a fresh challenge + run capture+POST. */
  readonly open: (target: AuthTarget) => Promise<void>;
  /** Discard state (and the consumed nonce) -> back to IDLE. */
  readonly reset: () => void;
  /** Reset + re-open with a FRESH challenge (never resends a spent nonce). */
  readonly retry: () => Promise<void>;
}

export function useBiometricAuth(
  opts: UseBiometricAuthOptions,
): BiometricAuthApi {
  const [snapshot, send] = useMachine(authMachine);
  const [disabled, setDisabled] = useState(false);

  // The client is stable for the hook's lifetime.
  const client = useMemo(
    () => opts.client ?? new BiometricAuthClient({ endpoint: opts.endpoint }),
    [opts.client, opts.endpoint],
  );
  const capture = opts.captureFn ?? captureAuthAudio;

  // Carry the last target so retry() can re-open with a fresh challenge.
  const targetRef = useRef<AuthTarget | null>(null);
  // Guard against overlapping open() calls (double-click). The machine
  // already rejects illegal transitions, but we also short-circuit a
  // second concurrent run that would race the first.
  const runningRef = useRef(false);

  const reset = useCallback((): void => {
    setDisabled(false);
    send({ type: 'RESET' });
  }, [send]);

  const run = useCallback(
    async (target: AuthTarget): Promise<void> => {
      if (runningRef.current) {
        return;
      }
      runningRef.current = true;
      targetRef.current = target;
      setDisabled(false);
      try {
        // 1. Fetch a FRESH single-use challenge. A 404 = gated off.
        let challenge;
        try {
          challenge = await client.challenge(
            target.prId,
            target.astMutationId,
            target.blastRadiusHash,
          );
        } catch (exc) {
          if (exc instanceof AuthDisabledError) {
            setDisabled(true);
            return; // stay IDLE; the modal shows "disabled".
          }
          throw exc;
        }

        // 2. OPEN the machine -> AWAITING_MIC_PERMISSION.
        send({
          type: 'OPEN',
          prId: target.prId,
          astMutationId: target.astMutationId,
          blastRadiusHash: target.blastRadiusHash,
          challenge,
        });

        // 3. Request mic + capture. captureAuthAudio acquires the mic, runs
        //    the bounded capture, and ALWAYS releases it. We mark the FSM
        //    MIC_GRANTED (-> CAPTURING_AUDIO) once the capture call resolves
        //    successfully (the mic was granted + the buffer captured); a
        //    permission denial throws AudioCaptureError -> MIC_DENIED.
        let captured: CaptureResult;
        try {
          captured = await capture({ durationMs: opts.captureMs });
        } catch (exc) {
          const msg = exc instanceof Error ? exc.message : String(exc);
          send({ type: 'MIC_DENIED', message: msg });
          return;
        }
        // The mic was granted -> advance into CAPTURING_AUDIO. (CAPTURE_DONE
        // is only legal from CAPTURING_AUDIO, so this strict step is what
        // makes a bufferless POST structurally impossible.)
        send({ type: 'MIC_GRANTED' });

        // 4. CAPTURE_DONE carries the buffer -> PROCESSING_EMBEDDING. This
        //    is the only path into PROCESSING, so a SUBMIT/POST can never
        //    precede a captured buffer.
        send({
          type: 'CAPTURE_DONE',
          audio: {
            audioB64: captured.audioB64,
            sampleRate: captured.sampleRate,
          },
        });
        // SUBMIT is the explicit "POST in flight" signal (inert, guarded on
        // hasAudio) -- it documents intent without an illegal transition.
        send({ type: 'SUBMIT' });

        // 5. POST the audio + the single-use nonce. The verdict body is the
        //    authority for both 200 (AUTHORIZED) and 403 (REJECTED).
        let verdict: ElevationVerdict;
        try {
          verdict = await client.authorize({
            pr_id: target.prId,
            nonce: challenge.nonce,
            ast_mutation_id: target.astMutationId,
            audio_b64: captured.audioB64,
            sample_rate: captured.sampleRate,
          });
        } catch (exc) {
          if (exc instanceof AuthDisabledError) {
            setDisabled(true);
            send({ type: 'ERROR', message: 'biometric auth disabled' });
            return;
          }
          throw exc;
        }

        if (verdict.decision === 'AUTHORIZED') {
          send({ type: 'RESULT_OK', verdict });
        } else {
          send({ type: 'RESULT_REJECTED', verdict });
        }
      } catch (exc) {
        const msg = exc instanceof Error ? exc.message : String(exc);
        send({ type: 'ERROR', message: msg });
      } finally {
        runningRef.current = false;
      }
    },
    [client, capture, opts.captureMs, send],
  );

  const open = useCallback(
    async (target: AuthTarget): Promise<void> => {
      await run(target);
    },
    [run],
  );

  const retry = useCallback(async (): Promise<void> => {
    const target = targetRef.current;
    // Discard the consumed nonce first, then re-open with a FRESH challenge.
    send({ type: 'RESET' });
    setDisabled(false);
    if (target !== null) {
      await run(target);
    }
  }, [run, send]);

  const ctx = snapshot.context;
  const verdict = ctx.verdict;

  return {
    state: snapshot.value as AuthStateValue,
    verdict,
    phrase: ctx.challenge?.phrase ?? null,
    errorMessage: ctx.errorMessage,
    disabled,
    immutableOrange: isImmutableOrange(verdict),
    open,
    reset,
    retry,
  };
}
