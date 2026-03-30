export default function DashboardOverview() {
  return (
    <div>
      <h2 className="text-xl font-bold text-zinc-100 mb-6">System Overview</h2>
      <div className="grid grid-cols-3 gap-4">
        <div className="border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 uppercase tracking-wider">Connected Devices</p>
          <p className="text-2xl font-mono text-zinc-100 mt-1">—</p>
        </div>
        <div className="border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 uppercase tracking-wider">Commands Today</p>
          <p className="text-2xl font-mono text-zinc-100 mt-1">—</p>
        </div>
        <div className="border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 uppercase tracking-wider">Active Jobs</p>
          <p className="text-2xl font-mono text-zinc-100 mt-1">—</p>
        </div>
      </div>
      <div className="mt-8 border border-zinc-800 rounded-lg p-4">
        <h3 className="text-sm font-bold text-zinc-400 mb-4">Live Command Feed</h3>
        <p className="text-zinc-600 text-sm">Connect devices to see live activity.</p>
      </div>
    </div>
  );
}
