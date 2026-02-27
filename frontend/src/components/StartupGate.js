/**
 * StartupGate.js - Invisible Backend Readiness Gate
 * ==================================================
 *
 * Pure logic gate — no visual UI. The reactor core loading page (loading.html
 * on port 8080) is the ONLY loading experience. This component handles routing:
 *
 * - If `jarvis_ready=1` URL param present → backend was verified by loading page,
 *   render children (with brief retry if backend momentarily unreachable)
 * - If backend is ready on first check → render children immediately
 * - If backend is NOT ready and no readiness param → redirect to loading server
 * - While checking → render null (black screen via body { background: #000 })
 */

import React, { useState, useEffect, useRef } from 'react';

const LOADING_SERVER_PORT = window.JARVIS_LOADING_SERVER_PORT || 8080;
const BACKEND_PORT = process.env.REACT_APP_BACKEND_PORT || 8010;
const MAX_READY_RETRIES = 5;
const RETRY_DELAY_MS = 1000;

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

const StartupGate = ({ children }) => {
  const [ready, setReady] = useState(false);
  const [checked, setChecked] = useState(false);
  const retryCount = useRef(0);
  const hostname = window.location.hostname || 'localhost';

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

      // No readiness param and backend not ready — redirect to loading server
      if (!cancelled) {
        const loadingUrl = `http://${hostname}:${LOADING_SERVER_PORT}/`;
        console.log(`[StartupGate] Backend not ready, redirecting to loading page: ${loadingUrl}`);
        window.location.href = loadingUrl;
      }
    }

    gate();
    return () => { cancelled = true; };
  }, [hostname]);

  // While checking: render nothing (body background is #000)
  if (!checked || !ready) {
    return null;
  }

  return <>{children}</>;
};

export default StartupGate;
