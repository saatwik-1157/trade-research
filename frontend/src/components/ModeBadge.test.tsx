import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModeBadge } from "./ModeBadge";

describe("ModeBadge", () => {
  it("shows PAPER and DEMO plainly", () => {
    const { rerender } = render(<ModeBadge mode="paper" />);
    expect(screen.getByRole("status")).toHaveTextContent("PAPER");
    rerender(<ModeBadge mode="demo" />);
    expect(screen.getByRole("status")).toHaveTextContent("DEMO");
  });

  it("shows LIVE as blocked unless execution is allowed", () => {
    const { rerender } = render(<ModeBadge mode="live" liveAllowed={false} blockers={10} />);
    expect(screen.getByRole("status")).toHaveTextContent("LIVE · BLOCKED");
    rerender(<ModeBadge mode="live" liveAllowed={true} blockers={0} />);
    expect(screen.getByRole("status")).toHaveTextContent("LIVE · ARMED");
  });

  it("never defaults to a mode when the API is unknown", () => {
    render(<ModeBadge />);
    expect(screen.getByRole("status")).toHaveTextContent("MODE UNKNOWN");
  });
});
