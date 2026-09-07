"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import {
  modelRegistryService,
  type DeploymentStatus,
  type ModelDeploymentRow,
  type ModelLifecycleEventRow,
  type ModelStatus,
  type ModelVersionRow,
} from "@/lib/services";

const MODEL_KEY = "trade_probability";

/**
 * The model registry. Informational and auditable; no lifecycle control. §25.
 *
 * **Nothing on this page changes a model's status.** Registering requires
 * `manage_ai_models`; promoting, rolling back and retiring require
 * `promote_ai_models`, which only an administrator has. §25 says to display
 * only the actions allowed for the user's role, and the honest reading of that
 * on a page rendered for every role is to display none: an action a viewer
 * cannot take, greyed out, teaches them the button exists and invites the
 * request. The API is where a lifecycle change is made, with a reason attached.
 *
 * **`promoted` is not green.** It is `accent`, the same tone `PASS` gets on the
 * validation table — because "the version this scope resolves to" is not
 * "deployed to live". `TRADING_MODE` stays paper and `LIVE_TRADING` stays
 * false whatever is promoted, and a green badge would suggest otherwise.
 *
 * **`retired` and `rejected` are neutral, not critical.** Neither is a
 * deletion, and neither is a fault: one is a deliberate withdrawal and the
 * other a refused candidate. `rolled_back` is the warning one — it is the state
 * that means something went wrong.
 */
const STATUS_TONE: Record<ModelStatus, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  draft: "neutral",
  validated: "neutral",
  registered: "neutral",
  paper: "accent",
  promoted: "accent",
  rejected: "neutral",
  rolled_back: "warning",
  retired: "neutral",
};

const DEPLOYMENT_TONE: Record<DeploymentStatus, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  active: "accent",
  superseded: "neutral",
  rolled_back: "warning",
  stopped: "warning",
};

