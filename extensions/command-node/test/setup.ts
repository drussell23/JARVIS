import '@testing-library/jest-dom/vitest';

// React Flow measures the DOM with ResizeObserver, which jsdom does not
// implement. Provide a no-op stub so components mount in tests.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (typeof globalThis.ResizeObserver === 'undefined') {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).ResizeObserver = ResizeObserverStub;
}

// jsdom lacks DOMMatrixReadOnly / matchMedia used by React Flow.
if (typeof (globalThis as unknown as { matchMedia?: unknown }).matchMedia === 'undefined') {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).matchMedia = () => ({
    matches: false,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
  });
}
