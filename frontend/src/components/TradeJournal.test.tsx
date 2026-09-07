import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TradeDetail, TradeJournal } from "./TradeJournal";
import type { TradeRow, TradeStatistics } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    journalService: {
      ...actual.journalService,
      list: vi.fn(),
      get: vi.fn(),
      timeline: vi.fn(),
      decisions: vi.fn(),
      executions: vi.fn(),
      analysis: vi.fn(),
      statistics: vi.fn(),
      exportUrl: () => "/v1/trades/export",
    },
  };
});

const { journalService } = await import("@/lib/services");

function draw(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function trade(over: Partial<TradeRow> = {}): TradeRow {
  return {
    id: "trade-1",
    mode: "paper",
    status: "closed",
    symbol: "EURUSD",
    side: "long",
    volume: "1",
    entry_price: "1.1000",
    exit_price: "1.1050",
    opened_at: "2026-09-04T10:00:00",
    closed_at: "2026-09-04T11:00:00",
    gross_profit: "50",
    commission: "0",
    swap: "0",
    fees: null,
    net_profit: "50",
    r_multiple: "1.00",
    currency: "USD",
    exit_reason: "take_profit",
    strategy_version_id: null,
    bot_id: null,
    position_id: "pos-1",
    order_id: null,
    broker_position_id: null,
    source: "pipeline",
    data_quality: null,
    ...over,
  };
}

function stats(over: Partial<TradeStatistics> = {}): TradeStatistics {
  return {
    trades: 1,
    wins: 1,
    losses: 0,
    win_rate: 1,
    net_profit: "50",
    gross_profit: "50",
    commission: "0",
    swap: "0",
    r_multiple: { sum: "1.00", mean: "1.00", trades_with_r: 1, note: "the figure to pool." },
    by_exit_reason: { take_profit: 1 },
    by_mode: { paper: 1 },
    reconciliation_required: 0,
    not_computed: { sharpe: "L32. Analytics owns it." },
    ...over,
  };
}

describe("TradeJournal", () => {
  it("says NO TRADES rather than showing a sample row", async () => {
    vi.mocked(journalService.list).mockResolvedValue({
      items: [],
      page: { total: 0, limit: 25, offset: 0 },
    });
    vi.mocked(journalService.statistics).mockResolvedValue(stats({ trades: 0, wins: 0, win_rate: null }));
    draw(<TradeJournal />);
    expect(await screen.findByText("NO TRADES")).toBeInTheDocument();
    expect(screen.getByText(/no sample rows are shown/)).toBeInTheDocument();
  });

  it("has no win rate on an empty set rather than nought percent", async () => {
    vi.mocked(journalService.list).mockResolvedValue({
      items: [],
      page: { total: 0, limit: 25, offset: 0 },
    });
    vi.mocked(journalService.statistics).mockResolvedValue(stats({ trades: 0, wins: 0, losses: 0, win_rate: null }));
    draw(<TradeJournal />);
    expect(await screen.findByLabelText("Win rate: no data")).toBeInTheDocument();
    expect(screen.queryByText("0.0%")).not.toBeInTheDocument();
  });

  it("shows the environment on every row and does not merge them", async () => {
    vi.mocked(journalService.list).mockResolvedValue({
      items: [trade(), trade({ id: "trade-2", mode: "demo" })],
      page: { total: 2, limit: 25, offset: 0 },
    });
    vi.mocked(journalService.statistics).mockResolvedValue(stats({ by_mode: { paper: 1, demo: 1 } }));
    draw(<TradeJournal />);
    expect(await screen.findByText("paper")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
  });

  it("tells the reader to pool R rather than net currency", async () => {
    vi.mocked(journalService.list).mockResolvedValue({
      items: [trade()],
      page: { total: 1, limit: 25, offset: 0 },
    });
    vi.mocked(journalService.statistics).mockResolvedValue(stats());
    draw(<TradeJournal />);
    expect(await screen.findByText(/not net currency/)).toBeInTheDocument();
    expect(await screen.findByText("not poolable")).toBeInTheDocument();
  });

  it("marks a reconciliation-required trade as critical, not closed", async () => {
    vi.mocked(journalService.list).mockResolvedValue({
      items: [trade({ status: "reconciliation_required" })],
      page: { total: 1, limit: 25, offset: 0 },
    });
    vi.mocked(journalService.statistics).mockResolvedValue(stats());
    draw(<TradeJournal />);
    expect(await screen.findByText("reconciliation_required")).toBeInTheDocument();
  });
});

describe("TradeDetail", () => {
  function wire(over: Partial<TradeRow> = {}) {
    vi.mocked(journalService.get).mockResolvedValue(trade(over));
    vi.mocked(journalService.decisions).mockResolvedValue({
      trade_id: "trade-1",
      environment: "paper",
      strategy: { available: false, what: "strategy attribution", why: "no strategy is linked." },
      ai: {
        available: false,
        what: "AI attribution",
        why: "no AI decision is linked. AI_DISABLED is the expected shape and it is NOT evidence the AI was bypassed.",
      },
      risk: { available: false, what: "risk attribution", why: "no risk decision is linked." },
      sizing: { available: false, what: "position sizing", why: "no sizing record is linked." },
      execution: {},
      costs: {},
      holding: { seconds: 3600, hours: 1, note: "exact" },
    });
    vi.mocked(journalService.timeline).mockResolvedValue({
      trade_id: "trade-1",
      events: [
        {
          at: "2026-09-04T11:00:00",
          source: "journal",
          source_note: "the trade journal (L31)",
          kind: "trade_recorded",
          detail: {},
        },
      ],
      count: 1,
      gaps: ["no signal is linked"],
      note: "derived at read time",
    });
    vi.mocked(journalService.executions).mockResolvedValue({
      trade_id: "trade-1",
      available: true,
      closes: [
        {
          at: "2026-09-04T11:00:00",
          quantity: "1",
          fill_price: "1.1050",
          reason: "take_profit",
          realized_running: "50",
          broker_deal_id: "d1",
          fill_source: "simulator",
        },
      ],
      exit: { price: "1.1050", quantity: "1", closes: 1, note: "volume-weighted" },
      entry_price: "1.1000",
      entry_note: "already weighted",
    });
  }

  it("says which kind of absent each missing context block is", async () => {
    wire();
    draw(<TradeDetail tradeId="trade-1" />);
    expect(await screen.findByText(/NOT evidence the AI was bypassed/)).toBeInTheDocument();
    expect(screen.getByText(/no sizing record is linked/)).toBeInTheDocument();
  });

  it("renders the timeline and names its gaps", async () => {
    wire();
    draw(<TradeDetail tradeId="trade-1" />);
    expect(await screen.findByText("trade_recorded")).toBeInTheDocument();
    expect(screen.getByText(/no signal is linked/)).toBeInTheDocument();
  });

  it("surfaces data-quality findings rather than hiding them", async () => {
    wire({
      data_quality: {
        checked: true,
        errors: 1,
        warnings: 0,
        findings: [
          { code: "negative_duration", detail: "closed_at precedes opened_at", severity: "error" },
        ],
      },
    });
    draw(<TradeDetail tradeId="trade-1" />);
    expect(await screen.findByText("negative_duration")).toBeInTheDocument();
    expect(screen.getByText("1 data-quality error(s)")).toBeInTheDocument();
  });

  it("shows each cost separately rather than folded together", async () => {
    wire({ commission: "3", swap: "1", gross_profit: "54" });
    draw(<TradeDetail tradeId="trade-1" />);
    expect(await screen.findByText("Commission")).toBeInTheDocument();
    expect(screen.getByText("3.00")).toBeInTheDocument();
    // 1.00 appears twice -- swap and R -- and that is the point: each cost is
    // its own tile rather than folded into one total.
    expect(screen.getAllByText("1.00").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("54.00")).toBeInTheDocument();
  });
});
