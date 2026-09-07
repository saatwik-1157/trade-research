import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NAV } from "@/lib/nav";
import { MobileNav } from "./MobileNav";

vi.mock("next/navigation", () => ({ usePathname: () => "/terminal" }));

/**
 * Below `md` the sidebar is hidden and, until this component, nothing replaced
 * it: every route was reachable only by typing its URL. These assert the two
 * things that made it a hole rather than a rough edge -- that the routes are
 * reachable at all, and that the drawer can be got rid of again.
 */
describe("MobileNav", () => {
  it("reaches every route the sidebar reaches", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));

    const dialog = screen.getByRole("dialog", { name: /primary/i });
    const links = screen.getAllByRole("link");
    const byHref = new Map(links.map((l) => [l.getAttribute("href"), l]));
    for (const item of NAV) {
      expect(byHref.has(item.href)).toBe(true);
      expect(byHref.get(item.href)).toHaveTextContent(item.label);
    }
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("marks the current route", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));
    expect(screen.getByRole("link", { name: /Trading Terminal/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("closes on Escape", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes when a destination is chosen", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));
    const first = screen
      .getAllByRole("link")
      .find((l) => l.getAttribute("href") === NAV[0].href);
    fireEvent.click(first!);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("releases the page scroll it locked", () => {
    render(<MobileNav />);
    fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.body.style.overflow).not.toBe("hidden");
  });

  it("is shut until it is opened", () => {
    render(<MobileNav />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /open navigation/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
