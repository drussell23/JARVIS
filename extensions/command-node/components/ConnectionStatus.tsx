'use client';

/**
 * ConnectionStatus -- a small live indicator of the SSE connection
 * state (connecting / connected / reconnecting / error / polling /
 * closed), plus the current Last-Event-ID for operator visibility.
 */

import { ConnectionState } from '../hooks/useSovereignStream';
import { STATE } from '../lib/tokens';

const STATE_COLOR: Record<ConnectionState, string> = {
  disconnected: STATE.pending,
  connecting: STATE.warn,
  connected: STATE.ok,
  reconnecting: STATE.attention,
  error: STATE.danger,
  closed: STATE.pending,
  polling: STATE.applied,
};

const STATE_LABEL: Record<ConnectionState, string> = {
  disconnected: 'Disconnected',
  connecting: 'Connecting',
  connected: 'Live',
  reconnecting: 'Reconnecting',
  error: 'Error',
  closed: 'Closed',
  polling: 'Poll fallback',
};

export interface ConnectionStatusProps {
  readonly state: ConnectionState;
  readonly lastEventId: string | null;
}

export function ConnectionStatus({
  state,
  lastEventId,
}: ConnectionStatusProps): JSX.Element {
  return (
    <div className="conn-status" data-testid="conn-status" data-state={state}>
      <span
        className="conn-dot"
        style={{ backgroundColor: STATE_COLOR[state] }}
        aria-hidden="true"
      />
      <span className="conn-label">{STATE_LABEL[state]}</span>
      {lastEventId ? (
        <span className="conn-eid mono" title="Last-Event-ID">
          {lastEventId}
        </span>
      ) : null}
    </div>
  );
}

export default ConnectionStatus;
