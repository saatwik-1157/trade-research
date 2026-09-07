import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SystemHealth } from "./SystemHealth";
import type { ComponentRow, MonitoringSummary } from "@/lib/services";
import type { User } from "@/lib/types";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    monitoringService: {
      summary: vi.fn(),
      components: vi.fn(),
      component: vi.fn(),
      events: vi.fn(),
      metrics: vi.fn(),
      thresholds: vi.fn(),
      uptime: vi.fn(),
      contract: vi.fn(),
      collect: vi.fn(),
    },
  };
});

vi.mock("@/hooks/useMe", () => ({ useMe: vi.fn() }));

const { monitoringService, ApiError } = await import("@/lib/services");
const { useMe } = await import("@/hooks/useMe");

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SystemHealth />
    </QueryClientProvider>,
  );
}

function signedInAs(role: User["role"]) {
  vi.mocked(useMe).mockReturnValue({
    data: { id: "u1", email: "a@b.io", role, is_active: true },
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useMe>);
}

function summary(over: Partial<MonitoringSummary> = {}): MonitoringSummary {
  return {
    status: "HEALTHY",
    trading_safety: "SAFE",
    trading_safety_reasons: ["every authoritative component is healthy"],
    trading_safety_note: "observational. Nothing in the platform reads this to decide.",
    environment: {
      environment: "development",
      trading_mode: "paper",
      live_trading: false,
      live_execution_allowed: false,
      live_execution_blockers: ["gate not built: kill_switch"],
    },
    collected_at: "2026-09-05T12:00:00",
    duration_ms: 12.5,
    components: 12,
    not_healthy: [],
    ...over,
  };
}

function row(over: Partial<ComponentRow> = {}): ComponentRow {
  return {
    name: "database",
    layer: "INFRASTRUCTURE",
    status: "HEALTHY",
    criticality: "CRITICAL",
    detail: "SELECT 1 ok",
    last_checked: "2026-09-05T12:00:00",
    latency_ms: 3.2,
    error_count: 0,
    last_error: null,
    facts: {},
    ...over,
  };
}

beforeEach(() => {
  // Each test asserts about calls made by that test. A shared mock keeps a
  // call log across the file, and "was never called" would be false for
  // reasons that have nothing to do with the test making the claim.
  vi.clearAllMocks();
});

function seed(rows: ComponentRow[] = [row()]) {
  vi.mocked(monitoringService.summary).mockResolvedValue(summary());
  vi.mocked(monitoringService.components).mockResolvedValue({
    collected_at: "2026-09-05T12:00:00",
    layers: { INFRASTRUCTURE: rows },
    components: rows,
  });
  vi.mocked(monitoringService.events).mockResolvedValue({
    events: [
      {
        id: "e1",
        component: "database",
        event_type: "SERVICE_DOWN",
        level: "critical",
        occurred_at: "2026-09-05T11:00:00",
        correlation_id: null,
        payload: { detail: "unavailable" },
      },
    ],
    retention: "none is applied automatically.",
  });
}

describe("the health summary", () => {
  it("shows the overall state and the derived trading safety", async () => {
    signedInAs("admin");
    seed();
    draw();
    // The word appears on the overall tile and on the database component.
    await waitFor(() => expect(screen.getAllByText("HEALTHY").length).toBe(2));
    expect(screen.getAllByText("SAFE").length).toBeGreaterThan(0);
  });

  it("always shows the trading mode and whether live trading is on", async () => {
    signedInAs("user");
    seed();
    draw();
    expect(await screen.findByText("PAPER")).toBeInTheDocument();
    expect(screen.getByText("DISABLED")).toBeInTheDocument();
    expect(screen.getByText("1 gate(s) not built")).toBeInTheDocument();
  });

  it("shows the reasons behind the safety verdict rather than only the word", async () => {
    signedInAs("admin");
    seed();
    vi.mocked(monitoringService.summary).mockResolvedValue(
      summary({
        trading_safety: "UNKNOWN",
        trading_safety_reasons: ["broker could not be observed"],
      }),
    );
    draw();
    expect(await screen.findByText("— broker could not be observed")).toBeInTheDocument();
  });

  it("renders an error rather than an empty green board", async () => {
    signedInAs("admin");
    seed();
    vi.mocked(monitoringService.summary).mockRejectedValue(new Error("API unreachable"));
    draw();
    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });

  it("says UNKNOWN when nothing has been collected", async () => {
    signedInAs("admin");
    seed();
    vi.mocked(monitoringService.summary).mockResolvedValue(
      summary({
        status: "UNKNOWN",
        trading_safety: "UNKNOWN",
        collected_at: null,
        note: "nothing has been collected. This is UNKNOWN rather than HEALTHY.",
      }),
    );
    draw();
    expect(await screen.findByText("never collected")).toBeInTheDocument();
    expect(
      screen.getByText("nothing has been collected. This is UNKNOWN rather than HEALTHY."),
    ).toBeInTheDocument();
  });
});

describe("the component list", () => {
  it("renders every state distinctly", async () => {
    signedInAs("admin");
    seed([
      row({ name: "database", status: "HEALTHY" }),
      row({ name: "redis", status: "DEGRADED", detail: "slow" }),
      row({ name: "broker", status: "NOT_CONFIGURED", detail: "no adapter registered" }),
      row({ name: "market_data.freshness", status: "UNKNOWN", detail: "no bar stored" }),
    ]);
    draw();
    expect(await screen.findByText("no adapter registered")).toBeInTheDocument();
    expect(screen.getByText("NOT_CONFIGURED")).toBeInTheDocument();
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.getByText("DEGRADED")).toBeInTheDocument();
  });

  it("expands one component to show what was observed", async () => {
    signedInAs("admin");
    seed([row({ name: "oms", status: "DEGRADED", facts: { orders_unknown: 1 } })]);
    draw();
    fireEvent.click(await screen.findByText("oms"));
    expect(await screen.findByText("orders_unknown")).toBeInTheDocument();
  });

  it("says the detail needs the admin role rather than showing an empty box", async () => {
    signedInAs("trader");
    seed();
    draw();
    expect(
      await screen.findByText("Detailed component health requires the admin role."),
    ).toBeInTheDocument();
    expect(monitoringService.components).not.toHaveBeenCalled();
  });

  it("handles a 403 from the backend as a permission message", async () => {
    signedInAs("admin");
    seed();
    vi.mocked(monitoringService.components).mockRejectedValue(
      new ApiError(403, "requires the manage_system_settings permission"),
    );
    draw();
    expect(
      await screen.findByText("Detailed component health requires the admin role."),
    ).toBeInTheDocument();
  });

  it("says nothing has been collected rather than showing a healthy board", async () => {
    signedInAs("admin");
    seed();
    vi.mocked(monitoringService.components).mockResolvedValue({
      collected_at: null,
      layers: {},
      components: [],
    });
    draw();
    expect(await screen.findByText("No collection has run yet.")).toBeInTheDocument();
  });
});

describe("incidents", () => {
  it("lists recent incidents with their level", async () => {
    signedInAs("admin");
    seed();
    draw();
    expect(await screen.findByText("SERVICE_DOWN")).toBeInTheDocument();
    expect(screen.getByText("critical")).toBeInTheDocument();
  });

  it("says none recorded rather than inventing one", async () => {
    signedInAs("admin");
    seed();
    vi.mocked(monitoringService.events).mockResolvedValue({
      events: [],
      retention: "none",
    });
    draw();
    expect(await screen.findByText("No incidents recorded.")).toBeInTheDocument();
  });
});

describe("collecting on demand", () => {
  it("is offered to an administrator and calls the backend", async () => {
    signedInAs("admin");
    seed();
    vi.mocked(monitoringService.collect).mockResolvedValue({
      ...summary(),
      incidents: [],
    });
    draw();
    fireEvent.click(await screen.findByText("Collect now"));
    await waitFor(() => expect(monitoringService.collect).toHaveBeenCalled());
  });

  it("is not offered to anyone else", async () => {
    signedInAs("user");
    seed();
    draw();
    await screen.findByText("PAPER");
    expect(screen.queryByText("Collect now")).toBeNull();
  });
});
