"use client";

import { usePathname } from "next/navigation";
import { isPublicRoute, navItemFor } from "@/lib/nav";
import { RouteGuard } from "./RouteGuard";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (isPublicRoute(pathname)) {
    return <main className="flex min-h-screen items-center justify-center p-4">{children}</main>;
  }

  // Unknown routes (404) fall back to the lowest role rather than being open.
  const minRole = navItemFor(pathname)?.minRole ?? "user";

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="flex-1 overflow-x-hidden p-4 md:p-6">
          <RouteGuard minRole={minRole}>{children}</RouteGuard>
        </main>
      </div>
    </div>
  );
}
