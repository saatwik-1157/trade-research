import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PortfolioDashboard } from "./PortfolioDashboard";
import type { PortfolioSummaryRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    accountsService: { ...actual.accountsService, list: vi.fn() },
    portfolioService: {
      ...actual.portfolioService,
      summary: vi.fn(),
      positions: vi.fn(),
      exposure: vi.fn(),
      reconciliation: vi.fn(),
      history: vi.fn(),
    },
  };
});

const { accountsService, portfolioService } = await import("@/lib/services");

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PortfolioDashboard />
    </QueryClientProvider>,
  );
}

function summary(over: Partial<PortfolioSummaryRow> = {}): PortfolioSummaryRow {
  return {
    at: "2026-09-04T12:00:00",
    environment: "paper",
    health: "HEALTHY",
    health_reasons: ["every reading is present and within tolerance"],
    freshness: "FRESH",
    account: {
      account_id: "acc1",
      environment: "paper",
      currency: "USD",
      broker: null,
      balance: "100000.0000",
      equity: "100650.0000",
      margin_used: null,
      margin_free: null,
      unrealized_pnl: null,
      realized_pnl: "500.0000",
      as_of: "2026-09-04T12:00:00",
      source: "PAPER_ENGINE",
      freshness: "FRESH",
      unavailable_reason: null,
    },
    pnl: {
      realized: "500.0000",
      unrealized: null,
      total: null,
      realized_today: "25.0000",
      trades_today: 1,
      realized_trades: 4,
      open_positions: 1,
      unrealized_unavailable: ["EURUSD"],
    },
    drawdown: {
      peak_equity: "120000.0000",
      current: "19350.0000",
      current_pct: 0.16125,
      max_drawdown: "19350.0000",
      recovered: false,
    },
    margin: null,
    exposure: { gross: "110000.00", net: "110000.00", positions: 1, uncomputable: 0 },
    position_count: 1,
    ...over,
  };
}

function quiet() {
  vi.mocked(portfolioService.positions).mockResolvedValue({
    environment: "paper",
    positions: [],
    count: 0,
    unmarked: [],
  });
  vi.mocked(portfolioService.exposure).mockResolvedValue({
    environment: "paper",
    exposure: null,
    open_risk: {
      total: "0",
      unstopped_positions: 0,
      uncomputable_positions: 0,
      complete: true,
      note: "",
    },
    correlation: { available: false, reason: "no correlation analysis has been run.", closest_available: "" },
  });
  vi.mocked(portfolioService.reconciliation).mockResolvedValue({
    environment: "paper",
    reconciliation: {
      checked: false,
      agrees: false,
      internal_positions: 0,
      broker_positions: 0,
      mismatches: [],
      note: "no reconciliation has run.",
    },
  });
}

describe("PortfolioDashboard", () => {
  it("says there are no accounts rather than rendering an empty portfolio", async () => {
    vi.mocked(accountsService.list).mockResolvedValue([]);
    draw();
    expect(await screen.findByText("No trading accounts")).toBeInTheDocument();
  });

  it("renders an unavailable figure as a dash, never as a zero", async () => {
    vi.mocked(accountsService.list).mockResolvedValue([
      { id: "acc1", name: "paper", environment: "paper", currency: "USD" },
    ]);
    vi.mocked(portfolioService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    const marginUsed = await screen.findByLabelText("Margin used: no data");
    expect(marginUsed).toHaveTextContent("—");
    expect(screen.queryByText("0.00 USD")).not.toBeInTheDocument();
  });

  it("withholds unrealized P&L rather than showing a partial total", async () => {
    vi.mocked(accountsService.list).mockResolvedValue([
      { id: "acc1", name: "paper", environment: "paper", currency: "USD" },
    ]);
    vi.mocked(portfolioService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    expect(await screen.findByText(/no mark for EURUSD/)).toBeInTheDocument();
    expect(screen.getByText(/withheld rather than partial/)).toBeInTheDocument();
  });

  it("shows the reasons when the view is not healthy", async () => {
    vi.mocked(accountsService.list).mockResolvedValue([
      { id: "acc1", name: "paper", environment: "paper", currency: "USD" },
    ]);
    vi.mocked(portfolioService.summary).mockResolvedValue(
      summary({
        health: "STALE",
        freshness: "STALE",
        health_reasons: ["the account was last read at 2026-09-04T10:00:00"],
      }),
    );
    quiet();
    draw();
    expect(await screen.findByText(/last read at/)).toBeInTheDocument();
    expect(screen.getAllByText("STALE").length).toBeGreaterThan(0);
  });

  it("does not present an unchecked reconciliation as agreement", async () => {
    vi.mocked(accountsService.list).mockResolvedValue([
      { id: "acc1", name: "paper", environment: "paper", currency: "USD" },
    ]);
    vi.mocked(portfolioService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    expect(await screen.findByText("NOT CHECKED")).toBeInTheDocument();
    expect(screen.queryByText("AGREES")).not.toBeInTheDocument();
  });

  it("shows gross and net side by side, never one alone", async () => {
    vi.mocked(accountsService.list).mockResolvedValue([
      { id: "acc1", name: "paper", environment: "paper", currency: "USD" },
    ]);
    vi.mocked(portfolioService.summary).mockResolvedValue(summary());
    quiet();
    vi.mocked(portfolioService.exposure).mockResolvedValue({
      environment: "paper",
      exposure: {
        total: {
          long: "50000",
          short: "30000",
          gross: "80000",
          net: "20000",
          long_positions: 1,
          short_positions: 1,
          positions: 2,
          uncomputable: 0,
        },
        by_symbol: {},
        by_strategy: {},
        by_bot: {},
        by_asset_class: {},
        by_currency: null,
        currency_note: "unavailable: one or more symbols carry no base/quote currency.",
        concentration: { by_symbol: {}, largest: null, note: "" },
        uncomputable: [],
      },
      open_risk: {
        total: "1000",
        unstopped_positions: 0,
        uncomputable_positions: 0,
        complete: true,
        note: "",
      },
      correlation: {
        available: false,
        reason: "no correlation analysis has been run.",
        closest_available: "",
      },
    });
    draw();
    expect(await screen.findByText("80,000.00")).toBeInTheDocument();
    expect(screen.getByText("20,000.00")).toBeInTheDocument();
  });

  it("says why the currency breakdown is missing rather than showing an empty table", async () => {
    vi.mocked(accountsService.list).mockResolvedValue([
      { id: "acc1", name: "paper", environment: "paper", currency: "USD" },
    ]);
    vi.mocked(portfolioService.summary).mockResolvedValue(summary());
    quiet();
    vi.mocked(portfolioService.exposure).mockResolvedValue({
      environment: "paper",
      exposure: {
        total: {
          long: "0",
          short: "0",
          gross: "0",
          net: "0",
          long_positions: 0,
          short_positions: 0,
          positions: 0,
          uncomputable: 0,
        },
        by_symbol: {},
        by_strategy: {},
        by_bot: {},
        by_asset_class: {},
        by_currency: null,
        currency_note: "unavailable: one or more symbols carry no base/quote currency.",
        concentration: { by_symbol: {}, largest: null, note: "" },
        uncomputable: [],
      },
      open_risk: {
        total: "0",
        unstopped_positions: 0,
        uncomputable_positions: 0,
        complete: true,
        note: "",
      },
      correlation: {
        available: false,
        reason: "no correlation analysis has been run.",
        closest_available: "",
      },
    });
    draw();
    expect(await screen.findByText(/one or more symbols carry no base\/quote currency/)).toBeInTheDocument();
  });
});
