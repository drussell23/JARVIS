import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import MicrophonePermissionHelper from './MicrophonePermissionHelper';

jest.mock('../services/MicrophonePermissionManager', () => ({
  __esModule: true,
  default: {
    subscribe: () => () => {},
    getPermissionInstructions: () => ({ steps: [] }),
  },
}));

jest.mock('../services/DynamicConfigService', () => ({
  __esModule: true,
  default: { getApiUrl: () => null },
}));

// jsdom has no navigator.mediaDevices: the helper lands on 'unsupported',
// one of the states the panel can be minimized for.
const TITLE = 'Browser Not Supported';
const KEY = 'jarvis.microphoneHelper.minimizedFor';

beforeEach(() => window.localStorage.clear());

const expanded = () => screen.findByRole('heading', { name: TITLE });

test('the panel minimizes to one line and stays minimized after a reload', async () => {
  const { unmount } = render(<MicrophonePermissionHelper />);
  await expanded();

  fireEvent.click(screen.getByRole('button', { name: /minimize microphone help/i }));
  expect(screen.queryByRole('heading', { name: TITLE })).toBeNull();
  expect(screen.getByRole('button', { name: /browser not supported/i })).toBeInTheDocument();
  expect(window.localStorage.getItem(KEY)).toBe('unsupported');

  unmount();
  render(<MicrophonePermissionHelper />);
  await waitFor(() => expect(screen.getByTitle('Show microphone help')).toBeInTheDocument());
  expect(screen.queryByRole('heading', { name: TITLE })).toBeNull();
});

test('the pill opens the full help again', async () => {
  window.localStorage.setItem(KEY, 'unsupported');
  render(<MicrophonePermissionHelper />);
  fireEvent.click(await screen.findByTitle('Show microphone help'));
  await expanded();
  expect(window.localStorage.getItem(KEY)).toBeNull();
});

test('a different problem than the one minimized opens expanded', async () => {
  window.localStorage.setItem(KEY, 'denied');
  render(<MicrophonePermissionHelper />);
  await expanded();
});

test('without browser storage the panel still minimizes for this session', async () => {
  const getItem = jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new Error('blocked');
  });
  const setItem = jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error('blocked');
  });
  try {
    render(<MicrophonePermissionHelper />);
    await expanded();
    fireEvent.click(screen.getByRole('button', { name: /minimize microphone help/i }));
    expect(screen.getByTitle('Show microphone help')).toBeInTheDocument();
  } finally {
    getItem.mockRestore();
    setItem.mockRestore();
  }
});
