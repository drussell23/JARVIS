import { choosePersonaVoice } from './voicePersona';

const DANIEL = { name: 'Daniel', locale: 'en-gb', gender: 'male', enforce: true };
const voice = (name, lang) => ({ name, lang });

test('the declared voice wins where it exists (macOS Chrome)', () => {
  const v = choosePersonaVoice([voice('Samantha', 'en-US'), voice('Daniel', 'en-GB')], DANIEL);
  expect(v.name).toBe('Daniel');
});

test('elsewhere a same-locale voice that states the gender', () => {
  const v = choosePersonaVoice([voice('Google UK English Female', 'en-GB'),
    voice('Google UK English Male', 'en-GB')], DANIEL);
  expect(v.name).toBe('Google UK English Male');
});

test('never another person: "Female" is not "male", a US voice is not British', () => {
  expect(choosePersonaVoice([voice('Google UK English Female', 'en-GB'),
    voice('Microsoft David - English (United States)', 'en-US')], DANIEL)).toBeNull();
});
