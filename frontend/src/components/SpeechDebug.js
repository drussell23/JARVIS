/**
 * Speech Debug Panel v2.0
 * =======================
 * Advanced debugging component with:
 * - Dynamic endpoint configuration
 * - Real-time connection status
 * - Audio context state monitoring
 * - Voice synthesis testing
 * - Backend speech API testing
 */

import React, { useState, useEffect, useCallback } from 'react';
import configService, {
  getBackendState,
  onConfigReady
} from '../services/DynamicConfigService';

const SpeechDebug = () => {
  const [voices, setVoices] = useState([]);
  const [selectedVoice, setSelectedVoice] = useState(null);
  const [status, setStatus] = useState('Loading...');
  const [volume, setVolume] = useState(1.0);
  const [rate, setRate] = useState(1.0);
  const [apiUrl, setApiUrl] = useState(null);
  const [backendStatus, setBackendStatus] = useState('unknown');
  const [audioContextState, setAudioContextState] = useState('unknown');
  const [testHistory, setTestHistory] = useState([]);

  // Get API URL dynamically
  const getApiUrlAsync = useCallback(async () => {
    try {
      await configService.waitForConfig(5000);
      const url = configService.getApiUrl();
      setApiUrl(url);
      return url;
    } catch {
      // Fallback
      const hostname = window.location.hostname || 'localhost';
      const protocol = window.location.protocol.replace(':', '');
      const fallback = `${protocol}://${hostname}:8000`;
      setApiUrl(fallback);
      return fallback;
    }
  }, []);

  useEffect(() => {
    // Check if speech synthesis is available
    if (!('speechSynthesis' in window)) {
      setStatus('Speech synthesis not supported');
      return;
    }

    const loadVoices = () => {
      const availableVoices = speechSynthesis.getVoices();
      console.log('[SpeechDebug] Loaded voices:', availableVoices.length);
      setVoices(availableVoices);

      if (availableVoices.length > 0) {
        setStatus(`${availableVoices.length} voices available`);

        // Find best English voice (prefer Daniel for JARVIS)
        const danielVoice = availableVoices.find(v =>
          v.name.toLowerCase().includes('daniel')
        );
        const englishVoice = availableVoices.find(v =>
          v.lang.startsWith('en') && v.localService
        );

        setSelectedVoice(danielVoice || englishVoice || availableVoices[0]);
      } else {
        setStatus('Loading voices...');
      }
    };

    // Load immediately
    loadVoices();

    // Listen for voice changes (Chrome requirement)
    speechSynthesis.onvoiceschanged = loadVoices;

    // Check audio context state
    if ('AudioContext' in window || 'webkitAudioContext' in window) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      const audioContext = new AudioContextClass();
      setAudioContextState(audioContext.state);

      if (audioContext.state === 'suspended') {
        setStatus(prev => prev + ' (Audio may be blocked - click to enable)');
      }

      // Cleanup
      return () => {
        audioContext.close();
        speechSynthesis.cancel();
      };
    }

    return () => {
      speechSynthesis.cancel();
    };
  }, []);

  // Initialize API URL and check backend
  useEffect(() => {
    const init = async () => {
      await getApiUrlAsync();

      // Subscribe to config updates
      onConfigReady(() => {
        const url = configService.getApiUrl();
        setApiUrl(url);
        checkBackendStatus(url);
      });
    };

    init();
  }, [getApiUrlAsync]);

  // Check backend status
  const checkBackendStatus = async (url = apiUrl) => {
    if (!url) return;

    try {
      const response = await fetch(`${url}/health/ping`, {
        method: 'GET',
        signal: AbortSignal.timeout(3000)
      });

      if (response.ok) {
        setBackendStatus('online');
      } else {
        setBackendStatus('error');
      }
    } catch (error) {
      setBackendStatus('offline');
    }
  };

  const addToHistory = (type, message, success) => {
    setTestHistory(prev => [{
      type,
      message,
      success,
      timestamp: new Date().toISOString()
    }, ...prev.slice(0, 9)]);
  };

  const testSpeech = (text = "JARVIS speech test. Full autonomy activated. All systems online.") => {
    console.log('[SpeechDebug] Testing speech:', text);
    console.log('[SpeechDebug] Selected voice:', selectedVoice?.name);

    // Cancel any ongoing speech
    speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);

    if (selectedVoice) {
      utterance.voice = selectedVoice;
    }

    utterance.volume = volume;
    utterance.rate = rate;
    utterance.pitch = 1.0;

    utterance.onstart = () => {
      console.log('[SpeechDebug] Speech started');
      setStatus('Speaking...');
    };

    utterance.onend = () => {
      console.log('[SpeechDebug] Speech ended');
      setStatus('Speech completed');
      addToHistory('browser', 'Speech synthesis completed', true);
    };

    utterance.onerror = (event) => {
      console.error('[SpeechDebug] Speech error:', event);
      setStatus(`Speech error: ${event.error}`);
      addToHistory('browser', `Error: ${event.error}`, false);
    };

    try {
      speechSynthesis.speak(utterance);
      console.log('[SpeechDebug] Speech queued');
    } catch (error) {
      console.error('[SpeechDebug] Failed to speak:', error);
      setStatus(`Failed: ${error.message}`);
      addToHistory('browser', `Failed: ${error.message}`, false);
    }
  };

  const testBackendSpeech = async () => {
    const url = apiUrl || await getApiUrlAsync();

    try {
      setStatus('Sending to backend...');

      const response = await fetch(`${url}/voice/jarvis/speak`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          text: "Backend speech test. JARVIS systems online."
        }),
        signal: AbortSignal.timeout(10000)
      });

      if (response.ok) {
        setStatus('Backend speech request sent');
        setBackendStatus('online');
        addToHistory('backend', 'Speech request successful', true);
      } else {
        const errorText = await response.text();
        setStatus(`Backend error: ${response.status}`);
        addToHistory('backend', `Error ${response.status}: ${errorText.slice(0, 50)}`, false);
      }
    } catch (error) {
      setStatus(`Backend unreachable: ${error.message}`);
      setBackendStatus('offline');
      addToHistory('backend', `Unreachable: ${error.message}`, false);
    }
  };

  const testHealthEndpoint = async () => {
    const url = apiUrl || await getApiUrlAsync();

    try {
      const response = await fetch(`${url}/health`, {
        signal: AbortSignal.timeout(3000)
      });

      if (response.ok) {
        const data = await response.json();
        setBackendStatus('online');
        addToHistory('health', `Status: ${data.status || 'ok'}`, true);
        return data;
      } else {
        setBackendStatus('error');
        addToHistory('health', `Error: ${response.status}`, false);
      }
    } catch (error) {
      setBackendStatus('offline');
      addToHistory('health', `Failed: ${error.message}`, false);
    }
  };

  const resumeAudioContext = async () => {
    try {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      const audioContext = new AudioContextClass();

      if (audioContext.state === 'suspended') {
        await audioContext.resume();
        setAudioContextState(audioContext.state);
        setStatus('Audio context resumed');
        addToHistory('audio', 'Context resumed', true);
      }
    } catch (error) {
      addToHistory('audio', `Resume failed: ${error.message}`, false);
    }
  };

  const panelStyle = {
    position: 'fixed',
    bottom: '20px',
    left: '20px',
    background: 'rgba(0, 0, 0, 0.95)',
    color: '#fff',
    padding: '16px',
    borderRadius: '12px',
    maxWidth: '380px',
    maxHeight: '500px',
    overflow: 'auto',
    zIndex: 10000,
    fontFamily: 'SF Mono, Monaco, monospace',
    fontSize: '11px',
    boxShadow: '0 4px 20px rgba(0,0,0,0.5)',
    border: '1px solid rgba(255, 215, 0, 0.3)'
  };

  const headerStyle = {
    margin: '0 0 12px 0',
    color: '#ffd700',
    fontSize: '14px',
    fontWeight: '600'
  };

  const statusBadgeStyle = (isOnline) => ({
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: '4px',
    fontSize: '10px',
    fontWeight: '600',
    background: isOnline ? 'rgba(76, 175, 80, 0.3)' : 'rgba(244, 67, 54, 0.3)',
    color: isOnline ? '#4CAF50' : '#f44336',
    marginLeft: '8px'
  });

  const buttonStyle = (color) => ({
    padding: '6px 12px',
    background: color,
    color: color === '#ffd700' ? '#000' : '#fff',
    border: 'none',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '11px',
    fontWeight: '500',
    transition: 'opacity 0.2s'
  });

  const sliderStyle = {
    width: '100%',
    marginTop: '4px',
    accentColor: '#ffd700'
  };

  return (
    <div style={panelStyle}>
      <h3 style={headerStyle}>
        Speech Debug Panel
        <span style={statusBadgeStyle(backendStatus === 'online')}>
          {backendStatus.toUpperCase()}
        </span>
      </h3>

      {/* Status Display */}
      <div style={{ marginBottom: '12px', padding: '8px', background: 'rgba(255,255,255,0.05)', borderRadius: '6px' }}>
        <strong>Status:</strong> {status}
      </div>

      {/* Connection Info */}
      <div style={{ marginBottom: '12px', fontSize: '10px', opacity: 0.8 }}>
        <div><strong>API:</strong> {apiUrl || 'Discovering...'}</div>
        <div><strong>Audio Context:</strong> {audioContextState}</div>
        <div><strong>Voices:</strong> {voices.length} available</div>
        {selectedVoice && (
          <div><strong>Selected:</strong> {selectedVoice.name} ({selectedVoice.lang})</div>
        )}
      </div>

      {/* Volume Control */}
      <div style={{ marginBottom: '10px' }}>
        <label>
          Volume: {Math.round(volume * 100)}%
          <input
            type="range"
            min="0"
            max="1"
            step="0.1"
            value={volume}
            onChange={(e) => setVolume(parseFloat(e.target.value))}
            style={sliderStyle}
          />
        </label>
      </div>

      {/* Rate Control */}
      <div style={{ marginBottom: '12px' }}>
        <label>
          Rate: {rate}x
          <input
            type="range"
            min="0.5"
            max="2"
            step="0.1"
            value={rate}
            onChange={(e) => setRate(parseFloat(e.target.value))}
            style={sliderStyle}
          />
        </label>
      </div>

      {/* Test Buttons */}
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '12px' }}>
        <button onClick={() => testSpeech()} style={buttonStyle('#ffd700')}>
          Browser Speech
        </button>
        <button onClick={() => testSpeech("Quick test")} style={buttonStyle('#4CAF50')}>
          Quick Test
        </button>
        <button onClick={testBackendSpeech} style={buttonStyle('#2196F3')}>
          Backend API
        </button>
        <button onClick={testHealthEndpoint} style={buttonStyle('#9C27B0')}>
          Health Check
        </button>
        {audioContextState === 'suspended' && (
          <button onClick={resumeAudioContext} style={buttonStyle('#FF9800')}>
            Resume Audio
          </button>
        )}
        <button onClick={() => window.location.reload()} style={buttonStyle('#f44336')}>
          Reload
        </button>
      </div>

      {/* Test History */}
      {testHistory.length > 0 && (
        <div style={{ borderTop: '1px solid rgba(255,255,255,0.1)', paddingTop: '10px' }}>
          <div style={{ fontWeight: '600', marginBottom: '6px', fontSize: '10px' }}>Recent Tests:</div>
          {testHistory.map((test, i) => (
            <div key={i} style={{
              fontSize: '9px',
              padding: '4px 6px',
              marginBottom: '4px',
              background: test.success ? 'rgba(76,175,80,0.1)' : 'rgba(244,67,54,0.1)',
              borderRadius: '4px',
              borderLeft: `2px solid ${test.success ? '#4CAF50' : '#f44336'}`
            }}>
              <span style={{ opacity: 0.6 }}>[{test.type}]</span> {test.message}
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: '10px', fontSize: '9px', opacity: 0.5 }}>
        Check browser console for detailed logs
      </div>
    </div>
  );
};

export default SpeechDebug;