export function ModelLifecycle() {
  const query = useQuery({
    queryKey: ["registry-contract"],
    queryFn: modelRegistryService.contract,
  });
  const contract = query.data;
  if (!contract) return <p className="text-body text-muted">loading…</p>;

  return (
    <div className="flex flex-col gap-3">
      <table className="w-full text-body">
        <thead>
          <tr className="text-left text-muted">
            <th className="py-1 pr-3 font-normal">Status</th>
            <th className="py-1 pr-3 font-normal">Means</th>
            <th className="py-1 font-normal">May become</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(contract.statuses).map(([status, meaning]) => (
            <tr key={status} className="border-t border-line/50 align-top">
              <td className="py-1 pr-3">
                <Badge tone={STATUS_TONE[status as ModelStatus] ?? "neutral"}>{status}</Badge>
              </td>
              <td className="py-1 pr-3 leading-relaxed">{meaning}</td>
              <td className="py-1 font-mono text-muted">
                {(contract.transitions[status] ?? []).join(", ") || "— terminal"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <p className="text-body font-medium">The registry guarantees</p>
          <ul className="list-disc pl-4 text-body leading-relaxed text-muted">
            {contract.guarantees.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
        <div>
          <p className="text-body font-medium">The registry cannot</p>
          <ul className="list-disc pl-4 text-body leading-relaxed text-muted">
            {contract.does_not.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      </div>
      <p className="text-body leading-relaxed">
        <strong>Promotion is not deployment to live.</strong> {contract.trading_safety}
      </p>
      <p className="text-body leading-relaxed text-muted">
        <strong>Artifact integrity.</strong> {contract.artifact.integrity} {contract.artifact.security}
      </p>
    </div>
  );
}

export function ModelVersions() {
  const query = useQuery({
    queryKey: ["registry-versions", MODEL_KEY],
    queryFn: () => modelRegistryService.versions(MODEL_KEY),
  });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: ModelVersionRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<ModelVersionRow>[] = [
    {
      key: "version",
      header: "Version",
      render: (r) => (
        <span className="font-mono">{(r.artifact_ref ?? "").split(":")[1] ?? r.sequence}</span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <Badge tone={STATUS_TONE[r.status] ?? "neutral"}>{r.status}</Badge>,
    },
    {
      key: "serves",
      header: "Serves",
      render: (r) => (
        <span className={r.serves_inference ? "text-body" : "text-body text-muted"}>
          {r.serves_inference ? "yes" : "no"}
        </span>
      ),
    },
    { key: "dataset", header: "Dataset", render: (r) => <Fingerprint row={r} /> },
    {
      key: "features",
      header: "Features",
      render: (r) => <span className="font-mono text-body">{r.feature_version ?? "—"}</span>,
    },
    { key: "artifact", header: "Artifact", render: (r) => <Artifact row={r} /> },
    {
      key: "created",
      header: "Created",
      render: (r) => r.created_at?.slice(0, 19) ?? "—",
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no model versions"
        emptyHint="A version is written when a training run produces a candidate."
        caption="Model versions"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        <strong>Nothing here is ever deleted.</strong> <span className="font-mono">rejected</span>{" "}
        and <span className="font-mono">retired</span> are terminal states, not removals: a
        historical version is needed for audit, backtesting, trade review, reproducibility and
        rollback. Lifecycle changes are made through the API, with a reason recorded against the
        person who made them.
      </p>
    </div>
  );
}

/** The dataset a version was fitted on. Truncated, because it is a digest. */
function Fingerprint({ row }: { row: ModelVersionRow }) {
  if (!row.dataset_fingerprint) return <span className="text-muted">—</span>;
  return (
    <span className="font-mono text-body" title={row.dataset_fingerprint}>
      {row.dataset_version ? `v${row.dataset_version} · ` : ""}
      {row.dataset_fingerprint.slice(0, 10)}
    </span>
  );
}

/**
 * Whether this version carries the digest that makes its artifact checkable.
 *
 * "No digest" is shown as its own thing rather than as a failure: a draft has
 * never been registered, which is different from an artifact that changed.
 */
function Artifact({ row }: { row: ModelVersionRow }) {
  if (!row.artifact.sha256) {
    return <span className="text-body text-muted">not registered</span>;
  }
  return (
    <span className="font-mono text-body" title={row.artifact.sha256}>
      {row.artifact.kind} · {row.artifact.sha256.slice(0, 10)}
    </span>
  );
}

export function ModelDeployments() {
  const query = useQuery({
    queryKey: ["registry-deployments", MODEL_KEY],
    queryFn: () => modelRegistryService.deployments(MODEL_KEY),
  });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: ModelDeploymentRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<ModelDeploymentRow>[] = [
    {
      key: "status",
      header: "Status",
      render: (r) => <Badge tone={DEPLOYMENT_TONE[r.status] ?? "neutral"}>{r.status}</Badge>,
    },
    { key: "version", header: "Version", render: (r) => <span className="font-mono">{r.version}</span> },
    { key: "environment", header: "Environment", render: (r) => r.environment },
    { key: "scope", header: "Scope", render: (r) => <Scope row={r} /> },
    { key: "from", header: "From", render: (r) => r.activated_at?.slice(0, 19) ?? "—" },
    { key: "to", header: "Until", render: (r) => r.deactivated_at?.slice(0, 19) ?? "—" },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="nothing is deployed"
        emptyHint="A registered version is deployed to paper before it can be promoted."
        caption="Model deployments"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        At most one <strong>active</strong> deployment per scope, enforced by a unique index rather
        than by a check — two administrators promoting at once produce one winner and one error.
        An unset axis means <em>not restricted on it</em>, which is a recorded absence rather than
        a wildcard.
      </p>
    </div>
  );
}

function Scope({ row }: { row: ModelDeploymentRow }) {
  const parts = [
    row.strategy_key && `strategy=${row.strategy_key}`,
    row.symbol && `symbol=${row.symbol}`,
    row.timeframe && `tf=${row.timeframe}`,
  ].filter(Boolean);
  return (
    <span className="font-mono text-body">{parts.length ? parts.join(" ") : "unrestricted"}</span>
  );
}

export function ModelHistory() {
  const query = useQuery({
    queryKey: ["registry-history", MODEL_KEY],
    queryFn: () => modelRegistryService.history(MODEL_KEY),
  });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: ModelLifecycleEventRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<ModelLifecycleEventRow>[] = [
    { key: "at", header: "When", render: (r) => r.at?.slice(0, 19) ?? "—" },
    { key: "version", header: "Version", render: (r) => <span className="font-mono">{r.version}</span> },
    {
      key: "transition",
      header: "Transition",
      render: (r) => (
        <span className="font-mono text-body">
          {`${r.from ?? "—"} → ${r.to}`}
        </span>
      ),
    },
    {
      key: "actor",
      header: "By",
      render: (r) => (
        <span className="font-mono text-body">{r.actor_user_id?.slice(0, 8) ?? "system"}</span>
      ),
    },
    { key: "reason", header: "Reason", render: (r) => <span className="text-body">{r.reason}</span> },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no lifecycle events"
        emptyHint="Every status change leaves a row here and in the platform audit log."
        caption="Model lifecycle history"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        Append-only. Every transition records both halves — what it was and what it became — with
        the person who made it and the reason they gave, so <em>why is this version active</em> is
        one row rather than an inference from timestamps.
      </p>
    </div>
  );
}
