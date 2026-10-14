/**
 * voicePersona - the browser speaks as JARVIS or not at all.
 *
 * JARVIS's voice is declared once on the backend (backend/core/voice_persona,
 * served at /audio/persona). When the page has to speak by itself (backend
 * TTS failed), it chooses by the same rule as every server-side engine:
 * the declared name; else a voice in the persona's locale whose name states
 * the persona's gender (browser voices carry no gender field); else none.
 * A fixed list of names here picked whoever the OS happened to have.
 */

let personaPromise = null;

/** The declared persona: {name, locale, gender, enforce}. Fetched once. */
export function getVoicePersona(apiUrl) {
  if (!personaPromise) {
    personaPromise = fetch(`${apiUrl}/audio/persona`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .catch((err) => {
        personaPromise = null; // a later call may reach a backend that was not up yet
        throw err;
      });
  }
  return personaPromise;
}

const norm = (value) => (value || '').trim().replace(/_/g, '-').toLowerCase();

const wordIn = (word, text) =>
  Boolean(word) && new RegExp(`\\b${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(text);

/** The SpeechSynthesisVoice that renders *persona*, or null. */
export function choosePersonaVoice(voices, persona) {
  if (!persona || !voices || !voices.length) return null;
  const exact = voices.find((v) => wordIn(persona.name, v.name));
  if (exact) return exact;
  if (!persona.locale) return null;
  const sameLocale = voices.filter((v) => norm(v.lang) === norm(persona.locale));
  if (!persona.gender) return sameLocale[0] || null;
  return sameLocale.find((v) => wordIn(persona.gender, v.name)) || null;
}
