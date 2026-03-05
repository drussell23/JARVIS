/**
 * StartupGate.js - Backend Readiness Gate with Live Connection Awareness
 * ======================================================================
 *
 * v300.0: Structural rewrite. The gate now observes JarvisConnectionService's
 * live connection state instead of relying solely on a one-shot HTTP health
 * probe. This eliminates the timing race where the backend comes online via
 * WebSocket while the gate is stuck in its redirect-fallback timer.
 *
 * Architecture:
 * - Primary signal: JarvisConnectionService stateChange events (WebSocket)
 * - Secondary signal: HTTP /health poll (covers pre-WebSocket window)
 * - Tertiary signal: `jarvis_ready=1` URL param from loading page handoff
 *
 * The gate never latches into a permanent offline state. If the fallback UI
 * is shown, it auto-retries every FALLBACK_POLL_MS and also reacts instantly
 * to any JarvisConnectionService ONLINE event.
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import { getJarvisConnectionService, ConnectionState } from '../services/JarvisConnectionService';

const LOADING_SERVER_PORT = window.JARVIS_LOADING_SERVER_PORT || 8080;
const BACKEND_PORT = process.env.REACT_APP_BACKEND_PORT || 8010;
const MAX_READY_RETRIES = 5;
const RETRY_DELAY_MS = 1000;
const REDIRECT_GRACE_MS = 6000;
const FALLBACK_POLL_MS = 3000;

async function checkBackendReady(hostname) {
  const endpoints = [
    { url: `http://${hostname}:${BACKEND_PORT}/health`, type: 'backend' },
    { url: `http://${hostname}:${LOADING_SERVER_PORT}/api/startup-progress`, type: 'loading_server' },
  ];

  for (const endpoint of endpoints) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 3000);
      const resp = await fetch(endpoint.url, {
        cache: 'no-cache',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (!resp.ok) continue;
      const data = await resp.json();

      if (endpoint.type === 'backend') {
        return data.status === 'healthy' || data.status === 'ok';
      }
      if (endpoint.type === 'loading_server') {
        return data.progress >= 100 || data.stage === 'complete';
      }
    } catch {
      // Try next endpoint
    }
  }

  return false;
}

const spinnerKeyframes = `@keyframes jarvis-gate-spin { to { transform: rotate(360deg); } }`;

const LoadingIndicator = () => (
  <div style={{
    display: 'flex', justifyContent: 'center', alignItems: 'center',
    height: '100vh', background: '#000', color: '#00ff41',
    fontFamily: 'monospace', fontSize: '1.2rem',
    flexDirection: 'column', gap: '1rem',
  }}>
    <div style={{
      width: '40px', height: '40px', border: '3px solid #00ff41',
      borderTop: '3px solid transparent', borderRadius: '50%',
      animation: 'jarvis-gate-spin 1s linear infinite',
    }} />
    <div>Connecting to J.A.R.V.I.S...</div>
    <style>{spinnerKeyframes}</style>
  </div>
);

const OfflineFallback = ({ onRetry, autoRetryIn }) => (
  <div style={{
    display: 'flex', justifyContent: 'center', alignItems: 'center',
    height: '100vh', background: '#000', color: '#ccc',
    fontFamily: 'monospace', flexDirection: 'column', gap: '1.2rem',
    textAlign: 'center', padding: '2rem',
  }}>
    <div style={{ fontSize: '1.5rem', color: '#ff4444' }}>J.A.R.V.I.S. Offline</div>
    <div style={{ color: '#888', lineHeight: 1.6 }}>
      Backend and loading server are not responding.<br />
      Start JARVIS: <code style={{ color: '#00ff41' }}>python3 unified_supervisor.py</code>
    </div>
    <button
      onClick={onRetry}
      style={{
        marginTop: '0.5rem', padding: '0.6rem 1.8rem',
        background: 'transparent', border: '1px solid #00ff41',
        color: '#00ff41', fontFamily: 'monospace', fontSize: '1rem',
        cursor: 'pointer', borderRadius: '4px',
      }}
    >
      Retry Connection
    </button>
    {autoRetryIn != null && (
      <div style={{ color: '#555', fontSize: '0.85rem' }}>
        Auto-retrying in {Math.ceil(autoRetryIn / 1000)}s...
      </div>
    )}
  </div>
);

const StartupGate = ({ children }) => {
  const [ready, setReady] = useState(false);
  const [checked, setChecked] = useState(false);
  const [showFallback, setShowFallback] = useState(false);
  const [autoRetryCountdown, setAutoRetryCountdown] = useState(null);
  const retryCount = useRef(0);
  const hostname = window.location.hostname || 'localhost';
  const [gateTrigger, setGateTrigger] = useState(0);
  const cancelledRef = useRef(false);

  const openGate = useCallback(() => {
    if (!cancelledRef.current) {
      setReady(true);
      setChecked(true);
      setShowFallback(false);
      setAutoRetryCountdown(null);
    }
  }, []);

  // Live connection observer: if JarvisConnectionService goes ONLINE at
  // any point, open the gate immediately regardless of HTTP probe state.
  useEffect(() => {
    const service = getJarvisConnectionService();

    if (service.getState() === ConnectionState.ONLINE) {
      console.log('[StartupGate] JarvisConnectionService already ONLINE, opening gate');
      openGate();
      return;
    }

    const unsub = service.on('stateChange', ({ state: newState }) => {
      if (newState === ConnectionState.ONLINE) {
        console.log('[StartupGate] JarvisConnectionService went ONLINE, opening gate');
        openGate();
      }
    });

    return unsub;
  }, [openGate]);

  // Auto-retry poll when in fallback state
  useEffect(() => {
    if (!showFallback) return;

    let timer = null;
    let countdownTimer = null;
    let remaining = FALLBACK_POLL_MS;

    setAutoRetryCountdown(remaining);

    countdownTimer = setInterval(() => {
      remaining = Math.max(0, remaining - 1000);
      setAutoRetryCountdown(remaining);
    }, 1000);

    const poll = async () => {
      const service = getJarvisConnectionService();
      if (service.getState() === ConnectionState.ONLINE) {
        openGate();
        return;
      }
      const isReady = await checkBackendReady(hostname);
      if (isReady) {
        openGate();
        return;
      }
      remaining = FALLBACK_POLL_MS;
      setAutoRetryCountdown(remaining);
      timer = setTimeout(poll, FALLBACK_POLL_MS);
    };

    timer = setTimeout(poll, FALLBACK_POLL_MS);

    return () => {
      clearTimeout(timer);
      clearInterval(countdownTimer);
    };
  }, [showFallback, hostname, openGate]);

  // Main gate logic
  useEffect(() => {
    cancelledRef.current = false;

    const params = new URLSearchParams(window.location.search);
    const hasReadyParam = params.get('jarvis_ready') === '1';

    async function gate() {
      // Check 1: JarvisConnectionService already connected
      const service = getJarvisConnectionService();
      if (service.getState() === ConnectionState.ONLINE) {
        openGate();
        return;
      }

      // Check 2: HTTP health probe
      const isReady = await checkBackendReady(hostname);
      if (cancelledRef.current) return;

      if (isReady) {
        openGate();
        return;
      }

      // Check 3: Loading page handoff — retry briefly
      if (hasReadyParam) {
        while (retryCount.current < MAX_READY_RETRIES && !cancelledRef.current) {
          retryCount.current++;
          await new Promise(r => setTimeout(r, RETRY_DELAY_MS));
          if (cancelledRef.current) return;

          if (service.getState() === ConnectionState.ONLINE) {
            openGate();
            return;
          }

          const retryReady = await checkBackendReady(hostname);
          if (cancelledRef.current) return;

          if (retryReady) {
            openGate();
            return;
          }
        }

        if (!cancelledRef.current) {
          console.warn('[StartupGate] Backend not responding after retries, showing app (loading page verified readiness)');
          openGate();
        }
        return;
      }

      // No readiness param and backend not ready — try loading server redirect
      if (cancelledRef.current) return;

      const loadingUrl = `http://${hostname}:${LOADING_SERVER_PORT}/`;

      try {
        const probe = await fetch(
          `http://${hostname}:${LOADING_SERVER_PORT}/api/startup-progress`,
          { mode: 'cors', cache: 'no-cache', signal: AbortSignal.timeout(3000) }
        );
        if (probe.ok) {
          console.log('[StartupGate] Loading server responding, redirecting');
          window.location.href = loadingUrl;
          return;
        }
      } catch {
        // Loading server unreachable
      }

      // Redirect attempt + grace period with active polling
      console.log(`[StartupGate] Backend not ready, redirecting to loading page: ${loadingUrl}`);
      window.location.href = loadingUrl;

      // During the grace period, keep polling so we catch the backend
      // coming online instead of blindly waiting for the redirect timer.
      const graceEnd = Date.now() + REDIRECT_GRACE_MS;
      const gracePollMs = 1500;

      const gracePoll = async () => {
        while (Date.now() < graceEnd && !cancelledRef.current) {
          await new Promise(r => setTimeout(r, gracePollMs));
          if (cancelledRef.current) return;

          if (service.getState() === ConnectionState.ONLINE) {
            openGate();
            return;
          }

          const pollReady = await checkBackendReady(hostname);
          if (cancelledRef.current) return;
          if (pollReady) {
            openGate();
            return;
          }
        }

        // Grace period expired without success — show fallback
        if (!cancelledRef.current) {
          setShowFallback(true);
          setChecked(true);
        }
      };

      gracePoll();
    }

    gate();
    return () => { cancelledRef.current = true; };
  }, [hostname, gateTrigger, openGate]);

  if (showFallback) {
    return (
      <OfflineFallback
        autoRetryIn={autoRetryCountdown}
        onRetry={() => {
          setShowFallback(false);
          setChecked(false);
          setReady(false);
          retryCount.current = 0;
          setAutoRetryCountdown(null);
          setGateTrigger(prev => prev + 1);
        }}
      />
    );
  }

  if (!checked || !ready) {
    return <LoadingIndicator />;
  }

  return <>{children}</>;
};

export default StartupGate;
