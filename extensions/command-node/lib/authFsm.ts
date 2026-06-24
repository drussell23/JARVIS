/**
 * authFsm -- the STRICT typed UI state machine for the biometric
 * AuthorizeElevation flow, built on XState v5.
 *
 * This is a REAL state machine, not boolean-soup: the modal is locked to
 * the current state and only the explicitly-modelled transitions can
 * fire. Illegal events (e.g. SUBMIT before CAPTURE_DONE, a re-capture
 * while PROCESSING, a POST without a fresh nonce + a captured buffer) are
 * no-ops -- XState drops events that no state handles.
 *
 * The frontend NEVER decides authorization. It captures + transmits; the
 * backend is the sole authority. AUTHORIZED / REJECTED simply reflect the
 * backend verdict carried in the RESULT_* events.
 *
 * States:
 *   IDLE
 *     -> AWAITING_MIC_PERMISSION  (OPEN, once a challenge is loaded)
 *   AWAITING_MIC_PERMISSION
 *     -> CAPTURING_AUDIO          (MIC_GRANTED)
 *     -> ERROR                    (MIC_DENIED / ERROR)
 *   CAPTURING_AUDIO
 *     -> PROCESSING_EMBEDDING     (CAPTURE_DONE, carries the audio buffer)
 *     -> ERROR                    (ERROR)
 *   PROCESSING_EMBEDDING
 *     -> AUTHORIZED               (RESULT_OK)
 *     -> REJECTED                 (RESULT_REJECTED)
 *     -> ERROR                    (ERROR)
 *   AUTHORIZED  (final-ish; RESET returns to IDLE for a fresh challenge)
 *   REJECTED    (RESET returns to IDLE -- consumed nonce is discarded)
 *   ERROR       (RESET returns to IDLE)
 *
 * Note: SUBMIT is the orchestrator's signal that it is POSTing -- it is
 * only accepted in PROCESSING_EMBEDDING (we enter PROCESSING on
 * CAPTURE_DONE), so a SUBMIT can never precede a captured buffer. The
 * guard `hasAudio` additionally blocks RESULT_* / SUBMIT if no buffer was
 * captured, making an out-of-band POST structurally impossible.
 */

import { assign, setup } from 'xstate';
import {
  ElevationChallenge,
  ElevationVerdict,
} from './types';

/** A captured, encoded audio payload ready for transmission. */
export interface CapturedAudio {
  readonly audioB64: string;
  readonly sampleRate: number;
}

/** Typed machine context. Optional fields fill in as the flow advances. */
export interface AuthContext {
  /** The target PR + its mutation metadata (set on OPEN). */
  readonly prId: string | null;
  readonly astMutationId: string | null;
  readonly blastRadiusHash: string | null;
  /** The fresh, single-use challenge from the backend (set on OPEN). */
  readonly challenge: ElevationChallenge | null;
  /** The captured audio buffer (set on CAPTURE_DONE). */
  readonly audio: CapturedAudio | null;
  /** The backend verdict (set on RESULT_OK / RESULT_REJECTED). */
  readonly verdict: ElevationVerdict | null;
  /** A human-readable error message for the ERROR state. */
  readonly errorMessage: string | null;
}

/** The exhaustive, discriminated event union the machine accepts. */
export type AuthEvent =
  | {
      readonly type: 'OPEN';
      readonly prId: string;
      readonly astMutationId: string;
      readonly blastRadiusHash: string;
      readonly challenge: ElevationChallenge;
    }
  | { readonly type: 'MIC_GRANTED' }
  | { readonly type: 'MIC_DENIED'; readonly message: string }
  | { readonly type: 'CAPTURE_DONE'; readonly audio: CapturedAudio }
  | { readonly type: 'SUBMIT' }
  | { readonly type: 'RESULT_OK'; readonly verdict: ElevationVerdict }
  | { readonly type: 'RESULT_REJECTED'; readonly verdict: ElevationVerdict }
  | { readonly type: 'ERROR'; readonly message: string }
  | { readonly type: 'RESET' };

