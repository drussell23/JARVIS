/**
 * speechAudio - the audio of JARVIS's lines, fetched ahead of when it plays.
 *
 * A reply arrives a sentence at a time (backend/core/speech_segments). Each
 * sentence's audio is rendered by the backend (/audio/speak) in about a
 * second, most of it fixed per request -- fetched only when its turn came,
 * every sentence would follow a second of silence. The fetcher starts the
 * queued sentences' audio while the current one plays, and hands each to
 * the player when its turn comes.
 */

/** Text as it should be read aloud: no ellipses read as "full stop", no
 *  emoji, no markdown. One definition for every speech path. */
export function sanitizeForSpeech(rawText) {
  let cleaned = rawText || '';
  cleaned = cleaned.replace(/\.{2,}/g, ' ');        // "..." -> " "
  cleaned = cleaned.replace(/^[\s.!?,;:]+$/g, '');   // pure punctuation -> empty
  cleaned = cleaned.replace(/\.\s*$/g, '');          // trailing "." -> ""
  try {
    cleaned = cleaned.replace(/\p{Extended_Pictographic}/gu, '');
    cleaned = cleaned.replace(/‍/g, '');
    cleaned = cleaned.replace(/[︀-️]/g, '');
  } catch (_e) {
    cleaned = cleaned.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE00}-\u{FE0F}]/gu, '');
  }
  cleaned = cleaned.replace(/[*_~`#]/g, '');
  cleaned = cleaned.replace(/\s{2,}/g, ' ').trim();
  return cleaned;
}

/** Thrown when the backend could render nothing (X-TTS-Status: silent): the
 *  caller falls back to the browser's persona-matched voice or the text. */
export const TTS_SILENT = 'TTS_SILENT_FALLBACK';

/**
 * A fetcher of line audio. *apiUrl* is read per request (the backend's
 * address can change across a reconnect); *timeoutMs* bounds each request.
 *   take(text)      -- the audio Blob for *text*: fetched ahead, or now;
 *   prefetch(texts) -- start fetching what is not already on its way;
 *   abortAll()      -- a stop: every request in flight is cancelled.
 */
export function createSpeechAudioFetcher({ apiUrl, timeoutMs, fetchImpl }) {
  const doFetch = fetchImpl || ((...args) => fetch(...args));
  const pending = new Map(); // text -> {promise, controller}

  const start = (text) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const promise = (async () => {
      try {
        const response = await doFetch(`${apiUrl()}/audio/speak`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Audio generation failed: ${response.status}`);
        if (response.headers.get('X-TTS-Status') === 'silent') throw new Error(TTS_SILENT);
        return await response.blob();
      } finally {
        clearTimeout(timer);
      }
    })();
    // A prefetch nobody takes (a stop) must not surface as unhandled.
    promise.catch(() => {});
    return { promise, controller };
  };

  return {
    take(text) {
      const entry = pending.get(text) || start(text);
      pending.delete(text);
      return entry.promise;
    },
    prefetch(texts) {
      (texts || []).filter(Boolean).forEach((text) => {
        if (!pending.has(text)) pending.set(text, start(text));
      });
    },
    abortAll() {
      pending.forEach(({ controller }) => controller.abort());
      pending.clear();
    },
    get size() {
      return pending.size;
    },
  };
}
