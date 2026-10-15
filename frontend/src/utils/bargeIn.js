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

/** The barge-in policy: {enabled, min_words, echo_overlap, cooldown_ms, echo_tail_ms}. */
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

// Number words, so "eight thirty" said and "8:30" transcribed are the same
// words (2026-10-15: JARVIS said "from eight thirty to nine AM", heard its
// own echo as "8:30 to 9 a.m." -- two words in six matched -- took it for
// the user and stopped itself mid-answer).
const UNITS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
  'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen',
  'eighteen', 'nineteen'];
const TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'];
const NUMBER_WORDS = new Map([
  ...UNITS.map((word, value) => [word, value]),
  ...TENS.map((word, index) => [word, index * 10]).filter(([word]) => word),
]);
const SCALES = new Map([['hundred', 100], ['thousand', 1000]]);

/** Runs of number words as the digits they say: "twenty five" -> "25",
 *  "two hundred and five" -> "205"; "eight thirty" stays two numbers (a
 *  time), as "8:30" is two once split. */
function numbersAsDigits(tokens) {
  const out = [];
  let value = null;     // the number being read
  let lastUnit = null;  // the last word's own value, to tell "twenty five" from "five twenty"
  const flush = () => {
    if (value !== null) out.push(String(value));
    value = null;
    lastUnit = null;
  };
  tokens.forEach((token, i) => {
    if (NUMBER_WORDS.has(token)) {
      const n = NUMBER_WORDS.get(token);
      // A unit after a tens word joins it ("twenty five"); anything else
      // after a number starts a new one ("eight thirty", "five twenty").
      const joins = value !== null && lastUnit !== null && lastUnit % 10 === 0 && lastUnit >= 20
        && n < 10 && value % 10 === 0;
      if (joins) {
        value += n;
      } else if (value !== null && lastUnit === null) {
        value += n; // after "hundred": "two hundred five"
      } else {
        flush();
        value = n;
      }
      lastUnit = n;
    } else if (SCALES.has(token) && value !== null) {
      value *= SCALES.get(token);
      lastUnit = null;
    } else if (token === 'and' && value !== null && lastUnit === null && NUMBER_WORDS.has(tokens[i + 1])) {
      // "two hundred AND five": part of the number
    } else {
      flush();
      out.push(token);
    }
  });
  flush();
  return out;
}

/** The words of *text* in one canonical form, whoever wrote them: lower case,
 *  no accents, letters and digits only, "a.m."/"AM" -> "am", number words as
 *  digits, "8:30" as "8" "30". What JARVIS said and what the recogniser
 *  transcribed of it compare word for word. */
export function wordsOf(text) {
  const tokens = (text || '')
    .toLowerCase()
    .normalize('NFKD')
    .replace(/\p{M}/gu, '') // accents: the combining marks NFKD separated out
    // Single letters each followed by a dot are one word: "a.m." -> "am",
    // "u.s." -> "us" ("Dr." is not a run of single letters and stays).
    .replace(/\b(?:\p{L}\.)+\p{L}?\.?/gu, (run) => run.replace(/\./g, ''))
    .split(/[^\p{L}\p{N}']+/u)
    .map((w) => w.replace(/^'+|'+$/g, ''))
    .filter(Boolean);
  return numbersAsDigits(tokens);
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

/**
 * What to do with one recognition result, given where JARVIS's voice is:
 *   'interrupt' -- the user is talking over JARVIS: stop it;
 *   'user'      -- the user, just after JARVIS finished (its echo window);
 *   'ignore'    -- JARVIS's echo, or too little heard to tell, or within the
 *                  cooldown of the last interruption;
 *   'pass'      -- outside JARVIS's speech and its echo window (or already
 *                  judged as the user's utterance): the usual handling.
 * *moment*: {speaking, msSinceSpoke (null when JARVIS has not spoken),
 *            msSinceBargeIn (null when never), utteranceJudged}.
 */
export function decideHeard(heard, spokenLines, policy, moment) {
  if (!policy || !policy.enabled || moment.utteranceJudged) return { action: 'pass', overlap: 0 };
  const inEchoTail = !moment.speaking && moment.msSinceSpoke !== null
    && moment.msSinceSpoke < (policy.echo_tail_ms || 0);
  if (!moment.speaking && !inEchoTail) return { action: 'pass', overlap: 0 };
  if (moment.speaking && moment.msSinceBargeIn !== null && moment.msSinceBargeIn < policy.cooldown_ms) {
    return { action: 'ignore', overlap: 0, reason: 'cooldown' };
  }
  const { verdict, overlap } = judgeHeardWhileSpeaking(heard, spokenLines, policy);
  if (verdict !== 'user') return { action: 'ignore', overlap, reason: verdict };
  return { action: moment.speaking ? 'interrupt' : 'user', overlap };
}
