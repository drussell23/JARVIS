'use client';

/**
 * ConnectionStatus -- a small live indicator of the SSE connection
 * state (connecting / connected / reconnecting / error / polling /
 * closed), plus the current Last-Event-ID for operator visibility.
 */

import { ConnectionState } from '../hooks/useSovereignStream';

const STATE_COLOR: Record<ConnectionState, string> = {
  disconnected: '#6b7280',
  connecting: '#ca8a04',
  connected: '#16a34a',
  reconnecting: '#ea580c',
  error: '#dc2626',
  closed: '#6b7280',
  polling: '#0891b2',
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
