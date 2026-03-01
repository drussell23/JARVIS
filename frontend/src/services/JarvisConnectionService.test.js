import JarvisConnectionService, { ConnectionState } from './JarvisConnectionService';
import DynamicWebSocketClient from './DynamicWebSocketClient';

describe('JarvisConnectionService control-plane recovery discovery', () => {
  let initSpy;
  const originalPortRange = process.env.REACT_APP_LOADING_SERVER_PORT_RANGE;
  const originalFetch = global.fetch;

  beforeEach(() => {
    initSpy = jest
      .spyOn(JarvisConnectionService.prototype, '_initializeAsync')
      .mockImplementation(() => {});
    localStorage.clear();
    delete window.JARVIS_LOADING_SERVER_PORT;
    delete process.env.REACT_APP_LOADING_SERVER_PORT;
    delete process.env.REACT_APP_LOADING_SERVER_PORT_RANGE;
    window.history.pushState({}, '', '/');
  });

  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    if (originalFetch === undefined) {
      delete global.fetch;
    } else {
      global.fetch = originalFetch;
    }
    initSpy.mockRestore();
    if (originalPortRange === undefined) {
      delete process.env.REACT_APP_LOADING_SERVER_PORT_RANGE;
    } else {
      process.env.REACT_APP_LOADING_SERVER_PORT_RANGE = originalPortRange;
    }
  });

  test('includes unified supervisor loading ports and loopback variants', () => {
    window.JARVIS_LOADING_SERVER_PORT = 8080;

    const service = new JarvisConnectionService();
    service.backendUrl = 'http://localhost:8010';

    const candidates = service._getControlPlaneCandidates();

    expect(candidates).toContain('http://localhost:8080');
    expect(candidates).toContain('http://127.0.0.1:8080');
    expect(candidates).toContain('http://localhost:3001');
  });

  test('supports explicit loading-server port ranges from env', () => {
    process.env.REACT_APP_LOADING_SERVER_PORT_RANGE = '9100-9101,9200';

    const service = new JarvisConnectionService();
    service.backendUrl = 'http://localhost:8010';

    const candidates = service._getControlPlaneCandidates();

    expect(candidates).toContain('http://localhost:9100');
    expect(candidates).toContain('http://localhost:9101');
    expect(candidates).toContain('http://localhost:9200');
  });

  test('transient health probe failures go straight to reconnecting without error state', async () => {
    jest.useFakeTimers();

    const service = new JarvisConnectionService();
    service.backendUrl = 'http://localhost:8010';
    service.wsUrl = 'ws://localhost:8010';

    jest.spyOn(service, '_checkBackendHealth').mockResolvedValue({
      ok: false,
      error: 'Backend health probe timed out after 8000ms',
      code: 'ERR_FETCH_TIMEOUT',
      transient: true,
      source: 'health'
    });

    await service._connectToBackend();

    expect(service.getState()).toBe(ConnectionState.RECONNECTING);
    expect(service.getLastError()).toBe(
      'Backend temporarily unavailable: Backend health probe timed out after 8000ms'
    );
    expect(jest.getTimerCount()).toBe(1);
  });

  test('deduplicates backend reconnect scheduling', () => {
    jest.useFakeTimers();

    const service = new JarvisConnectionService();

    service._scheduleReconnect({ reason: 'test' });
    service._scheduleReconnect({ reason: 'test' });

    expect(jest.getTimerCount()).toBe(1);
    expect(service.getState()).toBe(ConnectionState.RECONNECTING);
  });

  test('deduplicates websocket retry scheduling', () => {
    jest.useFakeTimers();

    const service = new JarvisConnectionService();
    service.backendUrl = 'http://localhost:8010';
    service.wsUrl = 'ws://localhost:8010';
    jest.spyOn(service, 'isWebSocketConnected').mockReturnValue(false);

    service._scheduleWebSocketRetry('test');
    service._scheduleWebSocketRetry('test');

    expect(jest.getTimerCount()).toBe(1);
    expect(service.webSocketRetryState.attempt).toBe(1);
  });
});

describe('DynamicWebSocketClient reconnect policy', () => {
  test('honors autoReconnect=false on close', () => {
    const client = new DynamicWebSocketClient({ autoReconnect: false });
    const reconnectSpy = jest.spyOn(client, '_scheduleReconnect');

    client._handleClose('ws://localhost:8010/ws', { code: 1006 });

    expect(reconnectSpy).not.toHaveBeenCalled();
    client.destroy();
  });
});
