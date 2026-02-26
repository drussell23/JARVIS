/**
 * StartupGate.js - Backend Startup Progress Gate
 * ================================================
 *
 * Gates the React app behind a loading overlay that polls the backend's
 * actual startup progress. The loading page (loading.html) served by the
 * loading server handles the INITIAL startup experience; this component
 * handles the case where the React app loads before the backend is 100%
 * ready, ensuring accurate progress reflection and a smooth Matrix
 * transition when startup completes.
 *
 * Architecture:
 *   loading_server (/api/startup-progress) → StartupGate (poll) → overlay/gate
 *   unified_supervisor (_current_progress) → loading_server → here
 *
 * The progress shown here is the SAME value the CLI terminal displays.
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import MatrixBackground from './MatrixBackground';

const LOADING_SERVER_PORT = window.JARVIS_LOADING_SERVER_PORT || 8080;
const BACKEND_PORT = process.env.REACT_APP_BACKEND_PORT || 8010;
const POLL_INTERVAL_MS = 1500;
const TRANSITION_DURATION_MS = 2000;

/**
 * Attempts to fetch startup progress from available endpoints.
 * Tries loading server first (authoritative), falls back to backend health.
 */
async function fetchProgress(hostname) {
  // Try loading server first (authoritative source during startup)
  const candidates = [
    { url: `http://${hostname}:${LOADING_SERVER_PORT}/api/startup-progress`, type: 'loading_server' },
    { url: `http://${hostname}:${BACKEND_PORT}/health`, type: 'backend_health' },
  ];

  for (const candidate of candidates) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 3000);
      const resp = await fetch(candidate.url, {
        cache: 'no-cache',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (!resp.ok) continue;
      const data = await resp.json();

      if (candidate.type === 'loading_server') {
        // v271.2: Extract completion mode flags for degraded completion handling
        const completionMode = data.completion_mode || {};
        return {
          progress: typeof data.progress === 'number' ? data.progress : 0,
          stage: data.stage || 'unknown',
          message: data.message || '',
          isReady: data.progress >= 100 || data.stage === 'complete',
          source: 'loading_server',
          eta: data.predictive_eta?.eta_seconds || null,
          apiOnly: completionMode.api_only || completionMode.frontend_failed || false,
          frontendTimeout: completionMode.frontend_timeout || false,
        };
      }

      if (candidate.type === 'backend_health') {
        const isHealthy = data.status === 'healthy' || data.status === 'ok';
        return {
          progress: isHealthy ? 100 : 50,
          stage: isHealthy ? 'complete' : 'starting',
          message: isHealthy ? 'Backend ready' : 'Backend starting...',
          isReady: isHealthy,
          source: 'backend_health',
          eta: null,
        };
      }
    } catch {
      // Try next candidate
    }
  }

  return null;
}

