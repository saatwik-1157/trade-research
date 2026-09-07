import { describe, expect, it } from "vitest";
import { NAV, NAV_GROUPS, levelTag, navItemFor } from "./nav";

const REQUIRED = [
  "Dashboard",
  "Markets",
  "Trading Terminal",
  "Positions",
  "Orders",
  "Strategy Builder",
  "System Monitoring",
  "Strategies",
  "Backtesting",
  "Replay",
  "Paper Trading",
  "Bots",
  "Portfolio",
  "Journal",
  "Analytics",
  "AI Lab",
  "Risk",
  "Alerts",
  "Webhooks",
  "Broker Connections",
  "Community",
  "Settings",
  "Admin",
];

describe("NAV", () => {
  it("contains every required section exactly once", () => {
    const labels = NAV.map((n) => n.label);
    for (const r of REQUIRED) expect(labels).toContain(r);
    expect(new Set(labels).size).toBe(NAV.length);
    expect(NAV.length).toBe(REQUIRED.length);
  });

  it("has unique hrefs and a level on every item", () => {
    expect(new Set(NAV.map((n) => n.href)).size).toBe(NAV.length);
    for (const n of NAV) expect(n.level).toBeGreaterThanOrEqual(0);
  });

  it("nothing claims to be fully available yet", () => {
    expect(NAV.some((n) => n.status === "available")).toBe(false);
  });

  it("every group is used", () => {
    for (const g of NAV_GROUPS) expect(NAV.some((n) => n.group === g)).toBe(true);
  });

  it("formats level tags and resolves paths", () => {
    expect(levelTag(3)).toBe("L03");
    expect(levelTag(42)).toBe("L42");
    expect(navItemFor("/terminal")?.label).toBe("Trading Terminal");
    expect(navItemFor("/nope")).toBeUndefined();
  });
});
