import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TradeReview } from "./TradeReview";
import type { ReviewSection, TradeReviewRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    reviewService: {
      ...actual.reviewService,
      contract: vi.fn(),
      forTrade: vi.fn(),
      generate: vi.fn(),
      regenerate: vi.fn(),
      versions: vi.fn(),
      patterns: vi.fn(),
      summary: vi.fn(),
    },
  };
});

const { reviewService } = await import("@/lib/services");

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TradeReview tradeId="t1" />
    </QueryClientProvider>,
  );
}

function section(over: Partial<ReviewSection> = {}): ReviewSection {
  return {
    rating: "GOOD",
    evidence: [
      { kind: "OBSERVED", statement: "entry filled at 1.1030.", source: "trades.entry_price" },
      { kind: "INTERPRETED", statement: "the stop was 100 points away.", source: null },
    ],
    warnings: [],
    unavailable_reason: null,
    ...over,
  };
}

function review(over: Partial<TradeReviewRow> = {}): TradeReviewRow {
  return {
    id: "rev1",
    trade_id: "t1",
    environment: "paper",
    review_version: 1,
    status: "COMPLETED",
    summary: "EURUSD long, win at 50 USD.",
    outcome: "WIN",
    compliance: "COMPLIANT",
    strategy_alignment: section(),
    entry_quality: section(),
    exit_quality: section(),
    risk_quality: section({
      rating: "UNKNOWN",
      evidence: [],
      unavailable_reason: "no risk decision is linked to this trade.",
    }),
    execution_quality: section(),
    market_context: { regime: "TRENDING" },
    ai_context: { available: true },
    key_factors: [],
    warnings: [],
    lessons: [],
    follow_up_questions: ["Was the strategy signal generated during a volatility spike?"],
    confidence: 0.62,
    completeness: { available: ["decision.entry_price"], missing: ["outcome.mae"], fraction: 0.5 },
    attribution: {
      review_model: "deterministic",
      review_model_version: "1.0",
      prediction_model: "trade_probability",
      prediction_model_version: "2.3",
    },
    schema_version: "1.0",
    prompt_version: "trade_review_prompt_v1",
    validation: { valid: true, errors: [] },
    attempts: 1,
    error: null,
    duration_ms: 12.4,
    created_at: "2026-09-04T12:00:00",
    completed_at: "2026-09-04T12:00:00",
    ...over,
  };
}

describe("TradeReview", () => {
  it("says NO REVIEW rather than inventing one", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: false,
      eligible: true,
      why: "no review has been generated for this trade yet.",
    });
    draw();
    expect(await screen.findByText("NO REVIEW")).toBeInTheDocument();
    expect(screen.getByText("Generate review")).toBeInTheDocument();
  });

  it("explains why an ineligible trade cannot be reviewed", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: false,
      eligible: false,
      why: "the trade is 'unknown'. A review describes a completed episode.",
    });
    draw();
    expect(await screen.findByText(/completed episode/)).toBeInTheDocument();
    expect(screen.queryByText("Generate review")).not.toBeInTheDocument();
  });

  it("labels each statement with the kind of claim it is", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review(),
    });
    draw();
    expect((await screen.findAllByText("OBSERVED")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("INTERPRETED").length).toBeGreaterThan(0);
    expect(screen.getAllByText("(trades.entry_price)").length).toBeGreaterThan(0);
  });

  it("renders a hypothesis differently from an observation", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review({
        key_factors: [
          { kind: "HYPOTHESIS", statement: "elevated spread may explain the drawdown.", source: null },
        ],
      }),
    });
    draw();
    const line = await screen.findByText(/elevated spread may explain/);
    expect(line.className).toContain("text-muted");
    expect(screen.getByText("HYPOTHESIS")).toBeInTheDocument();
  });

  it("shows an UNKNOWN section neutrally, with the missing data named", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review(),
    });
    draw();
    expect(await screen.findByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.getByText(/no risk decision is linked/)).toBeInTheDocument();
  });

  it("shows the review model and the prediction model separately", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review(),
    });
    draw();
    // Twice each: once as a headline tile, once in the audit metadata.
    expect((await screen.findAllByText("Review model")).length).toBe(2);
    expect(screen.getAllByText("Prediction model").length).toBe(2);
    expect(screen.getByText(/are different/)).toBeInTheDocument();
    expect(screen.getByText("deterministic 1.0")).toBeInTheDocument();
    expect(screen.getByText("trade_probability 2.3")).toBeInTheDocument();
  });

  it("shows the metadata a reader needs to audit the review", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review(),
    });
    draw();
    expect(await screen.findByText("trade_review_prompt_v1")).toBeInTheDocument();
    expect(screen.getByText("Schema")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
  });

  it("says a failed review left the trade untouched", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review({ status: "FAILED", error: "the provider is unreachable", summary: null }),
    });
    draw();
    expect(await screen.findByText(/the provider is unreachable/)).toBeInTheDocument();
    expect(screen.getByText(/trade itself is unaffected/)).toBeInTheDocument();
  });

  it("derives confidence from data completeness rather than tone", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review(),
    });
    draw();
    expect(await screen.findByText("62%")).toBeInTheDocument();
    expect(screen.getByText("from data completeness")).toBeInTheDocument();
    expect(screen.getByText("1 fields missing")).toBeInTheDocument();
  });

  it("offers regeneration and says it adds a version", async () => {
    vi.mocked(reviewService.forTrade).mockResolvedValue({
      trade_id: "t1",
      available: true,
      review: review({ review_version: 2 }),
    });
    draw();
    expect(await screen.findByText("Regenerate")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
  });
});
