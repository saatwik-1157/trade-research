import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DatasetTable } from "./DatasetTable";
import type { DatasetRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    datasetService: {
      ...actual.datasetService,
      list: vi.fn(),
    },
  };
});

const { datasetService } = await import("@/lib/services");

function row(over: Partial<DatasetRow> = {}): DatasetRow {
  return {
    id: "d1",
    key: "eurusd-h1",
    version: "1",
    status: "READY",
    ready: true,
    provider: "mt5",
    timeframe: "H1",
    rows: 338,
    start: "2026-01-03T02:00:00",
    end: "2026-01-17T03:00:00",
    quality_score: 100,
    fingerprint: "abcdef0123456789abcdef",
    feature_set_version: "1.0",
    label_set_version: "1.0",
    leakage_passed: true,
    leakage_failed_checks: 0,
    blocked_reason: null,
    created_at: "2026-09-04T05:00:00",
    ...over,
  };
}

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DatasetTable />
    </QueryClientProvider>,
  );
}

describe("DatasetTable", () => {
  it("shows a failing leakage check rather than a status a reader must interpret", async () => {
    vi.mocked(datasetService.list).mockResolvedValue({
      available: true,
      data: [
        row({
          status: "CLEAN",
          ready: false,
          leakage_passed: false,
          leakage_failed_checks: 2,
          fingerprint: null,
          blocked_reason: "no_future_influence: row 41 changed when later bars were appended",
        }),
      ],
    });
    draw();
    expect(await screen.findByText("2 failed ✗")).toBeInTheDocument();
    expect(screen.getByText("CLEAN")).toBeInTheDocument();
  });

  it("shows no fingerprint for a dataset whose rows were never built", async () => {
    vi.mocked(datasetService.list).mockResolvedValue({
      available: true,
      data: [row({ status: "RAW", ready: false, rows: 0, fingerprint: null, leakage_passed: null })],
    });
    draw();
    expect(await screen.findByText("not run")).toBeInTheDocument();
  });

  it("says that nothing on the page can override the verdict", async () => {
    vi.mocked(datasetService.list).mockResolvedValue({ available: true, data: [row()] });
    draw();
    expect(await screen.findByText(/no caller — including this page — can override/)).toBeInTheDocument();
  });
});
