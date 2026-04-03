// jarvis-cloud/app/api/hive/summary/route.ts
// Public endpoint — no auth required. Returns sanitized Hive summary.

import { NextResponse } from "next/server";
import { getHiveSummary } from "@/lib/hive/hive-state";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const summary = await getHiveSummary();
    return NextResponse.json(summary, {
      headers: {
        "Cache-Control": "public, s-maxage=10, stale-while-revalidate=20",
        "Access-Control-Allow-Origin": "*",
      },
    });
  } catch (error) {
    console.error("[hive/summary] Error:", error);
    return NextResponse.json(
      { cognitive_state: "baseline", active_threads: [], recent_resolved: [], stats: { total_threads_today: 0, tokens_consumed_today: 0, debates_resolved_today: 0 } },
      { status: 200 }
    );
  }
}
