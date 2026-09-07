"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV, NAV_GROUPS, levelTag, type NavItem } from "@/lib/nav";

function StatusDot({ status }: { status: NavItem["status"] }) {
  const cls =
    status === "available"
      ? "bg-good"
      : status === "partial"
        ? "bg-warning"
        : "bg-baseline";
  const title =
    status === "available" ? "available" : status === "partial" ? "partial" : "planned";
  return <span className={`inline-block h-1.5 w-1.5 rounded-full ${cls}`} title={title} />;
}

/**
 * The nav itself, shared by the desktop rail and the mobile drawer.
 *
 * Extracted rather than copied. Two lists over the same `NAV` would agree
 * until the first route was added to one of them, and the one that drifts is
 * always the one fewer people open.
 */
export function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <>
      {NAV_GROUPS.map((group) => (
        <div key={group} className="mb-2">
          <div className="px-4 pb-1 pt-2 text-mini font-semibold uppercase tracking-wider text-muted">
            {group}
          </div>
          <ul>
            {NAV.filter((n) => n.group === group).map((item) => {
              const active = pathname === item.href;
              // The active row carries a 2px accent marker as well as a
              // fill. Fill alone was doing the job at 1.09:1 against the
              // sidebar, which is to say it was not doing it; the marker is
              // what reads, and the lifted --surface-2 (now 1.35:1)
              // supports it rather than carrying it.
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={`flex items-center justify-between gap-2 border-l-2 py-1.5 pl-3.5 pr-4 text-sm transition-colors duration-150 ${
                      active
                        ? "border-accent bg-surface-2 font-medium text-ink"
                        : "border-transparent text-ink-2 hover:bg-surface-2/60 hover:text-ink"
                    }`}
                  >
                    <span className="flex items-center gap-2">
                      <StatusDot status={item.status} />
                      {item.label}
                    </span>
                    <span className="font-mono text-micro text-muted">{levelTag(item.level)}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </>
  );
}

export function NavLegend() {
  return (
    <div className="border-t border-line px-4 py-2 text-micro text-muted">
      <span className="inline-block h-1.5 w-1.5 rounded-full bg-good" /> available{" "}
      <span className="ml-2 inline-block h-1.5 w-1.5 rounded-full bg-warning" /> partial{" "}
      <span className="ml-2 inline-block h-1.5 w-1.5 rounded-full bg-baseline" /> planned
    </div>
  );
}

export function Brand() {
  return (
    <>
      <span className="h-2.5 w-2.5 rounded-sm bg-accent" aria-hidden />
      <span className="text-sm font-semibold tracking-tight">trade-research</span>
    </>
  );
}

export function Sidebar() {
  return (
    <aside
      aria-label="Primary"
      className="hidden w-56 shrink-0 flex-col border-r border-line bg-surface md:flex"
    >
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
        <Brand />
      </div>
      <nav className="flex-1 overflow-y-auto py-2">
        <NavList />
      </nav>
      <NavLegend />
    </aside>
  );
}
