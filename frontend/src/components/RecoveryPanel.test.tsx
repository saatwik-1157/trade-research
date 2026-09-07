import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RecoveryPanel } from "./RecoveryPanel";
import type { RecoveryStatus } from "@/lib/services";
import type { User } from "@/lib/types";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    recoveryService: {
      status: vi.fn(),
      sequence: vi.fn(),
      reconciliation: vi.fn(),
      contract: vi.fn(),
      reconcile: vi.fn(),
      enterSafeMode: vi.fn(),
      exitSafeMode: vi.fn(),
    },
  };
});

vi.mock("@/hooks/useMe", () => ({ useMe: vi.fn() }));

const { recoveryService } = await import("@/lib/services");
const { useMe } = await import("@/hooks/useMe");

beforeEach(() => vi.clearAllMocks());

function draw() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RecoveryPanel />
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

function status(over: Partial<RecoveryStatus> = {}): RecoveryStatus {
  return {
    state: "NORMAL",
    safe_mode: {
      engaged: false,
      reasons: [],
      blocks: [],
      still_allowed: ["monitoring", "reconciliation", "recovery actions"],
      authority: "safe mode ADDS a refusal. It never approves anything.",
      persistence: "process state, re-derived at every startup.",
      history: [],
    },
    startup: {
      kind: "startup",
      at: "2026-09-05T12:00:00",
      clean: true,
      steps: [
        {
          step: "configuration",
          status: "OK",
          detail: "development, trading mode paper, live trading off",
          at: "2026-09-05T12:00:00",
          blocking: false,
          facts: {},
        },
        {
          step: "broker",
          status: "SKIPPED",
          detail: "no broker adapter is registered, so there is nothing to reconcile against.",
          at: "2026-09-05T12:00:00",
          blocking: false,
          facts: {},
        },
      ],
      needs_attention: [],
      safe_mode_engaged: [],
      authority: "reconciliation REPORTS. Nothing was repaired automatically.",
    },
    last_reconciliation: null,
    environment: { trading_mode: "paper", live_trading: false, live_execution_allowed: false },
    sequence: [{ step: "configuration", what: "settings load" }],
    rules: [
      "recovery cannot bypass the RiskEngine",
      "an unknown order state is settled by asking the venue, never by retrying",
    ],
    ...over,
  };
}

function engaged(): RecoveryStatus {
  return status({
    state: "SAFE_MODE",
    safe_mode: {
      engaged: true,
      reasons: [
        {
          reason: "UNKNOWN_ORDER_STATE",
          detail: "1 order whose venue state was never established",
          at: "2026-09-05T11:00:00",
          actor_user_id: null,
        },
      ],
      blocks: ["new order submission"],
      still_allowed: ["monitoring", "reconciliation", "recovery actions"],
      authority: "safe mode ADDS a refusal. It never approves anything.",
      persistence: "process state.",
      history: [],
    },
  });
}

describe("recovery state", () => {
  it("shows NORMAL and says nothing is blocking", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    expect(await screen.findByText("NORMAL")).toBeInTheDocument();
    expect(screen.getByText(/Safe mode is not engaged/)).toBeInTheDocument();
  });

  it("shows safe mode, the condition that closed it, and what still works", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(engaged());
    draw();
    expect(await screen.findByText("SAFE_MODE")).toBeInTheDocument();
    expect(screen.getByText("UNKNOWN_ORDER_STATE")).toBeInTheDocument();
    expect(
      screen.getByText(/1 order whose venue state was never established/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Still allowed:/)).toBeInTheDocument();
  });

  it("states that safe mode never approves anything", async () => {
    signedInAs("user");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    expect(
      await screen.findByText("safe mode ADDS a refusal. It never approves anything."),
    ).toBeInTheDocument();
  });

  it("renders an error rather than claiming a clean platform", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockRejectedValue(new Error("API unreachable"));
    draw();
    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });
});

describe("the startup sequence", () => {
  it("lists every step with its status", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    expect(await screen.findByText("configuration")).toBeInTheDocument();
    expect(screen.getByText("SKIPPED")).toBeInTheDocument();
    expect(screen.getByText(/nothing to reconcile against/)).toBeInTheDocument();
  });

  it("says nothing has been recorded rather than showing a clean board", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(status({ startup: null }));
    draw();
    expect(
      await screen.findByText("No startup sequence has been recorded."),
    ).toBeInTheDocument();
  });

  it("shows that reconciliation repaired nothing", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    expect(
      await screen.findByText(/Nothing was repaired automatically/),
    ).toBeInTheDocument();
  });
});

describe("recovery actions", () => {
  it("are administrator-only", async () => {
    signedInAs("trader");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    expect(
      await screen.findByText(
        "Reconciling and changing safe mode are administrator actions.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Reconcile now")).toBeNull();
  });

  it("require a reason of real length", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    const button = await screen.findByText("Reconcile now");
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "short" } });
    expect(screen.getByText("Reconcile now")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "checking after a restart" },
    });
    expect(screen.getByText("Reconcile now")).toBeEnabled();
  });

  it("does not offer release while safe mode is off", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    await screen.findByText("Reconcile now");
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "checking after a restart" },
    });
    expect(screen.getByText("Release safe mode")).toBeDisabled();
  });

  it("sends the reason with a reconcile", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    vi.mocked(recoveryService.reconcile).mockResolvedValue(status().startup!);
    draw();
    await screen.findByText("Reconcile now");
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "checking after a restart" },
    });
    fireEvent.click(screen.getByText("Reconcile now"));
    await waitFor(() =>
      expect(recoveryService.reconcile).toHaveBeenCalledWith("checking after a restart"),
    );
  });

  it("reports that the latch is still holding when the backend refuses to clear it", async () => {
    signedInAs("admin");
    vi.mocked(recoveryService.status).mockResolvedValue(engaged());
    vi.mocked(recoveryService.exitSafeMode).mockResolvedValue({
      ...engaged(),
      report: engaged().startup!,
    });
    draw();
    await screen.findByText("Release safe mode");
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "we think it is fine now" },
    });
    fireEvent.click(screen.getByText("Release safe mode"));
    expect(
      await screen.findByText(/Safe mode is still engaged: UNKNOWN_ORDER_STATE/),
    ).toBeInTheDocument();
  });
});

describe("what recovery will not do", () => {
  it("is listed from the backend rather than written into the page", async () => {
    signedInAs("user");
    vi.mocked(recoveryService.status).mockResolvedValue(status());
    draw();
    expect(
      await screen.findByText("— recovery cannot bypass the RiskEngine"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "— an unknown order state is settled by asking the venue, never by retrying",
      ),
    ).toBeInTheDocument();
  });
});
