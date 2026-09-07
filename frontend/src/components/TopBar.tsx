"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useHealth } from "@/hooks/useHealth";
import { useLogout, useMe } from "@/hooks/useMe";
import { navItemFor } from "@/lib/nav";
import { MobileNav } from "./MobileNav";
import { ModeBadge } from "./ModeBadge";
import { NotificationBell } from "./NotificationBell";

function Clock() {
  // Rendered after mount only, so server and client markup match.
  const [now, setNow] = useState<string | null>(null);
  useEffect(() => {
    const tick = () => setNow(new Date().toISOString().slice(11, 19) + " UTC");
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="tabular font-mono text-body text-muted" aria-label="UTC clock">
      {now ?? "--:--:-- UTC"}
    </span>
  );
}

function SessionMenu() {
  const me = useMe();
  const signOut = useLogout();
  const router = useRouter();

  if (me.isPending) return null;
  if (me.isError) {
    return (
      <Link href="/login" className="text-body text-accent hover:underline">
        Sign in
      </Link>
    );
  }
  return (
    <span className="flex items-center gap-2 text-body" aria-label="session">
      <span className="text-ink-2">{me.data.email}</span>
      <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-mini uppercase text-muted">
        {me.data.role}
      </span>
      <button
        type="button"
        className="text-muted hover:text-ink"
        onClick={() => signOut.mutate(undefined, { onSettled: () => router.replace("/login") })}
        disabled={signOut.isPending}
      >
        Sign out
      </button>
    </span>
  );
}

export function TopBar() {
  const pathname = usePathname();
  const item = navItemFor(pathname);
  const health = useHealth();

  const api = health.isPending
    ? { label: "API: connecting", cls: "text-muted" }
    : health.isError
      ? { label: "API: unreachable", cls: "text-critical" }
      : { label: `API: ok · v${health.data.version}`, cls: "text-good" };

  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-4">
      <div className="flex min-w-0 items-center gap-2">
        <MobileNav />
        <span className="truncate text-sm font-medium">{item?.label ?? "trade-research"}</span>
      </div>
      {/* The right-hand cluster drops its least load-bearing members first as
          the bar narrows: the clock, then the API version, then the session.
          The mode badge never hides -- which mode this is, is the one thing
          on this bar nobody may have to guess at. */}
      <div className="flex shrink-0 items-center gap-3">
        {/* L34. Beside the session rather than in the sidebar: the bell is
            about the signed-in person, and it renders nothing at all when
            there is no session to count against. */}
        <NotificationBell />
        <span className="hidden sm:flex">
          <SessionMenu />
        </span>
        <span className={`hidden text-body lg:inline ${api.cls}`} role="status">
          {api.label}
        </span>
        <ModeBadge
          mode={health.data?.trading_mode}
          liveAllowed={health.data?.live_execution_allowed}
          blockers={health.data?.live_execution_blockers.length}
        />
        <span className="hidden xl:inline">
          <Clock />
        </span>
      </div>
    </header>
  );
}
