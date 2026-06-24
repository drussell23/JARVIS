/**
 * audioCapture -- Web Audio mic capture for the biometric write-path.
 *
 * Captures a bounded clean PCM buffer of the operator speaking the
 * challenge phrase, resamples to 16kHz mono, encodes a 16-bit PCM WAV,
 * and returns it base64-encoded for the backend's `audio_b64` field.
 *
 * Hard invariants:
 *   - the mic stream is ALWAYS released (no leaked mic): every track is
 *     stopped + the AudioContext closed in a finally, on success, error,
 *     or cancel.
 *   - bounded duration (env NEXT_PUBLIC_AUTH_CAPTURE_MS, default 4000ms).
 *   - fail-soft: getUserMedia rejection (permission denied / no device)
 *     throws AudioCaptureError with a clear message so the FSM can route
 *     to ERROR rather than hang.
 *
 * The backend expects 16kHz mono. We resample with a simple linear
 * resampler (the ECAPA pipeline's front-end is robust to this; we are not
 * doing high-fidelity audio, just a clean speech sample).
 */

const TARGET_SAMPLE_RATE = 16000;

export class AudioCaptureError extends Error {
  public readonly cause_code: string;
  public constructor(message: string, causeCode = 'capture_failed') {
    super(message);
    this.name = 'AudioCaptureError';
    this.cause_code = causeCode;
  }
}

export interface CaptureResult {
  readonly audioB64: string;
  readonly sampleRate: number;
}

export interface CaptureOptions {
  /** Capture duration in ms. Defaults to env / 4000. */
  readonly durationMs?: number;
  /** Test injection -- defaults to navigator.mediaDevices.getUserMedia. */
  readonly getUserMedia?: (
    constraints: MediaStreamConstraints,
  ) => Promise<MediaStream>;
  /** Test injection -- a factory for an AudioContext-like object. */
  readonly audioContextFactory?: () => AudioCaptureContext;
  /** Abort the capture early (operator cancel). */
  readonly signal?: AbortSignal;
}

/**
 * The minimal AudioContext surface we depend on, so tests can inject a
 * lightweight fake without a full Web Audio implementation.
 */
export interface AudioCaptureContext {
  readonly sampleRate: number;
  createMediaStreamSource(stream: MediaStream): { connect(dest: unknown): void };
  /** Returns captured float32 PCM (mono) once the duration elapses. */
  capture(durationMs: number, signal?: AbortSignal): Promise<Float32Array>;
  close(): Promise<void>;
}

function resolveDurationMs(explicit?: number): number {
  if (explicit !== undefined && explicit > 0) {
    return explicit;
  }
  const raw =
    typeof process !== 'undefined'
      ? process.env.NEXT_PUBLIC_AUTH_CAPTURE_MS
      : undefined;
  if (raw !== undefined && raw !== '') {
    const n = Number.parseInt(raw, 10);
    if (Number.isFinite(n) && n > 0) {
      return n;
    }
  }
  return 4000;
}

function resolveGetUserMedia(
  override?: CaptureOptions['getUserMedia'],
): (constraints: MediaStreamConstraints) => Promise<MediaStream> {
  if (override !== undefined) {
    return override;
  }
  const md =
    typeof navigator !== 'undefined' ? navigator.mediaDevices : undefined;
  if (md === undefined || typeof md.getUserMedia !== 'function') {
    throw new AudioCaptureError(
      'no microphone device available in this environment',
      'no_device',
    );
  }
  return md.getUserMedia.bind(md);
}

/**
 * Capture + encode a bounded mono 16kHz WAV, base64-encoded. Always
 * releases the mic stream + closes the AudioContext.
 */
export async function captureAuthAudio(
  opts: CaptureOptions = {},
): Promise<CaptureResult> {
  const durationMs = resolveDurationMs(opts.durationMs);
  const getUserMedia = resolveGetUserMedia(opts.getUserMedia);

  let stream: MediaStream | null = null;
  let ctx: AudioCaptureContext | null = null;
  try {
    try {
      stream = await getUserMedia({ audio: true });
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      throw new AudioCaptureError(
        `microphone permission denied or unavailable: ${msg}`,
        'permission_denied',
      );
    }

    ctx = opts.audioContextFactory
      ? opts.audioContextFactory()
      : createBrowserContext();

    const source = ctx.createMediaStreamSource(stream);
    source.connect((ctx as unknown as { destination?: unknown }).destination);

    const float = await ctx.capture(durationMs, opts.signal);
    if (opts.signal?.aborted === true) {
      throw new AudioCaptureError('capture cancelled', 'cancelled');
    }

    const resampled = resampleLinear(float, ctx.sampleRate, TARGET_SAMPLE_RATE);
    const wav = encodeWav16(resampled, TARGET_SAMPLE_RATE);
    const audioB64 = bytesToBase64(wav);
    return { audioB64, sampleRate: TARGET_SAMPLE_RATE };
  } finally {
    // ALWAYS release the mic + the context -- no leaked stream.
    releaseStream(stream);
    if (ctx !== null) {
      try {
        await ctx.close();
      } catch {
        // Best-effort teardown; never mask the primary outcome.
      }
    }
  }
}

