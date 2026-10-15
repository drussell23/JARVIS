import { decideHeard, echoOverlap, judgeHeardWhileSpeaking, wordsOf } from './bargeIn';

const policy = { enabled: true, min_words: 2, echo_overlap: 0.6, cooldown_ms: 500, echo_tail_ms: 2500 };
const line = 'Derek, you have three meetings tomorrow. First up is a screening at 8 AM on Microsoft Teams.';

describe('bargeIn', () => {
  test('words ignore case, punctuation and accents', () => {
    expect(wordsOf("Derek, it's 8 AM — café!")).toEqual(['derek', "it's", '8', 'am', 'cafe']);
  });

  test('a fragment of the line being said is its echo', () => {
    expect(echoOverlap('three meetings tomorrow first up', line)).toBe(1);
    expect(judgeHeardWhileSpeaking('a screening at 8 am', [line], policy).verdict).toBe('echo');
  });

  test('the user talking over it is an interruption', () => {
    expect(judgeHeardWhileSpeaking('stop that what about friday', [line], policy).verdict).toBe('user');
    expect(judgeHeardWhileSpeaking('wait open my email instead', [line], policy).verdict).toBe('user');
  });

  test('an interim transcript cut mid-word is still the echo', () => {
    expect(echoOverlap('three meetings tomor', line)).toBe(1);
    expect(echoOverlap('tomor three meetings', line)).toBeLessThan(1); // only the last word may be cut
  });

  test('an echo of a queued line is still an echo', () => {
    const queued = 'Your Vals AI interview is on Google Meet.';
    expect(judgeHeardWhileSpeaking('vals ai interview', [line, queued], policy).verdict).toBe('echo');
  });

  test('too little heard is not judged', () => {
    expect(judgeHeardWhileSpeaking('um', [line], policy).verdict).toBe('undecided');
    expect(judgeHeardWhileSpeaking('', [line], policy).verdict).toBe('undecided');
  });

  test('the operator decides: off, or a stricter threshold', () => {
    expect(judgeHeardWhileSpeaking('stop that now', [line], { ...policy, enabled: false }).verdict)
      .toBe('undecided');
    expect(judgeHeardWhileSpeaking('derek stop now', [line], { ...policy, echo_overlap: 0.3 }).verdict)
      .toBe('echo');
    expect(judgeHeardWhileSpeaking('stop that what about friday', [line], null).verdict).toBe('undecided');
  });
});

// What the page does with each result (2026-10-15: JARVIS's "Let me check."
// came back after the line ended and was sent as the user's command).
describe('decideHeard', () => {
  const ack = 'Let me check.';
  const moment = (over) => ({ speaking: false, msSinceSpoke: null, msSinceBargeIn: null,
    utteranceJudged: false, ...over });

  test('its own echo, finalised after the line ended, is ignored', () => {
    const heard = decideHeard('let me check your account', [ack, line], policy, moment({ msSinceSpoke: 1200 }));
    expect(heard.action).toBe('ignore');
  });

  test('the user talking over JARVIS interrupts it; right after it, is the user', () => {
    expect(decideHeard('what about friday', [line], policy, moment({ speaking: true })).action)
      .toBe('interrupt');
    expect(decideHeard('what about friday', [line], policy, moment({ msSinceSpoke: 800 })).action)
      .toBe('user');
  });

  test('outside the speech and its echo window, the usual handling applies', () => {
    expect(decideHeard('let me check', [ack], policy, moment({ msSinceSpoke: 9000 })).action).toBe('pass');
    expect(decideHeard('let me check', [ack], policy, moment()).action).toBe('pass');
  });

  test('an utterance is judged once: its later words are the same speaker', () => {
    // "what's 17 times 23" re-interrupted itself at every interim result.
    const later = moment({ speaking: true, utteranceJudged: true });
    expect(decideHeard('three meetings tomorrow', [line], policy, later).action).toBe('pass');
  });

  test('right after an interruption, the cooldown holds', () => {
    const heard = decideHeard('what about friday', [line], policy, moment({ speaking: true, msSinceBargeIn: 100 }));
    expect(heard).toMatchObject({ action: 'ignore', reason: 'cooldown' });
  });

  test('no policy, or a policy switched off, changes nothing', () => {
    expect(decideHeard('stop now', [line], null, moment({ speaking: true })).action).toBe('pass');
    expect(decideHeard('stop now', [line], { ...policy, enabled: false }, moment({ speaking: true })).action)
      .toBe('pass');
  });
});
