import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AiDecisions, AiPipeline, AiStrategyConfigs } from "./AiStrategyCenter";
import type { AiDecisionRow, AiStrategyConfigRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    aiIntegrationService: {
      ...actual.aiIntegrationService,
      contract: vi.fn(),
      configs: vi.fn(),
      decisions: vi.fn(),
      models: vi.fn(),
    },
  };
});

const { aiIntegrationService } = await import("@/lib/services");

function draw(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function decision(over: Partial<AiDecisionRow> = {}): AiDecisionRow {
  return {
    id: "d1",
    strategy_key: "rsi_reversion",
    strategy_version: 1,
    symbol: "EURUSD",
    timeframe: "H1",
    bar_time: "2026-09-04T10:00:00",
    side: "buy",
    mode: "AI_FILTER",
    policy: "AI_OPTIONAL",
    decision: "ACCEPT",
    status: "OK",
    reason: "probability 0.7800 clears the minimum 0.6000",
    model: "trade_probability v2.1",
    model_key: "trade_probability",
    model_version: "2.1",
    feature_version: "1.0",
    probability: 0.78,
    predicted_class: "WIN",
    regime: "TRENDING_UP",
    anomaly_score: 0.12,
    confidence: 0.78,
    strategy_score: null,
    combined_score: null,
    latency_ms: { features: 3.1, inference: 0.4, total: 3.5 },
    final_outcome: "filled",
    risk_verdict: null,
    order_id: "o1",
    execution_id: "e1",
    created_at: "2026-09-04T10:00:01",
    authority: "advisory.",
    ...over,
  };
}

function configRow(over: Partial<AiStrategyConfigRow> = {}): AiStrategyConfigRow {
  return {
    id: "c1",
    strategy_key: "rsi_reversion",
    account_id: null,
    mode: "AI_FILTER",
    policy: "AI_OPTIONAL",
    enabled: true,
    required_models: [{ key: "trade_probability", version: "2.1" }],
    optional_models: [],
    feature_version: "1.0",
    thresholds: {
      minimum_probability: 0.6,
      maximum_anomaly_score: null,
      allowed_regimes: [],
      minimum_expected_return: null,
      maximum_latency_ms: 2000,
      scoring_method: "minimum",
      ai_weight: 0.5,
      minimum_combined_score: 0.5,
    },
    notes: null,
    updated_at: "2026-09-04T10:00:00",
    authority: "advisory.",
    ...over,
  };
}

const CONTRACT = {
  modes: {
    AI_DISABLED: "no inference runs at all",
    AI_ADVISORY: "recorded; the signal is unchanged",
    AI_FILTER: "may REJECT the signal",
    AI_SCORING: "a named formula",
  },
  policies: { AI_REQUIRED: "no answer means no trade", AI_OPTIONAL: "advice" },
  decisions: ["ACCEPT", "REJECT", "NEUTRAL", "ERROR"] as const,
  scoring_methods: { minimum: "min(strategy, ai)" },
  pipeline: [
    "TRADINGVIEW / STRATEGY",
    "SIGNAL ENGINE",
    "STRATEGY ENGINE",
    "AI STRATEGY FILTER",
    "RISK ENGINE",
    "POSITION SIZING",
    "OMS",
    "BROKER ADAPTER",
    "MT5",
  ],
  guarantees: ["the AI layer runs BEFORE the risk engine and can only decline"],
  does_not: ["place, modify or cancel an order", "enable live trading"],
  risk_authority: "absolute and unchanged.",
  default: "a strategy with no configuration runs exactly as the deterministic strategy does.",
};

describe("AiPipeline", () => {
  it("shows the AI filter sitting before the risk engine, never after", async () => {
    vi.mocked(aiIntegrationService.contract).mockResolvedValue(CONTRACT as never);
    const { container } = draw(<AiPipeline />);
    await screen.findByText("AI STRATEGY FILTER");
    const text = container.textContent ?? "";
    expect(text.indexOf("AI STRATEGY FILTER")).toBeLessThan(text.indexOf("RISK ENGINE"));
    expect(text.indexOf("RISK ENGINE")).toBeLessThan(text.indexOf("OMS"));
  });

  it("says what the AI layer cannot do", async () => {
    vi.mocked(aiIntegrationService.contract).mockResolvedValue(CONTRACT as never);
    draw(<AiPipeline />);
    expect(await screen.findByText(/place, modify or cancel an order/)).toBeInTheDocument();
    expect(screen.getByText(/enable live trading/)).toBeInTheDocument();
    expect(screen.getByText(/The risk engine is final/)).toBeInTheDocument();
  });
});

describe("AiDecisions", () => {
  it("does not present an ACCEPT as an executed trade", async () => {
    vi.mocked(aiIntegrationService.decisions).mockResolvedValue({
      available: true,
      data: [decision({ final_outcome: "risk_vetoed", risk_verdict: "max_daily_loss" })],
    });
    draw(<AiDecisions />);
    // "ACCEPT" also appears in the static caption below the table, so waiting
    // on it would resolve before the query settled. Wait on a row-only value.
    expect(await screen.findByText(/risk_vetoed · max_daily_loss/)).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "ACCEPT" })).toBeInTheDocument();
    expect(screen.getByText(/did not object/)).toBeInTheDocument();
  });

  it("shows the model, the probability, the regime and the latency", async () => {
    vi.mocked(aiIntegrationService.decisions).mockResolvedValue({
      available: true,
      data: [decision()],
    });
    draw(<AiDecisions />);
    expect(await screen.findByText("trade_probability v2.1")).toBeInTheDocument();
    expect(screen.getByText("78%")).toBeInTheDocument();
    expect(screen.getByText("TRENDING_UP")).toBeInTheDocument();
    expect(screen.getByText("4ms")).toBeInTheDocument();
  });

  it("renders an ERROR without a probability", async () => {
    vi.mocked(aiIntegrationService.decisions).mockResolvedValue({
      available: true,
      data: [
        decision({
          decision: "ERROR",
          status: "MODEL_UNAVAILABLE",
          probability: null,
          model: null,
          regime: null,
          anomaly_score: null,
        }),
      ],
    });
    const { container } = draw(<AiDecisions />);
    await screen.findByText("rsi_reversion");
    expect(screen.getByRole("cell", { name: "ERROR" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\d+%/);
  });

  it("distinguishes NEUTRAL from agreement in the caption", async () => {
    vi.mocked(aiIntegrationService.decisions).mockResolvedValue({
      available: true,
      data: [decision({ decision: "NEUTRAL", mode: "AI_ADVISORY" })],
    });
    draw(<AiDecisions />);
    await screen.findByText("rsi_reversion");
    expect(screen.getByRole("cell", { name: "NEUTRAL" })).toBeInTheDocument();
    expect(screen.getByText(/never counted\s+as agreement/)).toBeInTheDocument();
  });
});

