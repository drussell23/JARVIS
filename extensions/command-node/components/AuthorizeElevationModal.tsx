'use client';

/**
 * AuthorizeElevationModal -- the biometric write-path UI.
 *
 * Every surface is LOCKED to the FSM state (from useBiometricAuth): the
 * action button, the status line, the glow, and the verdict copy all switch
 * on the machine's current state. There is no boolean soup -- the modal
 * reads a single `state` value and renders the matching view.
 *
 * Styling pulls exclusively from the Sovereign design tokens (military-grade
 * console chrome): neon-glow on the active state, --state-danger glow on
 * REJECTED, --state-ok glow on AUTHORIZED, --state-attention (orange) glow
 * for the Immutable Orange law. The challenge PHRASE is the prominent
 * element -- the operator must speak THIS phrase.
 *
 * The frontend NEVER decides authorization. AUTHORIZED / REJECTED reflect
 * the backend verdict carried by the FSM; this component only renders it.
 */

import { useEffect } from 'react';
import type { AuthStateValue } from '../lib/authFsm';
import type { BiometricAuthApi } from '../hooks/useBiometricAuth';

export interface AuthorizeElevationModalProps {
  /** The orchestrating hook -- the modal is a pure view over it. */
  readonly auth: BiometricAuthApi;
  /** The PR being authorized (for the header). */
  readonly prId: string;
  readonly targetRepo?: string;
  /** Close the modal entirely (parent unmounts / hides it). */
  readonly onClose: () => void;
}

/** Per-state operator-facing copy for the status line. */
const STATE_COPY: Record<AuthStateValue, string> = {
  IDLE: 'Initializing secure challenge...',
  AWAITING_MIC_PERMISSION: 'Awaiting microphone permission...',
  CAPTURING_AUDIO: 'Listening -- speak the challenge phrase now.',
  PROCESSING_EMBEDDING: 'Verifying voice-print + anti-spoof + freshness...',
  AUTHORIZED: 'Operator authorization recorded.',
  REJECTED: 'Authorization refused.',
  ERROR: 'The authorization could not be completed.',
};

const ACTIVE_STATES: ReadonlySet<AuthStateValue> = new Set([
  'AWAITING_MIC_PERMISSION',
  'CAPTURING_AUDIO',
  'PROCESSING_EMBEDDING',
]);

function stateTone(state: AuthStateValue): 'ok' | 'danger' | 'neutral' {
  if (state === 'AUTHORIZED') {
    return 'ok';
  }
  if (state === 'REJECTED' || state === 'ERROR') {
    return 'danger';
  }
  return 'neutral';
}

export function AuthorizeElevationModal({
  auth,
  prId,
  targetRepo,
  onClose,
}: AuthorizeElevationModalProps): JSX.Element {
  const { state, verdict, phrase, errorMessage, disabled, immutableOrange } =
    auth;

  // Esc closes the modal (cancels the flow). The mic is released by
  // captureAuthAudio's finally regardless; closing just unmounts the view.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const tone = stateTone(state);
  const isActive = ACTIVE_STATES.has(state);

  return (
    <div
      className="auth-scrim"
      role="presentation"
      data-testid="auth-scrim"
      onClick={onClose}
    >
      <div
        className="auth-modal"
        role="dialog"
        aria-modal="true"
        aria-label="authorize critical elevation"
        data-testid="auth-modal"
        data-state={state}
        data-immutable-orange={immutableOrange ? 'true' : 'false'}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="auth-head">
          <h2 className="auth-title">Critical Elevation -- Biometric</h2>
          <span className="auth-pr mono">{prId}</span>
        </header>

        <div className="auth-state-line" data-testid="auth-state-line">
          <span
            className="auth-state-dot"
            data-active={isActive ? 'true' : 'false'}
            data-tone={tone}
            aria-hidden="true"
          />
          {state === 'PROCESSING_EMBEDDING' ? (
            <span className="auth-spinner" aria-hidden="true" />
          ) : null}
          <span data-testid="auth-state-copy">{STATE_COPY[state]}</span>
        </div>

        {disabled ? (
          <DisabledBody />
        ) : (
          <FlowBody
            state={state}
            phrase={phrase}
            prId={prId}
            targetRepo={targetRepo}
            errorMessage={errorMessage}
            verdict={verdict}
            immutableOrange={immutableOrange}
          />
        )}

        <ModalActions auth={auth} state={state} disabled={disabled}
          onClose={onClose} />
      </div>
    </div>
  );
}

function DisabledBody(): JSX.Element {
  return (
    <p className="auth-body" data-testid="auth-disabled">
      The biometric write-path is currently <strong>disabled</strong> on this
      node ({'JARVIS_COMMAND_NODE_AUTH_ENABLED'} is off). Authorization falls
      back to the CLI approval path. No write is possible from this surface
      until the gate is enabled.
    </p>
  );
}

interface FlowBodyProps {
  readonly state: AuthStateValue;
  readonly phrase: string | null;
  readonly prId: string;
  readonly targetRepo?: string;
  readonly errorMessage: string | null;
  readonly verdict: BiometricAuthApi['verdict'];
  readonly immutableOrange: boolean;
}

