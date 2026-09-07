import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ModelTable } from "./ModelTable";
import type { ModelRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return { ...actual, modelService: { ...actual.modelService, list: vi.fn() } };
});

const { modelService } = await import("@/lib/services");

function row(over: Partial<ModelRow> = {}): ModelRow {
  return {
    model: "regime",
    version: "1.0",
    kind: "classifier",
    feature_version: "1.0",
    label_version: null,
    dataset_version: null,
    dataset_fingerprint: null,
    trained_at: null,
    fitted: true,
    contract: {
      features: ["ema_spread_10_50", "atr_pct_14"],
      feature_version: "1.0",
      lookback: 50,
      compatible_feature_versions: [],
      policy: "a missing feature is a MODEL_INPUT_ERROR",
    },
    deterministic: true,
    authority: "advisory",
    ...over,
  };
}

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ModelTable />
    </QueryClientProvider>,
  );
}

describe("ModelTable", () => {
  it("says an empty table is the expected state, not a failure", async () => {
    vi.mocked(modelService.list).mockResolvedValue({ available: true, data: [] });
    draw();
    expect(await screen.findByText(/nothing loads a model by default/)).toBeInTheDocument();
    expect(screen.getByText(/under AI_REQUIRED means no trade/)).toBeInTheDocument();
  });

  it("marks a model with no parameters rather than showing it as ready", async () => {
    vi.mocked(modelService.list).mockResolvedValue({
      available: true,
      data: [row({ fitted: false })],
    });
    draw();
    expect(await screen.findByText("no parameters")).toBeInTheDocument();
  });

  it("does not present a model as trading performance", async () => {
    vi.mocked(modelService.list).mockResolvedValue({ available: true, data: [row()] });
    draw();
    expect(
      await screen.findByText(/Nothing on this page is trading performance/),
    ).toBeInTheDocument();
    expect(screen.getByText(/the verdict type has no field that could/)).toBeInTheDocument();
  });

  it("shows an uncalibrated probability model as not measured", async () => {
    vi.mocked(modelService.list).mockResolvedValue({
      available: true,
      data: [row({ model: "trade_probability", calibrated: false })],
    });
    draw();
    expect(await screen.findByText("not measured")).toBeInTheDocument();
  });
});
