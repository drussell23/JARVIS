/**
 * StartupGate.js - Backend Readiness Gate with Visible Loading States
 * ===================================================================
 *
 * v290.0: No more invisible blank page. Shows a visible spinner while
 * checking, CORS-safe loading server probe, and a fallback UI with
 * retry when both backend and loading server are unreachable.
 *
 * Flow:
 * - If `jarvis_ready=1` URL param present -> backend was verified by loading page,
 *   render children (with brief retry if backend momentarily unreachable)
 * - If backend is ready on first check -> render children immediately
 * - If backend is NOT ready and no readiness param -> redirect to loading server
 * - While checking -> visible spinner ("Connecting to J.A.R.V.I.S...")
 * - If redirect fails after 5s -> fallback offline UI with retry button
 */

import React, { useState, useEffect, useRef } from 'react';

const LOADING_SERVER_PORT = window.JARVIS_LOADING_SERVER_PORT || 8080;
const BACKEND_PORT = process.env.REACT_APP_BACKEND_PORT || 8010;
const MAX_READY_RETRIES = 5;
const RETRY_DELAY_MS = 1000;
const REDIRECT_FALLBACK_MS = 5000;

/**
 * Check if backend is responding and healthy.
 * Returns true if backend or loading server reports ready.
 */
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

/** Inline keyframes for spinner animation */
const spinnerKeyframes = `@keyframes jarvis-gate-spin { to { transform: rotate(360deg); } }`;

/** Visible loading state shown while gate is checking */
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

/** Fallback UI when both backend and loading server are unreachable */
const OfflineFallback = ({ onRetry }) => (
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
  </div>
);

const StartupGate = ({ children }) => {
  const [ready, setReady] = useState(false);
  const [checked, setChecked] = useState(false);
  const [showFallback, setShowFallback] = useState(false);
  const retryCount = useRef(0);
  const hostname = window.location.hostname || 'localhost';
  // Incremented to re-trigger the gate effect
  const [gateTrigger, setGateTrigger] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const params = new URLSearchParams(window.location.search);
    const hasReadyParam = params.get('jarvis_ready') === '1';

    async function gate() {
      // Fast path: backend is already up
      const isReady = await checkBackendReady(hostname);
      if (cancelled) return;

      if (isReady) {
        setReady(true);
        setChecked(true);
        return;
      }

      // Loading page already verified readiness — retry briefly
      if (hasReadyParam) {
        while (retryCount.current < MAX_READY_RETRIES && !cancelled) {
          retryCount.current++;
          await new Promise(r => setTimeout(r, RETRY_DELAY_MS));
          if (cancelled) return;

          const retryReady = await checkBackendReady(hostname);
          if (cancelled) return;

          if (retryReady) {
            setReady(true);
            setChecked(true);
            return;
          }
        }

        // Exhausted retries — show app anyway; loading page already verified
        // readiness and JarvisConnectionService handles reconnection
        if (!cancelled) {
          console.warn('[StartupGate] Backend not responding after retries, showing app (loading page verified readiness)');
          setReady(true);
          setChecked(true);
        }
        return;
      }

      // No readiness param and backend not ready — try loading server
      if (!cancelled) {
        const loadingUrl = `http://${hostname}:${LOADING_SERVER_PORT}/`;

        // v290.0: CORS-safe probe before redirect
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
          // Probe failure is "unknown" — try redirect anyway
        }

        // Attempt redirect; set a fallback timer in case it fails
        const redirectTimer = setTimeout(() => {
          if (!cancelled) {
            setShowFallback(true);
            setChecked(true);
          }
        }, REDIRECT_FALLBACK_MS);

        console.log(`[StartupGate] Backend not ready, redirecting to loading page: ${loadingUrl}`);
        window.location.href = loadingUrl;

        // Clean up timer on unmount
        return () => clearTimeout(redirectTimer);
      }
    }

    gate();
    return () => { cancelled = true; };
  }, [hostname, gateTrigger]);

  // Fallback: both backend and loading server unreachable
  if (showFallback) {
    return (
      <OfflineFallback
        onRetry={() => {
          setShowFallback(false);
          setChecked(false);
          retryCount.current = 0;
          setGateTrigger(prev => prev + 1);
        }}
      />
    );
  }

  // v290.0: Visible spinner instead of blank page
  if (!checked || !ready) {
    return <LoadingIndicator />;
  }

  return <>{children}</>;
};

export default StartupGate;
