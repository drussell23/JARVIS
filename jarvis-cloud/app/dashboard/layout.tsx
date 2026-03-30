import Link from "next/link";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Overview", icon: "⚡" },
  { href: "/dashboard/ouroboros", label: "Ouroboros", icon: "🔄" },
  { href: "/dashboard/devices", label: "Devices", icon: "📱" },
  { href: "/dashboard/telemetry", label: "Telemetry", icon: "📊" },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex">
      <nav className="w-56 border-r border-zinc-800 p-4 flex flex-col gap-1">
        <div className="mb-6">
          <h1 className="text-lg font-bold font-mono text-zinc-100">JARVIS</h1>
          <p className="text-xs text-zinc-500">Cloud Nervous System</p>
        </div>
        {NAV_ITEMS.map((item) => (
          <Link key={item.href} href={item.href} className="flex items-center gap-2 px-3 py-2 rounded-md text-sm text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/50 transition-colors">
            <span>{item.icon}</span>
            {item.label}
          </Link>
        ))}
      </nav>
      <main className="flex-1 p-6">{children}</main>
    </div>
  );
}