/** The names of every state, exported for exhaustive UI switches. */
export type AuthStateValue =
  | 'IDLE'
  | 'AWAITING_MIC_PERMISSION'
  | 'CAPTURING_AUDIO'
  | 'PROCESSING_EMBEDDING'
  | 'AUTHORIZED'
  | 'REJECTED'
  | 'ERROR';

const INITIAL_CONTEXT: AuthContext = {
  prId: null,
  astMutationId: null,
  blastRadiusHash: null,
  challenge: null,
  audio: null,
  verdict: null,
  errorMessage: null,
};

/**
 * The machine. `setup()` gives us strongly-typed actions + guards. Every
 * transition is explicit; XState ignores any event a state does not
 * declare, so illegal transitions are structurally impossible.
 */
export const authMachine = setup({
  types: {
    context: {} as AuthContext,
    events: {} as AuthEvent,
  },
  guards: {
    /** A POST is only legal once a buffer was captured. */
    hasAudio: ({ context }) => context.audio !== null,
  },
  actions: {
    onOpen: assign(({ event }) => {
      if (event.type !== 'OPEN') {
        return {};
      }
      return {
        prId: event.prId,
        astMutationId: event.astMutationId,
        blastRadiusHash: event.blastRadiusHash,
        challenge: event.challenge,
        audio: null,
        verdict: null,
        errorMessage: null,
      };
    }),
    onCapture: assign(({ event }) => {
      if (event.type !== 'CAPTURE_DONE') {
        return {};
      }
      return { audio: event.audio };
    }),
    onVerdict: assign(({ event }) => {
      if (event.type !== 'RESULT_OK' && event.type !== 'RESULT_REJECTED') {
        return {};
      }
      return { verdict: event.verdict };
    }),
    onError: assign(({ event }) => {
      if (event.type !== 'ERROR' && event.type !== 'MIC_DENIED') {
        return {};
      }
      return { errorMessage: event.message };
    }),
    reset: assign(() => ({ ...INITIAL_CONTEXT })),
  },
}).createMachine({
  id: 'biometricAuth',
  initial: 'IDLE',
  context: INITIAL_CONTEXT,
  states: {
    IDLE: {
      on: {
        OPEN: {
          target: 'AWAITING_MIC_PERMISSION',
          actions: 'onOpen',
        },
      },
    },
    AWAITING_MIC_PERMISSION: {
      on: {
        MIC_GRANTED: { target: 'CAPTURING_AUDIO' },
        MIC_DENIED: { target: 'ERROR', actions: 'onError' },
        ERROR: { target: 'ERROR', actions: 'onError' },
        RESET: { target: 'IDLE', actions: 'reset' },
      },
    },
    CAPTURING_AUDIO: {
      on: {
        CAPTURE_DONE: {
          target: 'PROCESSING_EMBEDDING',
          actions: 'onCapture',
        },
        ERROR: { target: 'ERROR', actions: 'onError' },
        RESET: { target: 'IDLE', actions: 'reset' },
      },
    },
    PROCESSING_EMBEDDING: {
      on: {
        // SUBMIT is accepted but inert here (the POST is in-flight); it
        // exists so the orchestrator can signal intent without an illegal
        // transition. Guarded on hasAudio so it can never fire bufferless.
        SUBMIT: { guard: 'hasAudio' },
        RESULT_OK: {
          guard: 'hasAudio',
          target: 'AUTHORIZED',
          actions: 'onVerdict',
        },
        RESULT_REJECTED: {
          guard: 'hasAudio',
          target: 'REJECTED',
          actions: 'onVerdict',
        },
        ERROR: { target: 'ERROR', actions: 'onError' },
      },
    },
    AUTHORIZED: {
      on: {
        RESET: { target: 'IDLE', actions: 'reset' },
      },
    },
    REJECTED: {
      on: {
        // RESET discards the consumed nonce; the orchestrator must fetch a
        // FRESH challenge before re-OPENing (single-use nonce respected).
        RESET: { target: 'IDLE', actions: 'reset' },
      },
    },
    ERROR: {
      on: {
        RESET: { target: 'IDLE', actions: 'reset' },
      },
    },
  },
});

export type AuthMachine = typeof authMachine;
