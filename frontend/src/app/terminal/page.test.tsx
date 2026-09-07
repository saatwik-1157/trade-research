import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import TerminalPage from "./page";

// jsdom has no canvas; the chart library is replaced by an inert double and
// the test asserts the overlay that tells the user the chart has no data.
vi.mock("lightweight-charts", () => ({
  createChart: () => ({ addSeries: () => ({}), remove: () => {} }),
  CandlestickSeries: {},
  ColorType: { Solid: "solid" },
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TerminalPage />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

const PANELS = ["Watchlist", "Chart", "Order panel", "Positions", "Orders", "Bot status"];
const ACCOUNT = [
  "Balance",
  "Equity",
  "Used margin",
  "Free margin",
  "Unrealized P&L",
  "Realized P&L",
];

describe("TerminalPage", () => {
  it("renders every panel and every account figure as unmeasured", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
    renderPage();
    for (const p of PANELS) {
      expect(screen.getByRole("region", { name: p })).toBeInTheDocument();
    }
    for (const s of ACCOUNT) {
      expect(screen.getByLabelText(`${s}: no data`)).toHaveTextContent("—");
    }
  });

  it("offers a symbol and a timeframe selector", () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
    renderPage();
    expect(screen.getByLabelText("symbol")).toBeInTheDocument();
    expect(screen.getByLabelText("timeframe")).toBeInTheDocument();
  });

  it("cannot submit an order without an account to submit it to", () => {
    // CHANGED AT L19: submission is built, so the ticket is no longer disabled
    // on principle. It is disabled until an account is named, because the
    // backend refuses an order for an account with no registered order
    // manager and a button that submits into a guaranteed refusal is worse
    // than one that will not arm.
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
    renderPage();
    const ticket = screen.getByRole("region", { name: "Order panel" });
    const submit = within(ticket).getByRole("button", { name: "submit order" });
    expect(submit).toBeDisabled();
    expect(within(ticket).getByLabelText("Account")).toBeInTheDocument();
  });

  it("shows the backend's order state and never an optimistic fill", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
    renderPage();
    const ticket = screen.getByRole("region", { name: "Order panel" });
    // Nothing has been submitted, so there is no result block at all — an
    // empty ticket must not render a status that could be read as a fill.
    expect(within(ticket).queryByLabelText("order result")).not.toBeInTheDocument();
    expect(within(ticket).queryByText(/filled/i)).not.toBeInTheDocument();
  });

  it("labels the chart as having no feed", () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
    renderPage();
    const chart = screen.getByRole("region", { name: "Chart" });
    expect(within(chart).getByRole("note")).toHaveTextContent("L08");
  });

  it("never claims a price it does not have", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
    renderPage();
    const watchlist = screen.getByRole("region", { name: "Watchlist" });
    // Symbols are listed; prices are em dashes, and the panel says why.
    expect(within(watchlist).getByText("EURUSD")).toBeInTheDocument();
    expect(await within(watchlist).findByRole("note")).toHaveTextContent("L08");
  });
});
