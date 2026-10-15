import { createSpeechAudioFetcher, sanitizeForSpeech, TTS_SILENT } from './speechAudio';

const response = ({ ok = true, status = 200, silent = false, body = 'mp3' } = {}) => ({
  ok,
  status,
  headers: { get: (name) => (name === 'X-TTS-Status' ? (silent ? 'silent' : 'ok') : null) },
  blob: async () => body,
});

const fetcherWith = (impl) => {
  const calls = [];
  const fetchImpl = (url, init) => {
    calls.push({ url, text: JSON.parse(init.body).text, signal: init.signal });
    return impl(url, init);
  };
  return { calls, fetcher: createSpeechAudioFetcher({ apiUrl: () => 'http://jarvis', timeoutMs: 1000, fetchImpl }) };
};

describe('speechAudio', () => {
  test('one sanitizer: no ellipses, emoji or markdown read aloud', () => {
    expect(sanitizeForSpeech('**Done**... 🎉 See you.')).toBe('Done See you');
    expect(sanitizeForSpeech('...')).toBe('');
  });

  test('a queued line is fetched once, ahead, and handed over when its turn comes', async () => {
    const { calls, fetcher } = fetcherWith(async () => response({ body: 'second' }));
    fetcher.prefetch(['Second sentence.', 'Second sentence.', '']);
    expect(calls.map((c) => c.text)).toEqual(['Second sentence.']);
    await expect(fetcher.take('Second sentence.')).resolves.toBe('second');
    expect(calls).toHaveLength(1);                     // not fetched again
    expect(fetcher.size).toBe(0);
  });

  test('a line not fetched ahead is fetched when taken', async () => {
    const { calls, fetcher } = fetcherWith(async () => response({ body: 'now' }));
    await expect(fetcher.take('Now.')).resolves.toBe('now');
    expect(calls[0].url).toBe('http://jarvis/audio/speak');
  });

  test('a silent render or a failed request is a rejection the caller falls back from', async () => {
    const silent = fetcherWith(async () => response({ silent: true })).fetcher;
    await expect(silent.take('Hi.')).rejects.toThrow(TTS_SILENT);
    const failed = fetcherWith(async () => response({ ok: false, status: 503 })).fetcher;
    await expect(failed.take('Hi.')).rejects.toThrow('503');
  });

  test('a stop cancels every line in flight, and none is handed over later', async () => {
    const { calls, fetcher } = fetcherWith((url, init) => new Promise((resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    }));
    fetcher.prefetch(['One.', 'Two.']);
    fetcher.abortAll();
    expect(calls.every((c) => c.signal.aborted)).toBe(true);
    expect(fetcher.size).toBe(0);
  });
});
