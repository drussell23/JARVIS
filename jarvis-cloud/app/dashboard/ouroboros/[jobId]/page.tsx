export default async function OuroborosJobDetail({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;
  return (
    <div>
      <h2 className="text-xl font-bold text-zinc-100 mb-2">Job: <span className="font-mono text-zinc-400">{jobId}</span></h2>
      <div className="mt-6 border border-zinc-800 rounded-lg p-4">
        <p className="text-zinc-500 text-sm">Job details will load here once results are available.</p>
      </div>
    </div>
  );
}
