/**
 * VoiceToggle — Slice 110
 * =======================
 * The ONLY write control in the command center, and deliberately COSMETIC:
 * mute/unmute Karen's voice. It POSTs through the gateway's loopback-only,
 * JARVIS_KAREN_VOICE_ENABLED-gated voice endpoint, which routes to the
 * sanctioned karen_voice_command_router env seam. It touches NOTHING
 * authority-bearing (no FSM / governance / graduation) — those are read-only
 * over the web by design (§1 sovereignty, fail-closed).
 *
 * Props: client (ObservabilityClient), health ({ voice_enabled, ... })
 */

import React, { useState } from 'react';
import { Volume2, VolumeX, AlertTriangle } from 'lucide-react';

export default function VoiceToggle({ client, health }) {
  const [muted, setMuted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const voiceEnabled = !!health?.voice_enabled;

  const toggle = async () => {
    if (!client || busy) return;
    setBusy(true);
    setNote('');
    try {
      const action = muted ? 'unmute' : 'mute';
      const res = await client.voice(action);
      if (res?.ok) {
        setMuted(!muted);
        setNote(res.spoken || '');
      } else {
        setNote(res?.detail || res?.error || 'voice control unavailable');
      }
    } catch (e) {
      setNote('voice control failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cc-panel cc-voice">
      <div className="cc-panel-title">KAREN · VOICE</div>
      <button
        className={`cc-voice-btn ${muted ? 'cc-muted' : 'cc-live'}`}
        onClick={toggle}
        disabled={busy || !voiceEnabled}
        title={voiceEnabled ? 'Cosmetic mute/unmute' : 'Set JARVIS_KAREN_VOICE_ENABLED=1'}
      >
        {muted ? <VolumeX size={18} /> : <Volume2 size={18} />}
        <span>{muted ? 'MUTED' : 'LIVE'}</span>
      </button>
      <div className="cc-voice-state">
        {voiceEnabled
          ? <span className="cc-ok">voice channel enabled</span>
          : <span className="cc-warn"><AlertTriangle size={12} /> channel off</span>}
      </div>
      {note ? <div className="cc-voice-note">{note}</div> : null}
      <div className="cc-voice-disclaimer">
        cosmetic only · FSM &amp; governance require the operator REPL
      </div>
    </div>
  );
}
