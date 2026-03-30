import { verifyCron } from "@/lib/auth/cron";

export async function POST(req: Request): Promise<Response> {
  if (verifyCron(req)) {
    return Response.json({ status: "scheduled_scan_queued" });
  }
  return new Response("Use /api/command for manual submissions", { status: 400 });
}
