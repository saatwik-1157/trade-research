import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminPanel } from "./AdminPanel";
import type { AdminDashboard, AdminUserRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    adminService: {
      dashboard: vi.fn(),
      contract: vi.fn(),
      permissions: vi.fn(),
      integrations: vi.fn(),
      configuration: vi.fn(),
      users: vi.fn(),
      user: vi.fn(),
      sessions: vi.fn(),
      deactivateUser: vi.fn(),
      activateUser: vi.fn(),
      revokeSessions: vi.fn(),
      setRole: vi.fn(),
      auditLogs: vi.fn(),
      auditActions: vi.fn(),
    },
  };
});

const { adminService } = await import("@/lib/services");

const SECRET = "broker-password-do-not-leak";

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AdminPanel />
    </QueryClientProvider>,
  );
}

function dashboard(over: Partial<AdminDashboard> = {}): AdminDashboard {
  return {
    environment: {
      environment: "development",
      trading_mode: "paper",
      live_trading: false,
      live_execution_allowed: false,
      live_execution_blockers: ["gate not built: kill_switch", "LIVE_TRADING is false"],
      note: "live execution is derived from gates that are built and verified.",
    },
    users: { total: 1, active: 1, by_role: { admin: 1 } },
    accounts: { paper: 0, broker: 0, mt5_connections: 0 },
    bots: { total: 0, enabled: 0, runs_live: 0 },
    strategies: { total: 0, active: 0 },
    models: { versions: 0, deployments_active: 0 },
    trading: {
      trades: 0,
      positions_open: 0,
      orders_working: 0,
      orders_needing_reconciliation: 0,
    },
    notifications: { total: 0, deliveries_queued: 0, deliveries_failed: 0 },
    authority: "administrative visibility.",
    ...over,
  };
}

function user(over: Partial<AdminUserRow> = {}): AdminUserRow {
  return {
    id: "u2",
    email: "victim@tr-platform.io",
    role: "user",
    is_active: true,
    created_at: "2026-09-01T00:00:00",
    last_login_at: null,
    ...over,
  };
}

function seed() {
  vi.mocked(adminService.dashboard).mockResolvedValue(dashboard());
  vi.mocked(adminService.contract).mockResolvedValue({
    environment: {},
    delegates_to: { bots: "/v1/bots — start, pause, disable (BotManager, L22)" },
    delegation_note: "each of those enforces its own permission.",
    writes_here: ["activate a user"],
    dangerous_actions_require: ["a reason"],
    cannot: ["enable live trading", "place, modify or cancel an order"],
    roles: ["user", "trader", "admin"],
    mfa: "NOT IMPLEMENTED.",
  });
  vi.mocked(adminService.users).mockResolvedValue({
    items: [user()],
    page: { total: 1, limit: 25, offset: 0, has_more: false },
  });
  vi.mocked(adminService.auditLogs).mockResolvedValue({
    items: [
      {
        id: "a1",
        actor_user_id: "u1",
        action: "user_deactivated",
        resource_type: "user",
        resource_id: "u2",
        occurred_at: "2026-09-05T12:00:00",
        ip: null,
        request_id: "r1",
        details: { reason: "offboarding after the contract ended" },
      },
    ],
    page: { total: 1, limit: 25, offset: 0, has_more: false },
  });
  vi.mocked(adminService.auditActions).mockResolvedValue({
    actions: [{ action: "user_deactivated", count: 1 }],
    retention: "none is applied automatically.",
    immutability: "there is no route that updates or deletes an audit row.",
  });
  vi.mocked(adminService.integrations).mockResolvedValue({
    notifications: { enabled: true, channels: [], consumer: null },
    email: { configured: false, enabled: true },
    discord: { channel: "DISCORD", state: "NOT_CONFIGURED", available: false, detail: "" },
    tradingview_webhook: { secret_configured: false, restrict_to_tradingview_ips: false },
    event_bus: { kind: "in_memory", enabled: true },
    brokers: {
      adapters_registered: 0,
      order_managers_registered: 0,
      note: "an adapter is registered by an operator action.",
    },
    never_returned: ["database URL or password", "Discord webhook URL or bot token"],
  });
  vi.mocked(adminService.permissions).mockResolvedValue({
    roles: [{ role: "admin", permissions: ["manage_users"], count: 1 }],
    permissions: [{ permission: "manage_users", min_role: "admin", dangerous: true }],
    note: "a route asks for a permission, not a role.",
    mfa: "NOT IMPLEMENTED. This platform has no multi-factor authentication.",
  });
  vi.mocked(adminService.configuration).mockResolvedValue({
    read_only: { trading_mode: "paper" },
    live_gates: {
      gates: { kill_switch: false, risk_engine_veto: false },
      all_built: false,
      note: "code, not configuration.",
    },
    runtime_editable: [],
    runtime_editable_note: "nothing. Configuration is environment variables read at startup.",
    feature_flags: {
      implemented: false,
      why_not: "a database-backed flag changes behaviour without a deployment.",
    },
  });
}

