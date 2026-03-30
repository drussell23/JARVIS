export function formatSSE(
  event: string,
  data: Record<string, unknown>,
  id?: string,
): string {
  const lines: string[] = [];
  if (id) lines.push(`id:${id}`);
  lines.push(`event:${event}`);
  lines.push(`data:${JSON.stringify(data)}`);
  lines.push("", "");
  return lines.join("\n");
}
