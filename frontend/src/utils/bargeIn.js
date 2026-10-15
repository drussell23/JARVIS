/**
 * bargeIn - the user may interrupt JARVIS, the way a person can.
 *
 * The page keeps listening while JARVIS speaks. What it hears then is either
 * the user or JARVIS's own voice coming back through the microphone (over
 * Remote Desktop the speakers sit beside the microphone, with no echo
 * canceller between them). A transcript is JARVIS's echo when most of its
 * words are words JARVIS is saying; anything else is the user talking, and
 * JARVIS stops.
 *
 * The thresholds are the operator's (backend/core/barge_in_policy, served at
 * /audio/barge-in); this module only applies them.
 */

let policyPromise = null;

/** The barge-in policy: {enabled, min_words, echo_overlap, cooldown_ms}. */
export function getBargeInPolicy(apiUrl) {
  if (!policyPromise) {
    policyPromise = fetch(`${apiUrl}/audio/barge-in`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .catch((err) => {
        policyPromise = null; // a later call may reach a backend that was not up yet
        throw err;
      });
  }
  return policyPromise;
}

/** Forget the fetched policy (a reconnect may reach a backend set differently). */
export function resetBargeInPolicy() {
  policyPromise = null;
}

/** The words of *text*, lower-cased, letters and digits only. */
export function wordsOf(text) {
  return (text || '')
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .split(/[^\p{L}\p{N}']+/u)
    .map((w) => w.replace(/^'+|'+$/g, ''))
    .filter(Boolean);
}

/**
 * The share of *heard*'s words that JARVIS is saying in *spoken* (0..1).
 * Containment, not symmetric similarity: an echo is a fragment of a long
 * line, so a measure that also weighs the line's length calls it different.
 */
export function echoOverlap(heard, spoken) {
  const heardWords = wordsOf(heard);
  if (heardWords.length === 0) return 0;
  const spokenWords = wordsOf(spoken);
  const said = new Set(spokenWords);
  if (said.size === 0) return 0;
  // An interim transcript may end mid-word ("tomor"): its last word is the
  // start of a word being said.
  const last = heardWords.length - 1;
  const matches = heardWords.filter((w, i) =>
    said.has(w) || (i === last && spokenWords.some((s) => s.startsWith(w))));
  return matches.length / heardWords.length;
}

/**
 * What a transcript heard while JARVIS speaks is:
 *   'echo'      -- JARVIS's own voice coming back;
 *   'user'      -- the user talking over it: stop;
 *   'undecided' -- too little heard yet to tell (a cough, a breath).
 * *spokenLines* are the lines JARVIS is saying or has queued.
 */
export function judgeHeardWhileSpeaking(heard, spokenLines, policy) {
  if (!policy || !policy.enabled) return { verdict: 'undecided', overlap: 0 };
  const words = wordsOf(heard);
  if (words.length < policy.min_words) return { verdict: 'undecided', overlap: 0 };
  const overlap = (spokenLines || []).reduce(
    (best, line) => Math.max(best, echoOverlap(heard, line)), 0);
  return { verdict: overlap >= policy.echo_overlap ? 'echo' : 'user', overlap };
}