describe("the environment banner", () => {
  it("shows PAPER and DISABLED unmissably", async () => {
    seed();
    draw();
    const banner = await screen.findByLabelText("trading environment");
    expect(banner).toHaveTextContent("PAPER");
    expect(banner).toHaveTextContent("DISABLED");
    expect(banner).toHaveTextContent("BLOCKED");
    expect(banner.className).not.toContain("border-critical");
  });

  it("makes a live configuration visually distinct", async () => {
    seed();
    vi.mocked(adminService.dashboard).mockResolvedValue(
      dashboard({
        environment: {
          environment: "production",
          trading_mode: "live",
          live_trading: true,
          live_execution_allowed: false,
          live_execution_blockers: ["gate not built: kill_switch"],
          note: "n",
        },
      }),
    );
    draw();
    const banner = await screen.findByLabelText("trading environment");
    expect(banner).toHaveTextContent("LIVE");
    expect(banner.className).toContain("border-critical");
  });

  it("lists the gates that are not built rather than only saying blocked", async () => {
    seed();
    draw();
    expect(await screen.findByText("2 gate(s) not built")).toBeInTheDocument();
  });
});

describe("the overview", () => {
  it("shows zeros rather than examples on an empty platform", async () => {
    seed();
    draw();
    await screen.findByLabelText("trading environment");
    expect(await screen.findByText("Bots")).toBeInTheDocument();
    // Every trading count is zero and none of them is a placeholder dash.
    const tiles = screen.getAllByText("0");
    expect(tiles.length).toBeGreaterThan(5);
  });

  it("names where each control actually lives rather than wrapping it", async () => {
    seed();
    draw();
    expect(await screen.findByText("bots")).toBeInTheDocument();
    expect(screen.getByText(/BotManager, L22/)).toBeInTheDocument();
  });

  it("states what the panel cannot do", async () => {
    seed();
    draw();
    expect(await screen.findByText("— enable live trading")).toBeInTheDocument();
    expect(screen.getByText("— place, modify or cancel an order")).toBeInTheDocument();
  });

  it("surfaces an error rather than an empty dashboard", async () => {
    seed();
    vi.mocked(adminService.dashboard).mockRejectedValue(new Error("403 forbidden"));
    draw();
    expect((await screen.findAllByText("403 forbidden")).length).toBeGreaterThan(0);
  });
});

