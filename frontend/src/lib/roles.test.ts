import { describe, expect, it } from "vitest";
import { NAV } from "./nav";
import { roleAtLeast } from "./roles";

describe("roles", () => {
  it("orders user < trader < admin", () => {
    expect(roleAtLeast("admin", "trader")).toBe(true);
    expect(roleAtLeast("trader", "admin")).toBe(false);
    expect(roleAtLeast("user", "user")).toBe(true);
    expect(roleAtLeast("user", "trader")).toBe(false);
  });

  it("protects the trading surfaces and admin in the nav table", () => {
    const need = Object.fromEntries(NAV.map((n) => [n.href, n.minRole]));
    for (const href of ["/terminal", "/strategies", "/bots", "/brokers", "/ai-lab", "/webhooks", "/paper"]) {
      expect(need[href], href).toBe("trader");
    }
    expect(need["/admin"]).toBe("admin");
    expect(NAV.every((n) => n.minRole !== undefined)).toBe(true);
  });
});
