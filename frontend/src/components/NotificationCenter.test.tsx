import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NotificationBell, worstOf } from "./NotificationBell";
import { NotificationCenter } from "./NotificationCenter";
import { notificationHref } from "@/lib/services";
import type {
  NotificationPreferenceRow,
  NotificationRow,
  NotificationSeverity,
} from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    notificationService: {
      list: vi.fn(),
      unreadCount: vi.fn(),
      markRead: vi.fn(),
      markAllRead: vi.fn(),
      preferences: vi.fn(),
      savePreferences: vi.fn(),
      deliveries: vi.fn(),
      channels: vi.fn(),
      contract: vi.fn(),
    },
  };
});

// The centre opens a WebSocket for live nudges. jsdom has none; the client
// already handles that by reporting DISCONNECTED, and the tests assert the
// page still renders from the API rather than depending on a socket.
const { notificationService } = await import("@/lib/services");

function draw(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function row(over: Partial<NotificationRow> = {}): NotificationRow {
  return {
    id: "n1",
    event_id: "e1",
    event_type: "TRADE_RECORDED",
    category: "TRADING",
    severity: "INFO",
    environment: "paper",
    title: "[PAPER] Trade recorded",
    body: "BUY EURUSD was journalled. Result 42.30 (1.4R).",
    entity_type: "trade",
    entity_id: "t1",
    read: false,
    read_at: null,
    created_at: "2026-09-05T12:00:00",
    context: {},
    ...over,
  };
}

function preference(over: Partial<NotificationPreferenceRow> = {}): NotificationPreferenceRow {
  return {
    category: "TRADING",
    channel: "EMAIL",
    enabled: true,
    min_severity: "CRITICAL",
    source: "default",
    locked: false,
    ...over,
  };
}

function page(items: NotificationRow[], total = items.length) {
  return { items, page: { total, limit: 25, offset: 0, has_more: false } };
}

// ------------------------------------------------------------------- unit


describe("severity ranking", () => {
  it("picks the worst unread severity, not the most common one", () => {
    expect(worstOf({ INFO: 11, CRITICAL: 1 })).toBe("CRITICAL");
    expect(worstOf({ INFO: 4, WARNING: 2 })).toBe("WARNING");
  });

  it("returns null when nothing is unread", () => {
    expect(worstOf({})).toBeNull();
    expect(worstOf({ INFO: 0 })).toBeNull();
  });
});

describe("deep links", () => {
  it("resolves the entity to a route the frontend actually has", () => {
    expect(notificationHref(row({ entity_type: "trade", entity_id: "t1" }))).toBe("/journal");
    expect(notificationHref(row({ entity_type: "bot", entity_id: "b1" }))).toBe("/bots");
    expect(
      notificationHref(row({ entity_type: "account", entity_id: "a1", category: "RISK" })),
    ).toBe("/risk");
  });

  it("returns null rather than a route that does not exist", () => {
    expect(notificationHref(row({ entity_type: "unheard_of", entity_id: "x" }))).toBeNull();
    expect(notificationHref(row({ entity_type: null, entity_id: null }))).toBeNull();
  });
});

// -------------------------------------------------------------------- bell


describe("the notification bell", () => {
  it("shows the unread count and colours it by the worst thing waiting", async () => {
    vi.mocked(notificationService.unreadCount).mockResolvedValue({
      unread: 12,
      by_severity: { INFO: 11, CRITICAL: 1 },
    });
    draw(<NotificationBell />);
    const badge = await screen.findByTestId("unread-badge");
    expect(badge).toHaveTextContent("12");
    expect(badge.className).toContain("bg-critical");
  });

  it("shows no badge at all when nothing is unread", async () => {
    vi.mocked(notificationService.unreadCount).mockResolvedValue({
      unread: 0,
      by_severity: {},
    });
    draw(<NotificationBell />);
    await screen.findByLabelText("Notifications");
    expect(screen.queryByTestId("unread-badge")).toBeNull();
  });

  it("says the list is empty rather than showing a sample alert", async () => {
    vi.mocked(notificationService.unreadCount).mockResolvedValue({ unread: 0, by_severity: {} });
    vi.mocked(notificationService.list).mockResolvedValue(page([]));
    draw(<NotificationBell />);
    fireEvent.click(await screen.findByLabelText("Notifications"));
    expect(await screen.findByText("No notifications.")).toBeInTheDocument();
  });

  it("marks one read through the API and does not decide the count itself", async () => {
    vi.mocked(notificationService.unreadCount).mockResolvedValue({
      unread: 1,
      by_severity: { INFO: 1 },
    });
    vi.mocked(notificationService.list).mockResolvedValue(page([row()]));
    vi.mocked(notificationService.markRead).mockResolvedValue({
      notification: row({ read: true }),
      unread: 0,
    });
    draw(<NotificationBell />);
    fireEvent.click(await screen.findByLabelText("Notifications: 1 unread"));
    fireEvent.click(await screen.findByText("Mark read"));
    await waitFor(() => expect(notificationService.markRead).toHaveBeenCalledWith("n1"));
  });
});

// ------------------------------------------------------------------ centre


function seedCentre(items: NotificationRow[] = [row()]) {
  vi.mocked(notificationService.list).mockResolvedValue(page(items));
  vi.mocked(notificationService.unreadCount).mockResolvedValue({
    unread: items.filter((i) => !i.read).length,
    by_severity: { INFO: 1 },
  });
  vi.mocked(notificationService.channels).mockResolvedValue({
    channels: [
      { channel: "IN_APP", state: "CONFIGURED", available: true },
      {
        channel: "EMAIL",
        state: "NOT_CONFIGURED",
        available: false,
        detail: "set SMTP_HOST to enable email delivery",
      },
      {
        channel: "DISCORD",
        state: "NOT_CONFIGURED",
        available: false,
        detail: "Discord delivery is built at level 35.",
      },
    ],
  });
  vi.mocked(notificationService.preferences).mockResolvedValue({
    preferences: [
      preference({ channel: "IN_APP", min_severity: "INFO", locked: true }),
      preference(),
      preference({ channel: "DISCORD", enabled: false }),
    ],
    locked: {
      channel: "IN_APP",
      severities: ["CRITICAL", "ERROR"],
      why: "a preference decides whether you are told.",
    },
    safety_note: "notification preferences never affect risk enforcement.",
  });
}

describe("the notification centre", () => {
  it("renders a notification with its environment and severity", async () => {
    seedCentre();
    draw(<NotificationCenter />);
    expect(await screen.findByText("[PAPER] Trade recorded")).toBeInTheDocument();
    expect(screen.getAllByText("PAPER").length).toBeGreaterThan(0);
    expect(screen.getAllByText("INFO").length).toBeGreaterThan(0);
  });

  it("never renders an environment the row did not carry", async () => {
    seedCentre([row({ environment: null, category: "AI", title: "Model activated" })]);
    draw(<NotificationCenter />);
    await screen.findByText("Model activated");
    expect(screen.queryByText("PAPER")).toBeNull();
    expect(screen.queryByText("LIVE")).toBeNull();
  });

  it("shows an unresolved environment as UNKNOWN rather than as paper", async () => {
    seedCentre([row({ environment: "unknown", title: "[UNKNOWN] Trade recorded" })]);
    draw(<NotificationCenter />);
    await screen.findByText("[UNKNOWN] Trade recorded");
    expect(screen.getAllByText("UNKNOWN").length).toBeGreaterThan(0);
  });

  it("shows an empty state rather than a fabricated row", async () => {
    seedCentre([]);
    draw(<NotificationCenter />);
    expect(
      await screen.findByText("No notifications match this filter."),
    ).toBeInTheDocument();
  });

  it("surfaces an error instead of an empty list when the API fails", async () => {
    seedCentre();
    vi.mocked(notificationService.list).mockRejectedValue(new Error("API unreachable"));
    draw(<NotificationCenter />);
    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });

  it("reports Discord as NOT_CONFIGURED without showing a webhook", async () => {
    seedCentre();
    draw(<NotificationCenter />);
    // DISCORD appears twice: once as a channel row, once in the preference
    // grid. Both are the seat L35 fills, and neither shows a URL.
    await waitFor(() => expect(screen.getAllByText("DISCORD").length).toBe(2));
    const states = screen.getAllByText("NOT_CONFIGURED");
    expect(states.length).toBe(2); // email and discord
    expect(document.body.textContent).not.toContain("http");
  });

  it("filters by severity through the API rather than in the browser", async () => {
    seedCentre();
    draw(<NotificationCenter />);
    await screen.findByText("[PAPER] Trade recorded");
    fireEvent.change(screen.getByLabelText("Severity"), { target: { value: "CRITICAL" } });
    await waitFor(() =>
      expect(notificationService.list).toHaveBeenCalledWith(
        expect.objectContaining({ severity: "CRITICAL" }),
      ),
    );
  });

  it("shows preference defaults as defaults, not as an empty page", async () => {
    seedCentre();
    draw(<NotificationCenter />);
    await screen.findByText("notification preferences never affect risk enforcement.");
    expect(screen.getAllByText("default").length).toBe(3);
  });

  it("will not let the in-app channel be switched off", async () => {
    seedCentre();
    draw(<NotificationCenter />);
    const inApp = await screen.findByLabelText("TRADING IN_APP enabled");
    expect(inApp).toBeDisabled();
  });

  it("saves a preference change through the API", async () => {
    seedCentre();
    vi.mocked(notificationService.savePreferences).mockResolvedValue({ preferences: [] });
    draw(<NotificationCenter />);
    const email = await screen.findByLabelText("TRADING EMAIL enabled");
    fireEvent.click(email);
    await waitFor(() =>
      expect(notificationService.savePreferences).toHaveBeenCalledWith([
        expect.objectContaining({ category: "TRADING", channel: "EMAIL", enabled: false }),
      ]),
    );
  });

  it("shows the reason when the backend refuses a preference", async () => {
    seedCentre();
    vi.mocked(notificationService.savePreferences).mockRejectedValue(
      new Error("in-app notifications cannot be switched off."),
    );
    draw(<NotificationCenter />);
    fireEvent.click(await screen.findByLabelText("TRADING EMAIL enabled"));
    expect(
      await screen.findByText("in-app notifications cannot be switched off."),
    ).toBeInTheDocument();
  });
});

describe("severity tones", () => {
  it.each<[NotificationSeverity, string]>([
    ["CRITICAL", "bg-critical"],
    ["ERROR", "bg-critical"],
    ["WARNING", "bg-warning"],
    ["INFO", "bg-surface-2"],
  ])("colours a %s badge distinctly", async (severity, expected) => {
    vi.mocked(notificationService.unreadCount).mockResolvedValue({
      unread: 1,
      by_severity: { [severity]: 1 },
    });
    draw(<NotificationBell />);
    const badge = await screen.findByTestId("unread-badge");
    expect(badge.className).toContain(expected);
  });
});
