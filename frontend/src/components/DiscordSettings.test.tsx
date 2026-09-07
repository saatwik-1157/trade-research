import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DiscordSettings } from "./DiscordSettings";
import type { DiscordStatus } from "@/lib/services";
import type { User } from "@/lib/types";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    notificationService: {
      ...actual.notificationService,
      discordStatus: vi.fn(),
      sendDiscordTest: vi.fn(),
    },
  };
});

vi.mock("@/hooks/useMe", () => ({ useMe: vi.fn() }));

const { notificationService } = await import("@/lib/services");
const { useMe } = await import("@/hooks/useMe");

const WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/aBcDeFgHiJkLmNo-0123";

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DiscordSettings />
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

function status(over: Partial<DiscordStatus> = {}): DiscordStatus {
  return {
    channel: "DISCORD",
    state: "CONFIGURED",
    available: true,
    detail: "delivering through a webhook",
    transport: "webhook",
    scope: "system",
    commands: false,
    sent: 3,
    failed: 0,
    rate_limited: 0,
    how_to_enable:
      "an administrator sets DISCORD_ENABLED and DISCORD_WEBHOOK_URL on the server.",
    rotation: "a leaked webhook cannot be rotated in place.",
    ...over,
  };
}

describe("Discord settings", () => {
  it("reports the configured state without showing a webhook", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(status());
    draw();
    expect(await screen.findByText("CONFIGURED")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(WEBHOOK);
    expect(document.body.textContent).not.toContain("discord.com/api/webhooks");
  });

  it("offers no field to type a webhook into", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(status());
    draw();
    await screen.findByText("CONFIGURED");
    expect(document.querySelectorAll("input").length).toBe(0);
    expect(document.querySelectorAll("textarea").length).toBe(0);
  });

  it("says NOT_CONFIGURED rather than pretending", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(
      status({
        state: "NOT_CONFIGURED",
        available: false,
        detail: "set DISCORD_WEBHOOK_URL to enable Discord delivery",
      }),
    );
    draw();
    expect(await screen.findByText("NOT_CONFIGURED")).toBeInTheDocument();
    expect(
      screen.getByText("set DISCORD_WEBHOOK_URL to enable Discord delivery"),
    ).toBeInTheDocument();
  });

  it("distinguishes DISABLED from NOT_CONFIGURED", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(
      status({ state: "DISABLED", available: false, detail: "Discord delivery is switched off" }),
    );
    draw();
    expect(await screen.findByText("DISABLED")).toBeInTheDocument();
  });

  it("hides the test control from a non-administrator", async () => {
    signedInAs("trader");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(status());
    draw();
    await screen.findByText("CONFIGURED");
    expect(screen.queryByText("Send test notification")).toBeNull();
    expect(
      screen.getByText(
        "Configuring Discord and sending a test are administrator actions.",
      ),
    ).toBeInTheDocument();
  });

  it("sends a test through the API and reports the result", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(status());
    vi.mocked(notificationService.sendDiscordTest).mockResolvedValue({
      status: "DELIVERED",
      delivered: true,
      detail: "",
      retryable: false,
      note: "a test message only. No trade, order, alert or notification was created.",
    });
    draw();
    fireEvent.click(await screen.findByText("Send test notification"));
    await waitFor(() => expect(notificationService.sendDiscordTest).toHaveBeenCalled());
    expect(await screen.findByRole("status")).toHaveTextContent("DELIVERED");
  });

  it("will not offer a test when the channel is unavailable", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(
      status({ state: "NOT_CONFIGURED", available: false }),
    );
    draw();
    expect(await screen.findByText("Send test notification")).toBeDisabled();
  });

  it("shows the error when a test fails", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(status());
    vi.mocked(notificationService.sendDiscordTest).mockRejectedValue(
      new Error("the webhook does not exist (404)"),
    );
    draw();
    fireEvent.click(await screen.findByText("Send test notification"));
    expect(await screen.findByText("the webhook does not exist (404)")).toBeInTheDocument();
  });

  it("states that the destination is shared and off by default", async () => {
    signedInAs("user");
    vi.mocked(notificationService.discordStatus).mockResolvedValue(status());
    draw();
    await screen.findByText("CONFIGURED");
    expect(screen.getByText(/one channel for this deployment/)).toBeInTheDocument();
    expect(document.body.textContent).toContain("off by default");
  });

  it("surfaces an error rather than an empty panel", async () => {
    signedInAs("admin");
    vi.mocked(notificationService.discordStatus).mockRejectedValue(
      new Error("API unreachable"),
    );
    draw();
    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });
});