function releaseStream(stream: MediaStream | null): void {
  if (stream === null) {
    return;
  }
  try {
    for (const track of stream.getTracks()) {
      track.stop();
    }
  } catch {
    // Best-effort.
  }
}

/**
 * Linear-interpolation resampler. Adequate for a speech sample fed into
 * the ECAPA front-end; we are not doing studio-grade audio.
 */
export function resampleLinear(
  input: Float32Array,
  fromRate: number,
  toRate: number,
): Float32Array {
  if (fromRate === toRate || input.length === 0) {
    return input;
  }
  const ratio = fromRate / toRate;
  const outLen = Math.max(1, Math.floor(input.length / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i += 1) {
    const srcPos = i * ratio;
    const i0 = Math.floor(srcPos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = srcPos - i0;
    out[i] = input[i0]! * (1 - frac) + input[i1]! * frac;
  }
  return out;
}

/** Encode mono float32 [-1,1] PCM as a 16-bit PCM WAV byte buffer. */
export function encodeWav16(
  samples: Float32Array,
  sampleRate: number,
): Uint8Array {
  const numSamples = samples.length;
  const blockAlign = 2; // mono * 16-bit
  const byteRate = sampleRate * blockAlign;
  const dataSize = numSamples * 2;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  writeAscii(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(view, 8, 'WAVE');
  writeAscii(view, 12, 'fmt ');
  view.setUint32(16, 16, true); // PCM fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true); // bits per sample
  writeAscii(view, 36, 'data');
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < numSamples; i += 1) {
    const s = Math.max(-1, Math.min(1, samples[i]!));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }
  return new Uint8Array(buffer);
}

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i += 1) {
    view.setUint8(offset + i, text.charCodeAt(i));
  }
}

/** Base64-encode a byte buffer (browser btoa or Node Buffer). */
export function bytesToBase64(bytes: Uint8Array): string {
  if (typeof btoa === 'function') {
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }
  // Node fallback (test environments without btoa).
  return Buffer.from(bytes).toString('base64');
}

/**
 * The real browser AudioContext capture path. Uses a MediaStream source
 * + a ScriptProcessor-free Analyser-less approach via a buffered
 * ScriptProcessorNode is avoided; we accumulate via an AudioWorklet-free
 * MediaRecorder-independent capture using an offline-friendly recorder.
 *
 * To keep the dependency surface small + jsdom-testable, the browser path
 * uses a ScriptProcessorNode to accumulate raw float samples. This runs
 * only in a real browser; tests inject `audioContextFactory`.
 */
function createBrowserContext(): AudioCaptureContext {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const AC: any =
    (globalThis as unknown as { AudioContext?: unknown }).AudioContext ??
    (globalThis as unknown as { webkitAudioContext?: unknown })
      .webkitAudioContext;
  if (AC === undefined) {
    throw new AudioCaptureError(
      'Web Audio API unavailable in this environment',
      'no_audio_context',
    );
  }
  const ctx = new AC();

  let processor: { disconnect(): void; onaudioprocess: unknown } | null = null;
  const chunks: Float32Array[] = [];

  return {
    get sampleRate(): number {
      return ctx.sampleRate as number;
    },
    createMediaStreamSource(stream: MediaStream) {
      const src = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor
        ? ctx.createScriptProcessor(4096, 1, 1)
        : null;
      if (node !== null) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        node.onaudioprocess = (e: any) => {
          const data = e.inputBuffer.getChannelData(0) as Float32Array;
          chunks.push(new Float32Array(data));
        };
        src.connect(node);
        node.connect(ctx.destination);
        processor = node;
      }
      return src;
    },
    async capture(durationMs: number, signal?: AbortSignal): Promise<Float32Array> {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, durationMs);
        if (signal !== undefined) {
          signal.addEventListener(
            'abort',
            () => {
              clearTimeout(timer);
              resolve();
            },
            { once: true },
          );
        }
      });
      if (processor !== null) {
        processor.disconnect();
      }
      let total = 0;
      for (const c of chunks) {
        total += c.length;
      }
      const out = new Float32Array(total);
      let off = 0;
      for (const c of chunks) {
        out.set(c, off);
        off += c.length;
      }
      return out;
    },
    async close(): Promise<void> {
      if (typeof ctx.close === 'function') {
        await ctx.close();
      }
    },
  };
}
