import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

/**
 * The route list is read in ONE tree scan and compared as a map. Twenty-five
 * separate `getByRole(name: RegExp)` calls each walk the whole tree, and under
 * a loaded full-suite run that reliably passed the 5s timeout -- a test that
 * fails on a busy machine reports load, not code.
 */
import { NAV } from "@/lib/nav";
import { Sidebar } from "./Sidebar";

vi.mock("next/navigation", () => ({ usePathname: () => "/terminal" }));

describe("Sidebar", () => {
  it("links every route and marks the current one", () => {
    render(<Sidebar />);
    const links = screen.getAllByRole("link");
    const byHref = new Map(links.map((l) => [l.getAttribute("href"), l]));
    for (const item of NAV) {
      expect(byHref.has(item.href)).toBe(true);
      expect(byHref.get(item.href)).toHaveTextContent(item.label);
    }
    expect(screen.getByRole("link", { name: /Trading Terminal/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
