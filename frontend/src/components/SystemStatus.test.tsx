import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SystemStatus } from "./SystemStatus";

const health = {
  status: "ok",
  time: "2026-09-02T00:00:00+00:00",
  service: "trade-research-platform",
  version: "0.2.0",
  environment: "development",
  trading_mode: "paper",
  live_trading: false,
  live_execution_allowed: false,
  live_execution_blockers: ["LIVE_TRADING is false", "gate not built: kill_switch"],
};

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());

describe("SystemStatus", () => {
  it("shows the mode and lists every live-execution blocker from the API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(health), { status: 200 })),
    );
    wrap(<SystemStatus />);
    expect(await screen.findByText("PAPER")).toBeInTheDocument();
    expect(screen.getByText("BLOCKED")).toBeInTheDocument();
    const list = screen.getByLabelText("live execution blockers");
    expect(list).toHaveTextContent("gate not built: kill_switch");
    expect(list.querySelectorAll("li")).toHaveLength(2);
  });

  it("shows DOWN when the API cannot be reached, not a default mode", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("x", { status: 503 })));
    wrap(<SystemStatus />);
    expect(await screen.findByText("DOWN")).toBeInTheDocument();
    expect(screen.queryByText("PAPER")).not.toBeInTheDocument();
  });
});