function FlowBody({
  state,
  phrase,
  targetRepo,
  errorMessage,
  verdict,
  immutableOrange,
}: FlowBodyProps): JSX.Element {
  return (
    <>
      {phrase !== null ? (
        <section
          className="auth-phrase-block"
          data-testid="auth-phrase-block"
          aria-label="challenge phrase"
        >
          <div className="auth-phrase-label">Speak this phrase</div>
          <div className="auth-phrase mono" data-testid="auth-phrase">
            {phrase}
          </div>
        </section>
      ) : null}

      {targetRepo ? (
        <dl className="auth-meta" data-testid="auth-meta">
          <dt>Target repo</dt>
          <dd className="mono">{targetRepo}</dd>
        </dl>
      ) : null}

      {state === 'ERROR' ? (
        <div className="auth-verdict" data-tone="danger"
          data-testid="auth-error">
          <div className="auth-verdict-title">Error</div>
          <p>{errorMessage ?? 'Unknown error.'}</p>
          <p className="muted">
            Fail-CLOSED: nothing was authorized. Retry fetches a fresh
            challenge.
          </p>
        </div>
      ) : null}

      {verdict !== null && state === 'AUTHORIZED' ? (
        <div className="auth-verdict" data-tone="ok"
          data-testid="auth-authorized">
          <div className="auth-verdict-title">Authorized</div>
          <p>
            Operator approval recorded for the elevation. The backend
            CRITICAL_ELEVATION path proceeds under the Immutable Orange floor.
          </p>
          <VerdictScores verdict={verdict} />
        </div>
      ) : null}

      {verdict !== null && state === 'REJECTED' ? (
        immutableOrange ? (
          <ImmutableOrangeBody verdict={verdict} />
        ) : (
          <div className="auth-verdict" data-tone="danger"
            data-testid="auth-rejected">
            <div className="auth-verdict-title">Rejected</div>
            <p data-testid="auth-reject-reason">{verdict.reason}</p>
            <p className="muted">
              Biometric mismatch, anti-spoof, or freshness check failed. A
              retry issues a brand-new single-use challenge.
            </p>
            <VerdictScores verdict={verdict} />
          </div>
        )
      ) : null}
    </>
  );
}

/**
 * The Immutable Orange law copy. This is NOT a biometric failure -- a
 * Mind/Nerves (prime/reactor) PR is PERMANENTLY human-merge-only by
 * Sovereign Law. A valid voice-print can never make it auto-merge. The
 * visual is distinct (orange/attention, not danger-red) so the operator
 * reads it as governance, not a mismatch.
 */
function ImmutableOrangeBody({
  verdict,
}: {
  readonly verdict: NonNullable<BiometricAuthApi['verdict']>;
}): JSX.Element {
  return (
    <div
      className="auth-verdict"
      data-tone="orange"
      data-testid="auth-immutable-orange"
    >
      <div className="auth-verdict-title">Immutable Orange -- Sovereign Law</div>
      <p>
        This is <strong>not</strong> a biometric failure. The target repo
        (<span className="mono">{verdict.target_repo}</span> -- the Mind or
        Nerves) is <strong>permanently human-merge-only</strong> by Sovereign
        Law. No biometric, however valid, can authorize an auto-merge of a
        Mind/Nerves PR.
      </p>
      <p className="muted">
        Your voice-print verified -- the law composes on top of it. This PR
        must be merged by a human through the normal review path; the
        Command Node cannot lift this floor.
      </p>
    </div>
  );
}

function VerdictScores({
  verdict,
}: {
  readonly verdict: NonNullable<BiometricAuthApi['verdict']>;
}): JSX.Element {
  return (
    <dl className="auth-meta" data-testid="auth-scores">
      <dt>ecapa score</dt>
      <dd className="auth-score">{verdict.ecapa_score.toFixed(4)}</dd>
      <dt>anti-spoof</dt>
      <dd className="auth-score">{verdict.antispoof_ok ? 'pass' : 'fail'}</dd>
      <dt>freshness</dt>
      <dd className="auth-score">{verdict.freshness_ok ? 'ok' : 'stale'}</dd>
    </dl>
  );
}

function ModalActions({
  auth,
  state,
  disabled,
  onClose,
}: {
  readonly auth: BiometricAuthApi;
  readonly state: AuthStateValue;
  readonly disabled: boolean;
  readonly onClose: () => void;
}): JSX.Element {
  const terminal =
    state === 'AUTHORIZED' || state === 'REJECTED' || state === 'ERROR';

  return (
    <div className="auth-actions">
      <button className="btn" onClick={onClose} data-testid="auth-close">
        Close
      </button>
      {disabled ? null : terminal && state !== 'AUTHORIZED' ? (
        <button
          className="btn btn-primary"
          data-testid="auth-retry"
          onClick={() => void auth.retry()}
        >
          Retry (fresh challenge)
        </button>
      ) : null}
    </div>
  );
}

export default AuthorizeElevationModal;
