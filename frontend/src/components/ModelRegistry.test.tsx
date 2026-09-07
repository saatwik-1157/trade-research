import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  ModelDeployments,
  ModelHistory,
  ModelLifecycle,
  ModelVersions,
} from "./ModelRegistry";
import type { ModelDeploymentRow, ModelLifecycleEventRow, ModelVersionRow } from "@/lib/services";

vi.mock("@/lib/services", async () => {
  const actual = await vi.importActual<typeof import("@/lib/services")>("@/lib/services");
  return {
    ...actual,
    modelRegistryService: {
      ...actual.modelRegistryService,
      contract: vi.fn(),
      versions: vi.fn(),
      deployments: vi.fn(),
      history: vi.fn(),
      resolve: vi.fn(),
    },
  };
});

const { modelRegistryService } = await import("@/lib/services");

function draw(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function version(over: Partial<ModelVersionRow> = {}): ModelVersionRow {
  return {
    id: "mv1",
    model: "trade_probability",
    sequence: 1,
    artifact_ref: "trade_probability:2.3",
    status: "promoted",
    features: ["rsi_14"],
    feature_version: "1.0",
    label_version: "1.0",
    dataset_version: "8",
    dataset_fingerprint: "abcdef0123456789",
    code_version: "1.0.0",
    metrics: {},
    calibrated: false,
    training_period: null,
    test_period: null,
    created_at: "2026-09-04T10:00:00",
    artifact: { sha256: "f".repeat(64), bytes: 412, kind: "logistic" },
    registered_at: "2026-09-04T10:01:00",
    promoted_at: "2026-09-04T10:05:00",
    retired_at: null,
    serves_inference: true,
    ...over,
  };
}

function deployment(over: Partial<ModelDeploymentRow> = {}): ModelDeploymentRow {
  return {
    id: "d1",
    model: "trade_probability",
    version: "2.3",
    model_version_id: "mv1",
    strategy_key: "breakout",
    symbol: "EURUSD",
    timeframe: "H1",
    environment: "paper",
    status: "active",
    activated_at: "2026-09-04T10:05:00",
    deactivated_at: null,
    previous_version_id: null,
    reason: "paper run",
    ...over,
  };
}

function event(over: Partial<ModelLifecycleEventRow> = {}): ModelLifecycleEventRow {
  return {
    id: "e1",
    model: "trade_probability",
    version: "2.3",
    from: "paper",
    to: "promoted",
    at: "2026-09-04T10:05:00",
    actor_user_id: "abcdef12-3456",
    reason: "two weeks of paper, 140 trades",
    environment: "paper",
    ...over,
  };
}

const CONTRACT = {
  statuses: {
    draft: "a trained candidate",
    validated: "validation returned PASS or CONDITIONAL",
    registered: "the registry accepted it",
    paper: "deployed to paper trading",
    promoted: "the version a scope resolves to",
    rejected: "refused. Terminal, and never a deletion.",
    rolled_back: "withdrawn because something was wrong",
    retired: "deliberately withdrawn. Terminal, never a deletion.",
  },
  transitions: {
    draft: ["rejected", "validated"],
    validated: ["registered", "rejected"],
    registered: ["paper", "rejected", "retired"],
    paper: ["promoted", "registered", "retired", "rolled_back"],
    promoted: ["registered", "retired", "rolled_back"],
    rejected: [],
    retired: [],
    rolled_back: ["registered", "retired"],
  },
  serving: ["paper", "promoted", "registered"],
  terminal: ["rejected", "retired"],
  declined: { ACTIVE: "already exists as `promoted`" },
  guarantees: ["there is exactly ONE edge into `promoted`, and it comes from `paper`"],
  does_not: ["enable live trading", "delete a model version, its artifact or its history"],
  artifact: {
    storage: "structured JSON on the model version row",
    integrity: "a sha256 digest taken at registration, re-checked before every load.",
    security: "nothing is deserialised; there is no upload route and no path field.",
    kinds: ["logistic"],
  },
  authorization: { promote: "promote_ai_models (administrator)" },
  trading_safety: "`promoted` means the version a scope resolves to. It does not enable live trading.",
};

describe("ModelLifecycle", () => {
  it("shows that the only way into promoted is from paper", async () => {
    vi.mocked(modelRegistryService.contract).mockResolvedValue(CONTRACT as never);
    draw(<ModelLifecycle />);
    await screen.findByText("promoted");
    // The `paper` row is the only one whose "may become" lists promoted.
    const rows = screen.getAllByRole("row").filter((r) => r.textContent?.includes("promoted"));
    const sources = rows.filter((r) => (r.textContent ?? "").includes("promoted, "));
    expect(sources).toHaveLength(1);
    expect(sources[0].textContent).toMatch(/^paper/);
  });

  it("says promotion is not deployment to live", async () => {
    vi.mocked(modelRegistryService.contract).mockResolvedValue(CONTRACT as never);
    draw(<ModelLifecycle />);
    expect(await screen.findByText(/Promotion is not deployment to live/)).toBeInTheDocument();
    expect(screen.getByText(/does not enable live trading/)).toBeInTheDocument();
  });

  it("shows the terminal states as having no way out", async () => {
    vi.mocked(modelRegistryService.contract).mockResolvedValue(CONTRACT as never);
    draw(<ModelLifecycle />);
    const cells = await screen.findAllByText("— terminal");
    expect(cells).toHaveLength(2);
  });
});

describe("ModelVersions", () => {
  it("shows the artifact digest and the dataset a version was fitted on", async () => {
    vi.mocked(modelRegistryService.versions).mockResolvedValue({
      available: true,
      data: [version()],
    });
    draw(<ModelVersions />);
    expect(await screen.findByText("2.3")).toBeInTheDocument();
    expect(screen.getByText(/logistic · ffffffffff/)).toBeInTheDocument();
    expect(screen.getByText(/v8 · abcdef0123/)).toBeInTheDocument();
  });

  it("distinguishes a version that was never registered from a broken one", async () => {
    vi.mocked(modelRegistryService.versions).mockResolvedValue({
      available: true,
      data: [
        version({
          status: "draft",
          serves_inference: false,
          artifact: { sha256: null, bytes: null, kind: null },
        }),
      ],
    });
    draw(<ModelVersions />);
    expect(await screen.findByText("not registered")).toBeInTheDocument();
    expect(screen.getByText("no")).toBeInTheDocument();
  });

  it("says nothing is ever deleted", async () => {
    vi.mocked(modelRegistryService.versions).mockResolvedValue({
      available: true,
      data: [version({ status: "retired" })],
    });
    draw(<ModelVersions />);
    await screen.findByText("retired");
    expect(screen.getByText(/Nothing here is ever deleted/)).toBeInTheDocument();
  });

  it("offers no control that changes a model's status", async () => {
    vi.mocked(modelRegistryService.versions).mockResolvedValue({
      available: true,
      data: [version()],
    });
    const { container } = draw(<ModelVersions />);
    await screen.findByText("2.3");
    // Interactive elements only. The caption legitimately explains that
    // rollback needs a previous version, and the STATUS badges legitimately
    // read `promoted` and `retired`; what must not exist is a control.
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(container.querySelectorAll("input, select, form, a[href]")).toHaveLength(0);
  });
});

describe("ModelDeployments", () => {
  it("shows the scope a deployment applies to", async () => {
    vi.mocked(modelRegistryService.deployments).mockResolvedValue({
      available: true,
      data: [deployment()],
    });
    draw(<ModelDeployments />);
    expect(await screen.findByText(/strategy=breakout symbol=EURUSD tf=H1/)).toBeInTheDocument();
    // "active" also appears in the caption below the table ("one active
    // deployment per scope"), so scope the query to the row.
    expect(screen.getByRole("cell", { name: "active" })).toBeInTheDocument();
  });

  it("shows an unrestricted scope as unrestricted, not as blank", async () => {
    vi.mocked(modelRegistryService.deployments).mockResolvedValue({
      available: true,
      data: [deployment({ strategy_key: null, symbol: null, timeframe: null })],
    });
    draw(<ModelDeployments />);
    expect(await screen.findByText("unrestricted")).toBeInTheDocument();
  });
});

describe("ModelHistory", () => {
  it("shows both halves of a transition, the actor and the reason", async () => {
    vi.mocked(modelRegistryService.history).mockResolvedValue({
      available: true,
      data: [event()],
    });
    draw(<ModelHistory />);
    expect(await screen.findByText("paper → promoted")).toBeInTheDocument();
    expect(screen.getByText(/two weeks of paper, 140 trades/)).toBeInTheDocument();
    expect(screen.getByText("abcdef12")).toBeInTheDocument();
  });

  it("says the history is append-only", async () => {
    vi.mocked(modelRegistryService.history).mockResolvedValue({
      available: true,
      data: [event()],
    });
    draw(<ModelHistory />);
    await screen.findByText("paper → promoted");
    expect(screen.getByText(/Append-only/)).toBeInTheDocument();
  });
});