const StartupGate = ({ children }) => {
  const [progress, setProgress] = useState(0);
  const [stage, setStage] = useState('connecting');
  const [message, setMessage] = useState('Connecting to JARVIS...');
  const [isReady, setIsReady] = useState(false);
  const [transitioning, setTransitioning] = useState(false);
  const [showContent, setShowContent] = useState(false);
  const [eta, setEta] = useState(null);
  const pollRef = useRef(null);
  const maxProgressRef = useRef(0);
  const hostname = window.location.hostname || 'localhost';

  // Check if backend was already ready on mount (fast path)
  const [initialCheckDone, setInitialCheckDone] = useState(false);

  const pollProgress = useCallback(async () => {
    const result = await fetchProgress(hostname);
    if (!result) return;

    // Monotonic progress — never decrease
    const effectiveProgress = Math.max(maxProgressRef.current, result.progress);
    maxProgressRef.current = effectiveProgress;

    setProgress(effectiveProgress);
    setStage(result.stage);
    setMessage(result.message);
    setEta(result.eta);

    if (result.isReady || effectiveProgress >= 100) {
      setProgress(100);
      setIsReady(true);
    }
  }, [hostname]);

  // Initial check — if backend is already ready, skip the gate entirely
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const result = await fetchProgress(hostname);
      if (cancelled) return;
      if (result?.isReady) {
        setProgress(100);
        setIsReady(true);
        setShowContent(true);
        setInitialCheckDone(true);
        return;
      }
      setInitialCheckDone(true);
      if (result) {
        const eff = Math.max(maxProgressRef.current, result.progress);
        maxProgressRef.current = eff;
        setProgress(eff);
        setStage(result.stage);
        setMessage(result.message);
      }
    })();
    return () => { cancelled = true; };
  }, [hostname]);

  // Start polling once initial check is done and we're not ready
  useEffect(() => {
    if (!initialCheckDone || isReady) return;

    pollRef.current = setInterval(pollProgress, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [initialCheckDone, isReady, pollProgress]);

  // Handle transition when ready
  useEffect(() => {
    if (!isReady || showContent) return;

    // Start the Matrix dissolve transition
    setTransitioning(true);

    const timer = setTimeout(() => {
      setShowContent(true);
    }, TRANSITION_DURATION_MS);

    return () => clearTimeout(timer);
  }, [isReady, showContent]);

  // If backend was already ready on first check, render children immediately
  if (showContent && !transitioning) {
    return <>{children}</>;
  }

  // If transitioning, render both with crossfade
  if (showContent && transitioning) {
    // Transition complete — just render children
    return <>{children}</>;
  }

  const progressBarWidth = `${Math.min(100, Math.max(0, progress))}%`;
  const displayProgress = Math.round(progress);

  return (
    <>
      {/* Loading overlay */}
      <div
        className={`startup-gate-overlay ${transitioning ? 'dissolving' : ''}`}
        style={{
          position: 'fixed',
          top: 0,
          left: 0,
          width: '100vw',
          height: '100vh',
          zIndex: 9999,
          background: '#000',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          transition: `opacity ${TRANSITION_DURATION_MS}ms cubic-bezier(0.4, 0, 0.2, 1)`,
          opacity: transitioning ? 0 : 1,
          pointerEvents: transitioning ? 'none' : 'auto',
        }}
      >
        {/* Matrix rain background */}
        <MatrixBackground
          opacity={transitioning ? 0.8 : 0.3}
          color="#00ff41"
          enabled={true}
          intensity={transitioning ? 'high' : 'medium'}
        />

        {/* Content container */}
        <div style={{
          position: 'relative',
          zIndex: 2,
          textAlign: 'center',
          fontFamily: "'Orbitron', 'Rajdhani', 'Courier New', monospace",
          color: '#00ff41',
          padding: '2rem',
          maxWidth: '600px',
          width: '90%',
        }}>
          {/* JARVIS title */}
          <h1 style={{
            fontSize: '3rem',
            fontWeight: 900,
            letterSpacing: '0.3em',
            textShadow: '0 0 20px rgba(0, 255, 65, 0.5), 0 0 40px rgba(0, 255, 65, 0.3)',
            marginBottom: '0.5rem',
            animation: 'gateGlow 2s ease-in-out infinite',
          }}>
            JARVIS
          </h1>

          {/* Subtitle */}
          <div style={{
            fontSize: '0.9rem',
            letterSpacing: '0.2em',
            color: 'rgba(0, 255, 65, 0.6)',
            marginBottom: '2.5rem',
            textTransform: 'uppercase',
          }}>
            {stage === 'connecting' ? 'Establishing Connection' :
             stage === 'complete' ? 'System Ready' :
             'Initializing Systems'}
          </div>


          {/* Progress percentage - large display */}
          <div style={{
            fontSize: '4rem',
            fontWeight: 700,
            textShadow: '0 0 30px rgba(0, 255, 65, 0.6)',
            marginBottom: '1rem',
            fontVariantNumeric: 'tabular-nums',
            transition: 'all 0.5s ease',
          }}>
            {displayProgress}%
          </div>

          {/* Progress bar container */}
          <div style={{
            width: '100%',
            height: '4px',
            background: 'rgba(0, 255, 65, 0.1)',
            borderRadius: '2px',
            overflow: 'hidden',
            marginBottom: '1.5rem',
            border: '1px solid rgba(0, 255, 65, 0.2)',
          }}>
            <div style={{
              width: progressBarWidth,
              height: '100%',
              background: 'linear-gradient(90deg, #00ff41, #00cc33)',
              borderRadius: '2px',
              transition: 'width 0.8s cubic-bezier(0.4, 0, 0.2, 1)',
              boxShadow: '0 0 10px rgba(0, 255, 65, 0.5)',
            }} />
          </div>

          {/* Status message */}
          <div style={{
            fontSize: '0.85rem',
            color: 'rgba(0, 255, 65, 0.7)',
            letterSpacing: '0.05em',
            minHeight: '1.2em',
          }}>
            {message}
          </div>

          {/* ETA display */}
          {eta && eta > 0 && (
            <div style={{
              fontSize: '0.75rem',
              color: 'rgba(0, 255, 65, 0.4)',
              marginTop: '0.5rem',
              letterSpacing: '0.05em',
            }}>
              ~{Math.ceil(eta)}s remaining
            </div>
          )}
        </div>
      </div>

      {/* Inline styles for animations */}
      <style>{`
        @keyframes gateGlow {
          0%, 100% { text-shadow: 0 0 20px rgba(0, 255, 65, 0.5), 0 0 40px rgba(0, 255, 65, 0.3); }
          50% { text-shadow: 0 0 30px rgba(0, 255, 65, 0.8), 0 0 60px rgba(0, 255, 65, 0.5); }
        }
        .startup-gate-overlay.dissolving {
          animation: matrixDissolve ${TRANSITION_DURATION_MS}ms forwards;
        }
        @keyframes matrixDissolve {
          0% { opacity: 1; filter: blur(0px) brightness(1); }
          30% { opacity: 0.9; filter: blur(0px) brightness(1.5); }
          60% { opacity: 0.5; filter: blur(2px) brightness(2); }
          100% { opacity: 0; filter: blur(8px) brightness(3); }
        }
      `}</style>

      {/* Pre-render children behind the overlay so they're ready when overlay dissolves */}
      {transitioning && (
        <div style={{ position: 'fixed', top: 0, left: 0, width: '100%', height: '100%', zIndex: 1 }}>
          {children}
        </div>
      )}
    </>
  );
};

export default StartupGate;
