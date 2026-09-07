import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TrainingJobs } from "./TrainingJobs";
import type { TrainingJobRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return { ...actual, trainingService: { ...actual.trainingService, list: vi.fn() } };
});

const { trainingService } = await import("@/lib/services");

function row(over: Partial<TrainingJobRow> = {}): TrainingJobRow {
  return {
    id: "job-1",
    model: "trade_probability",
    model_version: "1.0",
    dataset: "eurusd-h1:1",
    dataset_fingerprint: "abcdef0123456789",
    config_fingerprint: "cfg123",
    status: "validation_pending",
    stage: "done",
    progress: 1,
    random_seed: 42,
    model_version_id: "mv-abcdef12",
    started_at: "2026-09-04T10:00:00",
    finished_at: "2026-09-04T10:00:07",
    error: null,
    candidate_only: "a candidate exists",
    ...over,
  };
}

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TrainingJobs />
    </QueryClientProvider>,
  );
}

describe("TrainingJobs", () => {
  it("does not present a finished run as an approved model", async () => {
    vi.mocked(trainingService.list).mockResolvedValue({ available: true, data: [row()] });
    draw();
    expect(await screen.findByText("validation_pending")).toBeInTheDocument();
    // The sentence is split by a <strong>, so match a fragment inside one node.
    expect(
      screen.getByText(/L26 validates it and L28 promotes it/),
    ).toBeInTheDocument();
  });

  it("shows the candidate as a draft, never as a promoted model", async () => {
    vi.mocked(trainingService.list).mockResolvedValue({ available: true, data: [row()] });
    draw();
    expect(await screen.findByText(/draft · mv-abcde/)).toBeInTheDocument();
  });

  it("shows a failed run as leaving no candidate", async () => {
    vi.mocked(trainingService.list).mockResolvedValue({
      available: true,
      data: [
        row({
          status: "failed",
          model_version_id: null,
          error: "data quality gates refused this run",
        }),
      ],
    });
    draw();
    expect(await screen.findByText("none — failed")).toBeInTheDocument();
  });

  it("says a closed browser stops nothing", async () => {
    vi.mocked(trainingService.list).mockResolvedValue({ available: true, data: [] });
    draw();
    expect(await screen.findByText(/Closing this page stops nothing/)).toBeInTheDocument();
  });
});
