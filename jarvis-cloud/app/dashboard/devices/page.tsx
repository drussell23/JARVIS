export default function DevicesPage() {
  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-zinc-100">Devices</h2>
        <button className="bg-zinc-100 text-zinc-900 text-sm font-medium px-4 py-2 rounded-md hover:bg-zinc-200 transition-colors">Pair New Device</button>
      </div>
      <div className="border border-zinc-800 rounded-lg divide-y divide-zinc-800">
        <div className="p-4 text-zinc-500 text-sm">No devices paired. Click "Pair New Device" to get started.</div>
      </div>
    </div>
  );
}
