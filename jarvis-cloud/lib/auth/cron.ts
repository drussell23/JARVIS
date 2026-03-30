export function verifyCron(req: Request): boolean {
  const auth = req.headers.get("Authorization");
  return auth === `Bearer ${process.env.CRON_SECRET}`;
}
