
// Minimal Node driver that loads the compiled VS Code extension's
// StreamConsumer, runs it against the provided endpoint, and prints
// each event_type to stdout so the parent Python harness can
// collect them. Exits when it receives SIGTERM.
const path = require('path');
const streamModulePath = path.resolve(process.argv[2]);
const { StreamConsumer } = require(streamModulePath);

const endpoint = process.argv[3];
const consumer = new StreamConsumer({
    endpoint,
    autoReconnect: false,
    reconnectMaxBackoffMs: 1000,
});

consumer.onState((s) => {
    process.stdout.write('STATE ' + s + '\n');
});
consumer.onEvent((frame) => {
    process.stdout.write('EVENT ' + frame.event_type + '\n');
});

process.on('SIGTERM', async () => {
    await consumer.stop();
    process.exit(0);
});

consumer.start();

// Keep the event loop alive; the stream consumer holds its own
// pending IO but Node will exit on empty loop after start() returns
// if we don't register something.
setInterval(() => {}, 60_000);
