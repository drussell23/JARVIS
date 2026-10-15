import { echoOverlap, judgeHeardWhileSpeaking, wordsOf } from './bargeIn';

const policy = { enabled: true, min_words: 2, echo_overlap: 0.6, cooldown_ms: 500 };
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
