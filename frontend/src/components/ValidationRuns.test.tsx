import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ValidationRuns } from "./ValidationRuns";
import type { ValidationRunRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    validationService: { ...actual.validationService, list: vi.fn(), report: vi.fn() },
  };
});

const { validationService } = await import("@/lib/services");

function row(over: Partial<ValidationRunRow> = {}): ValidationRunRow {
  return {
    id: "run-1",
    model_version_id: "mv-abcdef123456",
    training_run_id: "job-1",
    dataset_id: "ds-1",
    dataset_fingerprint: "abcdef0123456789",
    status: "completed",
    verdict: "PASS",
    summary: "PASS on all 15 checks",
    stage: "done",
    progress: 1,
    checks: { PASS: 15, WARNING: 0, FAIL: 0, BLOCKED: 0 },
    validation_engine_version: "1.0.0",
    config_fingerprint: "cfg123",
    created_at: "2026-09-04T10:00:00",
    started_at: "2026-09-04T10:00:01",
    finished_at: "2026-09-04T10:00:09",
    error: null,
    authority: "advisory. A PASS makes a candidate eligible for CONSIDERATION.",
    ...over,
  };
}

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ValidationRuns />
    </QueryClientProvider>,
  );
}

describe("ValidationRuns", () => {
  it("does not present a PASS as an approval to trade", async () => {
    vi.mocked(validationService.list).mockResolvedValue({ available: true, data: [row()] });
    draw();
    expect(await screen.findByText("PASS")).toBeInTheDocument();
    expect(screen.getByText(/may be/)).toBeInTheDocument();
    expect(screen.getByText(/considered/)).toBeInTheDocument();
  });

  it("says that BLOCKED is not FAIL", async () => {
    vi.mocked(validationService.list).mockResolvedValue({
      available: true,
      data: [row({ verdict: "BLOCKED", summary: "BLOCKED on sample_size" })],
    });
    draw();
    expect(await screen.findByText("BLOCKED")).toBeInTheDocument();
    expect(screen.getByText(/is not FAIL/)).toBeInTheDocument();
  });

  it("shows no score anywhere, because the backend produces none", async () => {
    vi.mocked(validationService.list).mockResolvedValue({
      available: true,
      data: [row({ checks: { PASS: 12, WARNING: 2, FAIL: 1, BLOCKED: 0 } })],
    });
    const { container } = draw();
    expect(await screen.findByText("12P")).toBeInTheDocument();
    // No percentage, no "/100", no composite of any kind.
    expect(container.textContent).not.toMatch(/\d+\s*\/\s*100/);
    expect(container.textContent).not.toMatch(/score/i);
  });

  it("renders a run still in flight by its stage rather than a blank verdict", async () => {
    vi.mocked(validationService.list).mockResolvedValue({
      available: true,
      data: [
        row({
          status: "running",
          verdict: null,
          summary: null,
          stage: "economic",
          progress: 0.6,
          checks: { PASS: null, WARNING: null, FAIL: null, BLOCKED: null },
        }),
      ],
    });
    draw();
    expect(await screen.findByText("running")).toBeInTheDocument();
    expect(screen.getByText(/economic · 60%/)).toBeInTheDocument();
  });

  it("shows a failed run's error rather than an empty verdict", async () => {
    vi.mocked(validationService.list).mockResolvedValue({
      available: true,
      data: [
        row({
          status: "failed",
          verdict: null,
          summary: null,
          error: "ValidationError: no model version mv-1",
        }),
      ],
    });
    draw();
    expect(await screen.findByText(/no model version mv-1/)).toBeInTheDocument();
  });

  it("renders the report as named checks and quotes what the verdict means", async () => {
    vi.mocked(validationService.list).mockResolvedValue({
      available: true,
      data: [row({ verdict: "CONDITIONAL", summary: "CONDITIONAL: warnings on calibration" })],
    });
    vi.mocked(validationService.report).mockResolvedValue({
      run_id: "run-1",
      status: "completed",
      report: {
        validation_engine_version: "1.0.0",
        verdict: "CONDITIONAL",
        summary: "CONDITIONAL: warnings on calibration",
        means: "every hard requirement is met and at least one check returned a warning.",
        authority: "advisory and terminal.",
        checks: [
          { check: "leakage", severity: "PASS", summary: "all 6 checks pass", evidence: {} },
          {
            check: "calibration",
            severity: "WARNING",
            summary: "expected calibration error 0.14 exceeds 0.1",
            evidence: {},
          },
        ],
        counts: { PASS: 14, WARNING: 1, FAIL: 0, BLOCKED: 0 },
        recommendations: ["calibration is a caveat on how to read this model"],
        config: {},
        context: {},
        method: {
          scoring: "there is no score.",
          blocked: "BLOCKED means a check could not be evaluated.",
          separation: "ML and economic metrics are never merged.",
          reproducibility: "every random draw is seeded.",
        },
      },
    });
    draw();
    (await screen.findByRole("button", { name: "report" })).click();
    expect(await screen.findByText("calibration")).toBeInTheDocument();
    expect(screen.getByText(/expected calibration error 0.14/)).toBeInTheDocument();
    expect(screen.getByText(/every hard requirement is met/)).toBeInTheDocument();
  });

  it("offers no control that promotes, activates or deploys a model", async () => {
    vi.mocked(validationService.list).mockResolvedValue({ available: true, data: [row()] });
    const { container } = draw();
    await screen.findByText("PASS");
    const labels = Array.from(container.querySelectorAll("button")).map((b) =>
      (b.textContent ?? "").toLowerCase(),
    );
    for (const word of ["promote", "activate", "deploy", "approve", "go live"]) {
      expect(labels.some((label) => label.includes(word))).toBe(false);
    }
  });
});