describe("AiStrategyConfigs", () => {
  it("shows the mode, the models and the thresholds", async () => {
    vi.mocked(aiIntegrationService.configs).mockResolvedValue({
      available: true,
      data: [configRow()],
    });
    draw(<AiStrategyConfigs />);
    expect(await screen.findByText("AI_FILTER")).toBeInTheDocument();
    expect(screen.getByText("trade_probability v2.1")).toBeInTheDocument();
    expect(screen.getByText("0.60")).toBeInTheDocument();
    expect(screen.getByText("2000ms")).toBeInTheDocument();
  });

  it("shows a switched-off configuration as AI_DISABLED", async () => {
    vi.mocked(aiIntegrationService.configs).mockResolvedValue({
      available: true,
      data: [configRow({ enabled: false })],
    });
    draw(<AiStrategyConfigs />);
    expect(await screen.findByText("AI_DISABLED")).toBeInTheDocument();
  });

  it("offers no control that changes what a bot will decide", async () => {
    vi.mocked(aiIntegrationService.configs).mockResolvedValue({
      available: true,
      data: [configRow()],
    });
    const { container } = draw(<AiStrategyConfigs />);
    await screen.findByText("AI_FILTER");
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(container.querySelectorAll("input")).toHaveLength(0);
    expect(container.querySelectorAll("select")).toHaveLength(0);
  });
});
