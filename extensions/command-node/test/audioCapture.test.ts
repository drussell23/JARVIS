/**
 * audioCapture tests -- capture + encode + the hard mic-release invariant.
 *
 * The Web Audio surface is injected (getUserMedia + an AudioContext-like
 * factory) so the encode + resample + base64 + ALWAYS-release-the-mic
 * behavior is testable in jsdom without real audio hardware.
 */

import { describe, expect, test, vi } from 'vitest';
import {
  AudioCaptureError,
  bytesToBase64,
  captureAuthAudio,
  encodeWav16,
  resampleLinear,
  type AudioCaptureContext,
} from '../lib/audioCapture';

/** A MediaStream whose track-stop we can observe (the mic-release proof). */
function fakeStream() {
  const stop = vi.fn();
  const stream = {
    getTracks: () => [{ stop }],
  } as unknown as MediaStream;
  return { stream, stop };
}

/** A minimal AudioContext-like fake returning fixed PCM. */
function fakeContextFactory(
  pcm: Float32Array,
  sampleRate = 48000,
  closeSpy = vi.fn(),
): () => AudioCaptureContext {
  return () => ({
    sampleRate,
    createMediaStreamSource() {
      return { connect: () => {} };
    },
    async capture() {
      return pcm;
    },
    async close() {
      closeSpy();
    },
  });
}

describe('captureAuthAudio', () => {
  test('captures, encodes a 16kHz WAV, and base64-encodes it', async () => {
    const { stream, stop } = fakeStream();
    const getUserMedia = vi.fn().mockResolvedValue(stream);
    const pcm = new Float32Array([0, 0.5, -0.5, 1, -1, 0.25]);

    const result = await captureAuthAudio({
      durationMs: 10,
      getUserMedia,
      audioContextFactory: fakeContextFactory(pcm, 48000),
    });

    expect(result.sampleRate).toBe(16000);
    expect(typeof result.audioB64).toBe('string');
    expect(result.audioB64.length).toBeGreaterThan(0);
    // The decoded payload starts with the RIFF/WAVE header.
    const bytes = Buffer.from(result.audioB64, 'base64');
    expect(bytes.subarray(0, 4).toString('ascii')).toBe('RIFF');
    expect(bytes.subarray(8, 12).toString('ascii')).toBe('WAVE');
    // The mic was requested + released.
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(stop).toHaveBeenCalledTimes(1);
  });

  test('ALWAYS releases the mic + closes the context on error', async () => {
    const { stream, stop } = fakeStream();
    const getUserMedia = vi.fn().mockResolvedValue(stream);
    const closeSpy = vi.fn();
    const throwingCtx = (): AudioCaptureContext => ({
      sampleRate: 16000,
      createMediaStreamSource() {
        return { connect: () => {} };
      },
      async capture() {
        throw new Error('device exploded mid-capture');
      },
      async close() {
        closeSpy();
      },
    });

    await expect(
      captureAuthAudio({
        durationMs: 10,
        getUserMedia,
        audioContextFactory: throwingCtx,
      }),
    ).rejects.toThrow('device exploded');

    // Even on failure: track stopped + context closed (no leaked mic).
    expect(stop).toHaveBeenCalledTimes(1);
    expect(closeSpy).toHaveBeenCalledTimes(1);
  });

  test('permission denial throws a clear AudioCaptureError', async () => {
    const getUserMedia = vi
      .fn()
      .mockRejectedValue(new Error('NotAllowedError'));

    const err = await captureAuthAudio({
      durationMs: 10,
      getUserMedia,
      audioContextFactory: fakeContextFactory(new Float32Array([0])),
    }).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(AudioCaptureError);
    expect((err as AudioCaptureError).cause_code).toBe('permission_denied');
    expect((err as AudioCaptureError).message).toContain('permission denied');
  });

  test('aborted capture throws cancelled and still releases the mic', async () => {
    const { stream, stop } = fakeStream();
    const getUserMedia = vi.fn().mockResolvedValue(stream);
    const controller = new AbortController();
    controller.abort();

    const err = await captureAuthAudio({
      durationMs: 10,
      getUserMedia,
      audioContextFactory: fakeContextFactory(new Float32Array([0.1, 0.2])),
      signal: controller.signal,
    }).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(AudioCaptureError);
    expect((err as AudioCaptureError).cause_code).toBe('cancelled');
    expect(stop).toHaveBeenCalledTimes(1);
  });
});

describe('encode helpers', () => {
  test('resampleLinear downsamples 48k -> 16k by ~1/3', () => {
    const input = new Float32Array(48);
    const out = resampleLinear(input, 48000, 16000);
    expect(out.length).toBe(16);
  });

  test('resampleLinear is identity when rates match', () => {
    const input = new Float32Array([0.1, 0.2, 0.3]);
    expect(resampleLinear(input, 16000, 16000)).toBe(input);
  });

  test('encodeWav16 emits a 44-byte header + 2 bytes/sample', () => {
    const wav = encodeWav16(new Float32Array([0, 1, -1]), 16000);
    expect(wav.length).toBe(44 + 3 * 2);
    expect(String.fromCharCode(wav[0]!, wav[1]!, wav[2]!, wav[3]!)).toBe(
      'RIFF',
    );
  });

  test('bytesToBase64 round-trips', () => {
    const bytes = new Uint8Array([65, 66, 67]);
    expect(bytesToBase64(bytes)).toBe(Buffer.from('ABC').toString('base64'));
  });
});
