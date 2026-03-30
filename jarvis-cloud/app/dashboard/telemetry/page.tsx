export default function TelemetryPage() {
  return (
    <div>
      <h2 className="text-xl font-bold text-zinc-100 mb-6">Telemetry</h2>
      <div className="border border-zinc-800 rounded-lg p-4">
        <h3 className="text-sm font-bold text-zinc-400 mb-4">Event Log</h3>
        <p className="text-zinc-600 text-sm">Events will appear here as devices connect and send commands.</p>
      </div>
    </div>
  );
}
