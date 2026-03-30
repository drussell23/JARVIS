export const config = {
  crons: [
    { path: "/api/ouroboros/submit", schedule: "0 3 * * *" },
    { path: "/api/devices/health", schedule: "0 */6 * * *" },
  ],
};