describe("user management", () => {
  it("lists users and offers deactivation", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Users"));
    expect(await screen.findByText("victim@tr-platform.io")).toBeInTheDocument();
    expect(screen.getByText("Deactivate")).toBeInTheDocument();
  });

  it("will not confirm without a reason and the exact email", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Users"));
    fireEvent.click(await screen.findByText("Deactivate"));
    const confirm = await screen.findByText("Confirm");
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText("Why is this happening?"), {
      target: { value: "short" },
    });
    fireEvent.change(screen.getByPlaceholderText("victim@tr-platform.io"), {
      target: { value: "victim@tr-platform.io" },
    });
    expect(screen.getByText("Confirm")).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText("Why is this happening?"), {
      target: { value: "offboarding after the contract ended" },
    });
    expect(screen.getByText("Confirm")).toBeEnabled();
  });

  it("says that deactivation keeps every record", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Users"));
    fireEvent.click(await screen.findByText("Deactivate"));
    expect(
      await screen.findByText(/trades, journal and analytics are kept/),
    ).toBeInTheDocument();
  });

  it("sends the reason and the phrase to the backend", async () => {
    seed();
    vi.mocked(adminService.deactivateUser).mockResolvedValue({ is_active: false });
    draw();
    fireEvent.click(await screen.findByText("Users"));
    fireEvent.click(await screen.findByText("Deactivate"));
    fireEvent.change(screen.getByPlaceholderText("Why is this happening?"), {
      target: { value: "offboarding after the contract ended" },
    });
    fireEvent.change(screen.getByPlaceholderText("victim@tr-platform.io"), {
      target: { value: "victim@tr-platform.io" },
    });
    fireEvent.click(screen.getByText("Confirm"));
    await waitFor(() =>
      expect(adminService.deactivateUser).toHaveBeenCalledWith(
        "u2",
        "offboarding after the contract ended",
        "victim@tr-platform.io",
      ),
    );
  });

  it("shows the backend's refusal rather than claiming success", async () => {
    seed();
    vi.mocked(adminService.deactivateUser).mockRejectedValue(
      new Error("this is the last active administrator."),
    );
    draw();
    fireEvent.click(await screen.findByText("Users"));
    fireEvent.click(await screen.findByText("Deactivate"));
    fireEvent.change(screen.getByPlaceholderText("Why is this happening?"), {
      target: { value: "offboarding after the contract ended" },
    });
    fireEvent.change(screen.getByPlaceholderText("victim@tr-platform.io"), {
      target: { value: "victim@tr-platform.io" },
    });
    fireEvent.click(screen.getByText("Confirm"));
    expect(
      await screen.findByText("this is the last active administrator."),
    ).toBeInTheDocument();
  });

  it("says the list is empty rather than inventing a user", async () => {
    seed();
    vi.mocked(adminService.users).mockResolvedValue({
      items: [],
      page: { total: 0, limit: 25, offset: 0, has_more: false },
    });
    draw();
    fireEvent.click(await screen.findByText("Users"));
    expect(await screen.findByText("No users found.")).toBeInTheDocument();
  });
});

describe("the audit trail", () => {
  it("shows the reason recorded with each action", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Audit trail"));
    expect(await screen.findByText("user_deactivated")).toBeInTheDocument();
    expect(
      screen.getByText("offboarding after the contract ended"),
    ).toBeInTheDocument();
  });

  it("states that no route edits or deletes a row", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Audit trail"));
    expect(
      await screen.findByText("there is no route that updates or deletes an audit row."),
    ).toBeInTheDocument();
  });
});

describe("integrations and configuration", () => {
  it("reports configured states without any secret", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Integrations"));
    await screen.findByText("Discord");
    expect(document.body.textContent).not.toContain(SECRET);
    expect(screen.getByText("NO SECRET — REFUSING")).toBeInTheDocument();
    expect(screen.getByText("— database URL or password")).toBeInTheDocument();
  });

  it("offers no field that would edit configuration", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Roles & config"));
    await screen.findByText(/Configuration is environment variables/);
    expect(document.querySelectorAll("input").length).toBe(0);
  });

  it("shows every live gate as not built", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Roles & config"));
    await screen.findByText("kill_switch");
    expect(screen.getAllByText("NOT BUILT").length).toBe(2);
    expect(screen.queryByText("BUILT")).toBeNull();
  });

  it("shows the MFA gap rather than implying it exists", async () => {
    seed();
    draw();
    fireEvent.click(await screen.findByText("Roles & config"));
    expect(
      await screen.findByText(/NOT IMPLEMENTED. This platform has no multi-factor/),
    ).toBeInTheDocument();
  });
});
