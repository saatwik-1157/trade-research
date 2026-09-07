import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ModelAlerts, ModelHealth, MonitoringSnapshots } from "./ModelMonitoring";
import type {
  ModelAlertRow,
  ModelHealthRow,
  MonitoringSnapshotRow,
} from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    modelMonitoringService: {
      ...actual.modelMonitoringService,
      contract: vi.fn(),
      health: vi.fn(),
      snapshots: vi.fn(),
      alerts: vi.fn(),
    },
  };
});

const { modelMonitoringService } = await import("@/lib/services");

function draw(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function healthRow(over: Partial<ModelHealthRow> = {}): ModelHealthRow {
  return {
    model: "trade_probability",
    version: "2.3",
    model_version_id: "mv1",
    environment: "paper",
    scope: { strategy_key: "breakout", symbol: "EURUSD", timeframe: "H1" },
    health: "WARNING",
    measured_at: "2026-09-04T12:00:00",
    sample_count: 1284,
    note: null,
    ...over,
  };
}

function alertRow(over: Partial<ModelAlertRow> = {}): ModelAlertRow {
  return {
    id: "a1",
    fingerprint: "f1",
    model: "trade_probability",
    version: "2.3",
    check: "feature_drift",
    subject: "rsi_14",
    severity: "warning",
    status: "firing",
    title: "warning: feature_drift on rsi_14",
    body: "PSI 0.31",
    metric: "feature_drift",
    current_value: 0.31,
    baseline_value: null,
    threshold: 0.25,
    sample_size: 450,
    first_seen_at: "2026-09-04T09:00:00",
    last_seen_at: "2026-09-04T12:00:00",
    resolved_at: null,
    occurrences: 4,
    authority: "an alert is a message.",
    ...over,
  };
}

function snapshotRow(over: Partial<MonitoringSnapshotRow> = {}): MonitoringSnapshotRow {
  return {
    id: "s1",
    model: "trade_probability",
    version: "2.3",
    model_version_id: "mv1",
    scope: { strategy_key: null, symbol: null, timeframe: null, environment: "paper" },
    health: "HEALTHY",
    baseline: { kind: "VALIDATION", id: "run-1", period: [null, null] },
    window: { start: "2026-08-28T12:00:00", end: "2026-09-04T12:00:00" },
    sample_count: 1284,
    metrics: {
      feature: null,
      prediction: { findings: [], n: 1284, severity: "ok" },
      calibration: null,
      performance: null,
      latency: { findings: [], n: 1284, severity: "ok" },
      availability: null,
      trading: null,
      regime: null,
    },
    health_detail: null,
    created_at: "2026-09-04T12:00:05",
    note: null,
    ...over,
  };
}

describe("ModelHealth", () => {
  it("shows the health state with its sample size", async () => {
    vi.mocked(modelMonitoringService.health).mockResolvedValue({
      available: true,
      data: [healthRow()],
    });
    draw(<ModelHealth />);
    expect(await screen.findByText("WARNING")).toBeInTheDocument();
    expect(screen.getByText("1284")).toBeInTheDocument();
    expect(screen.getByText("trade_probability v2.3")).toBeInTheDocument();
  });

  it("does not present INSUFFICIENT_DATA as a pass", async () => {
    vi.mocked(modelMonitoringService.health).mockResolvedValue({
      available: true,
      data: [healthRow({ health: "INSUFFICIENT_DATA", sample_count: 0, measured_at: null })],
    });
    draw(<ModelHealth />);
    expect(await screen.findByText("INSUFFICIENT_DATA")).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
    expect(screen.getByText(/is not a pass/)).toBeInTheDocument();
  });

  it("says OFFLINE is a registry fact, not a clean bill of health", async () => {
    vi.mocked(modelMonitoringService.health).mockResolvedValue({
      available: true,
      data: [healthRow({ health: "OFFLINE" })],
    });
    draw(<ModelHealth />);
    expect(await screen.findByText("OFFLINE")).toBeInTheDocument();
    expect(screen.getByText(/is not a clean bill of health/)).toBeInTheDocument();
  });
});

describe("ModelAlerts", () => {
  it("shows the value beside the threshold it crossed, and the sample", async () => {
    vi.mocked(modelMonitoringService.alerts).mockResolvedValue({
      available: true,
      data: [alertRow()],
    });
    draw(<ModelAlerts />);
    expect(await screen.findByText("0.31 / 0.25")).toBeInTheDocument();
    expect(screen.getByText("450")).toBeInTheDocument();
    expect(screen.getByText("feature_drift · rsi_14")).toBeInTheDocument();
  });

  it("keeps a resolved alert with the time it cleared", async () => {
    vi.mocked(modelMonitoringService.alerts).mockResolvedValue({
      available: true,
      data: [alertRow({ status: "resolved", resolved_at: "2026-09-04T14:20:00" })],
    });
    draw(<ModelAlerts />);
    expect(await screen.findByText("resolved 14:20:00")).toBeInTheDocument();
  });

  it("says an alert never replaces a model", async () => {
    vi.mocked(modelMonitoringService.alerts).mockResolvedValue({
      available: true,
      data: [alertRow()],
    });
    draw(<ModelAlerts />);
    await screen.findByText("0.31 / 0.25");
    expect(screen.getByText(/never retrains, promotes,/)).toBeInTheDocument();
  });

  it("shows occurrences rather than one row per observation", async () => {
    vi.mocked(modelMonitoringService.alerts).mockResolvedValue({
      available: true,
      data: [alertRow({ occurrences: 12 })],
    });
    draw(<ModelAlerts />);
    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(screen.getByText(/One row per/)).toBeInTheDocument();
  });
});

describe("MonitoringSnapshots", () => {
  it("names the baseline a snapshot was measured against", async () => {
    vi.mocked(modelMonitoringService.snapshots).mockResolvedValue({
      available: true,
      data: [snapshotRow()],
    });
    draw(<MonitoringSnapshots />);
    expect(await screen.findByText("VALIDATION")).toBeInTheDocument();
    expect(screen.getByText(/never changed silently/)).toBeInTheDocument();
  });

  it("shows a missing baseline as none available, not as a comparison", async () => {
    vi.mocked(modelMonitoringService.snapshots).mockResolvedValue({
      available: true,
      data: [
        snapshotRow({
          baseline: { kind: null, id: null, period: [null, null] },
          health: "INSUFFICIENT_DATA",
        }),
      ],
    });
    draw(<MonitoringSnapshots />);
    expect(await screen.findByText("none available")).toBeInTheDocument();
  });

  it("lists only the metric blocks that were actually computed", async () => {
    vi.mocked(modelMonitoringService.snapshots).mockResolvedValue({
      available: true,
      data: [snapshotRow()],
    });
    draw(<MonitoringSnapshots />);
    // `feature` and `calibration` are null on this snapshot and must not appear.
    expect(await screen.findByText("prediction, latency")).toBeInTheDocument();
  });
});
