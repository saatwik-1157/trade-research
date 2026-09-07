import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AnalyticsDashboard } from "./AnalyticsDashboard";
import type { AnalyticsSummary, MetricBlock } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    analyticsService: {
      ...actual.analyticsService,
      contract: vi.fn(),
      summary: vi.fn(),
      equity: vi.fn(),
      drawdown: vi.fn(),
      breakdown: vi.fn(),
      timeBreakdown: vi.fn(),
      execution: vi.fn(),
      exposure: vi.fn(),
    },
  };
});

const { analyticsService } = await import("@/lib/services");

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AnalyticsDashboard />
    </QueryClientProvider>,
  );
}

function block(over: Partial<MetricBlock> = {}): MetricBlock {
  return {
    unit: "currency",
    poolable_across_instruments: false,
    trades: 4,
    winning_trades: 3,
    losing_trades: 1,
    breakeven_trades: 0,
    win_rate: 0.75,
    loss_rate: 0.25,
    gross_profit: 90,
    gross_loss: 20,
    net_profit: 70,
    average_trade: 17.5,
    average_win: 30,
    average_loss: -20,
    largest_win: 50,
    largest_loss: -20,
    payoff_ratio: 1.5,
    profit_factor: 4.5,
    expectancy: 17.5,
    standard_deviation: 30,
    sharpe_per_trade: "INSUFFICIENT_DATA",
    sortino_per_trade: "INSUFFICIENT_DATA",
    t_statistic: 1.2,
    streaks: { longest_win: 2, longest_loss: 1, current_win: 1, current_loss: 0, note: "" },
    distribution: {},
    durations: { available: true, average_seconds: 3600 },
    sample_note: "4 trades. A ratio below 20 is reported as INSUFFICIENT_DATA.",
    ...over,
  };
}

function summary(over: Partial<AnalyticsSummary> = {}): AnalyticsSummary {
  return {
    scope: {},
    trade_count: 4,
    currency: block(),
    r_multiple: block({ unit: "r", poolable_across_instruments: true, net_profit: 2.5 }),
    costs: {
      gross_profit: 90,
      commission: 2,
      swap: 1,
      fees: 0,
      total_costs: 3,
      net_profit: 87,
      reconciles: true,
      note: "",
      slippage: "see /v1/analytics/execution",
    },
    environments: { paper: 3, demo: 1 },
    which_to_read: "R pools across trades and net currency does not.",
    ...over,
  };
}

function quiet() {
  vi.mocked(analyticsService.equity).mockResolvedValue({
    realized: {
      environment: "paper",
      kind: "realized",
      points: [],
      count: 0,
      final_value: 0,
      note: "cumulative realised P&L",
    },
    account: { available: false, why: "an account curve is per account." },
    note: "two curves, never merged.",
  });
  vi.mocked(analyticsService.drawdown).mockResolvedValue({
    available: false,
    why: "the curve has no points",
  });
  vi.mocked(analyticsService.breakdown).mockResolvedValue({
    dimension: "strategy",
    total_trades: 0,
    group_count: 0,
    groups: {},
    note: "sample sizes are on every group.",
  });
  vi.mocked(analyticsService.execution).mockResolvedValue({
    orders: 0,
    by_status: {},
    fill_ratio: "INSUFFICIENT_DATA",
    rejection_ratio: "INSUFFICIENT_DATA",
    partial_fills: 0,
    latency_seconds: {
      create_to_submit: {},
      submit_to_fill: {},
      measured_from: "0 of 0 orders",
    },
    slippage_points: {},
    slippage_note: "per FILL and in POINTS",
    fills: 0,
  });
}

describe("AnalyticsDashboard", () => {
  it("says NO COMPLETED TRADES rather than showing sample figures", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(
      summary({ trade_count: 0, currency: block({ trades: 0, win_rate: "INSUFFICIENT_DATA" }) }),
    );
    quiet();
    draw();
    expect(await screen.findByText("NO COMPLETED TRADES")).toBeInTheDocument();
    expect(screen.getByText(/indistinguishable from a real one/)).toBeInTheDocument();
  });

  it("renders INSUFFICIENT_DATA as a labelled dash, never as zero", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    const sharpe = await screen.findByLabelText("Sharpe / trade: no data");
    expect(sharpe).toHaveTextContent("—");
    // And a MEASURED zero still renders as 0.00. That is the distinction: fees
    // really were zero, and showing that as a dash would be as wrong as showing
    // an uncomputable Sharpe as 0.00.
    expect(screen.getByText("0.00")).toBeInTheDocument();
    expect(screen.queryByLabelText("Fees: no data")).not.toBeInTheDocument();
  });

  it("shows R beside net currency and says which pools", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    expect(await screen.findByText("the poolable figure")).toBeInTheDocument();
    expect(screen.getByText("not poolable")).toBeInTheDocument();
    expect(screen.getByText(/R pools across trades/)).toBeInTheDocument();
  });

  it("shows which environments the summarised rows spanned", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    expect(await screen.findByText("paper · 3")).toBeInTheDocument();
    expect(screen.getByText("demo · 1")).toBeInTheDocument();
  });

  it("puts the trade count beside the t-statistic", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    expect(await screen.findByText("on 4 trades")).toBeInTheDocument();
  });

  it("surfaces a small-sample warning when the backend sends one", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(
      summary({ sample_warning: "4 trades. This repository reached a t of 9.33 on 14." }),
    );
    quiet();
    draw();
    expect(await screen.findByText(/t of 9.33/)).toBeInTheDocument();
  });

  it("flags a P&L that does not reconcile rather than hiding it", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(
      summary({
        costs: {
          gross_profit: 90,
          commission: 2,
          swap: 1,
          fees: 0,
          total_costs: 3,
          net_profit: 90,
          reconciles: false,
          note: "",
          slippage: "",
        },
      }),
    );
    quiet();
    draw();
    expect(await screen.findByText(/booked twice or not at all/)).toBeInTheDocument();
  });

  it("draws no equity curve when there are no points", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    expect(await screen.findByText("NO EQUITY CURVE")).toBeInTheDocument();
    expect(screen.getByText(/still fabricated/)).toBeInTheDocument();
  });

  it("marks a low-sample group in the breakdown", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(summary());
    quiet();
    vi.mocked(analyticsService.breakdown).mockResolvedValue({
      dimension: "strategy",
      total_trades: 4,
      group_count: 2,
      groups: {
        "sv-a": {
          trades: 3,
          currency: block({ trades: 3 }),
          r_multiple: block({ unit: "r" }),
          below_comparison_floor: true,
        },
        "sv-b": {
          trades: 1,
          currency: block({ trades: 1 }),
          r_multiple: block({ unit: "r" }),
          below_comparison_floor: true,
        },
      },
      note: "sample sizes are on every group.",
    });
    draw();
    expect(await screen.findByText("sv-a")).toBeInTheDocument();
    expect(screen.getByText(/is not a finding/)).toBeInTheDocument();
  });

  it("says there are no orders rather than reporting a zero fill ratio", async () => {
    vi.mocked(analyticsService.summary).mockResolvedValue(summary());
    quiet();
    draw();
    expect(await screen.findByText("NO ORDERS")).toBeInTheDocument();
  });
});
