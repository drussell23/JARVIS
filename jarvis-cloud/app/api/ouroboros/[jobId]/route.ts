import { getRedis } from "@/lib/redis/client";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const { jobId } = await params;
  const redis = getRedis();
  const [jobRaw, metaRaw] = await Promise.all([
    redis.get(`job:${jobId}`),
    redis.get(`jobmeta:${jobId}`),
  ]);
  if (!metaRaw) return new Response("Job not found", { status: 404 });
  const meta = typeof metaRaw === "string" ? JSON.parse(metaRaw) : metaRaw;
  const result = jobRaw ? (typeof jobRaw === "string" ? JSON.parse(jobRaw) : jobRaw) : null;
  return Response.json({
    job_id: jobId,
    command_id: meta.command_id,
    brain: meta.brain,
    submitted_at: meta.submitted_at,
    status: result ? result.status : "pending",
    result: result?.result ?? null,
    metrics: result?.metrics ?? null,
  });
}
