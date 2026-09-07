import type { Role } from "./roles";

/**
 * Every route in the platform, with the level that makes it real and the
 * role required to open it.
 *
 * `status` is what the page can honestly show today:
 *   available - backed by a working API
 *   partial   - some real data (health, mode, gates); the rest labelled
 *   planned   - shell only; every control disabled and labelled with its level
 */
export type NavStatus = "available" | "partial" | "planned";
export type NavGroup = "Trade" | "Research" | "Automate" | "Review" | "System";

export interface NavItem {
  href: string;
  label: string;
  level: number;
  status: NavStatus;
  group: NavGroup;
  /** Minimum role to open the page. Mirrors backend/app/auth/permissions.py. */
  minRole: Role;
}

export const NAV: readonly NavItem[] = [
  { href: "/", label: "Dashboard", level: 3, status: "partial", group: "Trade", minRole: "user" },
  { href: "/markets", label: "Markets", level: 8, status: "partial", group: "Trade", minRole: "user" },
  { href: "/terminal", label: "Trading Terminal", level: 3, status: "partial", group: "Trade", minRole: "trader" },
  { href: "/positions", label: "Positions", level: 21, status: "partial", group: "Trade", minRole: "trader" },
  { href: "/orders", label: "Orders", level: 19, status: "partial", group: "Trade", minRole: "trader" },
  { href: "/portfolio", label: "Portfolio", level: 30, status: "partial", group: "Trade", minRole: "user" },
  { href: "/paper", label: "Paper Trading", level: 16, status: "planned", group: "Trade", minRole: "trader" },

  { href: "/strategies", label: "Strategies", level: 12, status: "planned", group: "Research", minRole: "trader" },
  { href: "/strategy-builder", label: "Strategy Builder", level: 13, status: "planned", group: "Research", minRole: "trader" },
  { href: "/backtesting", label: "Backtesting", level: 14, status: "planned", group: "Research", minRole: "user" },
  { href: "/replay", label: "Replay", level: 15, status: "planned", group: "Research", minRole: "user" },
  { href: "/ai-lab", label: "AI Lab", level: 25, status: "partial", group: "Research", minRole: "trader" },

  { href: "/bots", label: "Bots", level: 22, status: "partial", group: "Automate", minRole: "trader" },
  { href: "/risk", label: "Risk", level: 17, status: "partial", group: "Automate", minRole: "user" },
  { href: "/webhooks", label: "Webhooks", level: 9, status: "planned", group: "Automate", minRole: "trader" },
  { href: "/brokers", label: "Broker Connections", level: 10, status: "planned", group: "Automate", minRole: "trader" },

  { href: "/journal", label: "Journal", level: 31, status: "partial", group: "Review", minRole: "user" },
  { href: "/analytics", label: "Analytics", level: 32, status: "partial", group: "Review", minRole: "user" },
  // L34 built the notification centre and every control on the page is real.
  // Still "partial": most CATEGORIES have no producer yet — nothing emits an
  // order, position, bot, broker or risk event today — so the page works and
  // several of its filters will stay empty until the levels that publish
  // those events land. `GET /v1/notifications/contract` says which.
  { href: "/alerts", label: "Alerts", level: 34, status: "partial", group: "Review", minRole: "user" },
  // L35 built the Discord channel. Still "partial": the page reports a
  // real configuration state and can send a real test message, and the
  // shared-research half of this route is not built.
  { href: "/community", label: "Community", level: 35, status: "partial", group: "Review", minRole: "user" },

  // L37 built the collected view: components, incidents, metrics and a
  // derived trading-safety verdict. Still "partial": the detail needs the
  // admin role, and several components report NOT_CONFIGURED because the
  // deployment genuinely has no broker adapter or market data feed.
  { href: "/monitoring", label: "System Monitoring", level: 37, status: "partial", group: "System", minRole: "user" },
  { href: "/settings", label: "Settings", level: 3, status: "partial", group: "System", minRole: "user" },
  // L36 built the panel: dashboard, user access, sessions, RBAC, the audit
  // trail, integrations and configuration. Still "partial": every trading
  // control deliberately lives on the surface that owns it, so the page
  // links out rather than wrapping them.
  { href: "/admin", label: "Admin", level: 36, status: "partial", group: "System", minRole: "admin" },
] as const;

export const NAV_GROUPS: readonly NavGroup[] = ["Trade", "Research", "Automate", "Review", "System"];

/** Routes that render without a session and without the app chrome. */
export const PUBLIC_ROUTES: readonly string[] = ["/login", "/register"];

export function levelTag(level: number): string {
  return `L${String(level).padStart(2, "0")}`;
}

export function navItemFor(pathname: string): NavItem | undefined {
  return NAV.find((n) => n.href === pathname);
}

export function isPublicRoute(pathname: string): boolean {
  return PUBLIC_ROUTES.includes(pathname);
}
