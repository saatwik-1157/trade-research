"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LoadingState,
  Panel,
  Select,
  StatTile,
} from "@/components/ui";
import { adminService } from "@/lib/services";
import type { AdminUserRow, AuditRow } from "@/lib/services";

/**
 * The administration panel. Sections 7, 39, 40 and 41.
 *
 * **It reports; the surfaces that own things act.** There is no button here
 * that pauses a bot, promotes a model or reconciles a broker, and there is no
 * backend route behind one either. Those controls live on `/bots`, `/ai-lab`
 * and `/brokers`, already gated by the same permissions, and this page links to
 * them. A second door would be a second authorization surface to keep in step
 * with the first.
 *
 * **The environment is the first thing on the page and it is unmissable.**
 * Section 8: an administrator must never be able to believe paper is live. It
 * is rendered from the backend's own derived answer, including the list of
 * gates that are not built, so "live is off" arrives with its reasons attached.
 *
 * **A dangerous action costs something.** Section 30 and section 40: a reason
 * of real length and the subject's email typed back. Both are enforced by the
 * backend; the dialog exists so the requirement is visible rather than
 * surprising.
 *
 * **Empty means empty.** Section 41 and 69: no seeded users, no sample bots, no
 * example audit rows. A platform with nothing in it shows zeros.
 */
type Tab = "overview" | "users" | "audit" | "integrations" | "access";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "users", label: "Users" },
  { id: "audit", label: "Audit trail" },
  { id: "integrations", label: "Integrations" },
  { id: "access", label: "Roles & config" },
];

function EnvironmentBanner() {
  const q = useQuery({ queryKey: ["admin", "dashboard"], queryFn: adminService.dashboard });
  if (q.isPending) return <LoadingState what="platform state" />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const env = q.data.environment;
  const live = env.live_trading || env.trading_mode === "live";
  return (
    <div
      role="status"
      aria-label="trading environment"
      className={`rounded border p-3 ${
        live ? "border-critical bg-critical/10" : "border-line bg-surface-2/40"
      }`}
    >
      <div className="flex flex-wrap items-center gap-3 text-body">
        <span className="font-semibold uppercase tracking-wide text-muted">Trading mode</span>
        <Badge tone={live ? "critical" : "accent"}>{env.trading_mode.toUpperCase()}</Badge>
        <span className="font-semibold uppercase tracking-wide text-muted">Live trading</span>
        <Badge tone={env.live_trading ? "critical" : "good"}>
          {env.live_trading ? "ENABLED" : "DISABLED"}
        </Badge>
        <span className="font-semibold uppercase tracking-wide text-muted">Live execution</span>
        <Badge tone={env.live_execution_allowed ? "critical" : "good"}>
          {env.live_execution_allowed ? "ALLOWED" : "BLOCKED"}
        </Badge>
      </div>
      {env.live_execution_blockers.length > 0 && (
        <details className="mt-2 text-body text-muted">
          <summary className="cursor-pointer">
            {env.live_execution_blockers.length} gate(s) not built
          </summary>
          <ul className="mt-1 space-y-0.5 font-mono">
            {env.live_execution_blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </details>
      )}
      <p className="mt-2 text-body text-muted">{env.note}</p>
    </div>
  );
}

function Overview() {
  const q = useQuery({ queryKey: ["admin", "dashboard"], queryFn: adminService.dashboard });
  const contract = useQuery({ queryKey: ["admin", "contract"], queryFn: adminService.contract });
  if (q.isPending) return <LoadingState what="dashboard" />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const d = q.data;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <StatTile label="Users" value={d.users.total} note={`${d.users.active} active`} />
        <StatTile label="Bots" value={d.bots.total} note={`${d.bots.enabled} enabled`} />
        <StatTile
          label="Strategies"
          value={d.strategies.total}
          note={`${d.strategies.active} active`}
        />
        <StatTile
          label="Model versions"
          value={d.models.versions}
          note={`${d.models.deployments_active} deployed`}
        />
        <StatTile label="Trades" value={d.trading.trades} />
        <StatTile label="Open positions" value={d.trading.positions_open} />
        <StatTile label="Working orders" value={d.trading.orders_working} />
        <StatTile
          label="Need reconciliation"
          value={d.trading.orders_needing_reconciliation}
          tone={d.trading.orders_needing_reconciliation > 0 ? "warning" : "default"}
          note="broker state not established"
        />
        <StatTile label="Paper accounts" value={d.accounts.paper} />
        <StatTile label="Broker accounts" value={d.accounts.broker} />
        <StatTile label="Notifications" value={d.notifications.total} />
        <StatTile
          label="Failed deliveries"
          value={d.notifications.deliveries_failed}
          tone={d.notifications.deliveries_failed > 0 ? "warning" : "default"}
        />
      </div>

      <Panel title="Where each control lives" level={36}>
        {contract.isPending ? (
          <LoadingState what="contract" />
        ) : contract.isError ? (
          <ErrorState message={(contract.error as Error).message} />
        ) : (
          <>
            <p className="mb-2 text-body text-muted">{contract.data.delegation_note}</p>
            <ul className="space-y-1 text-body">
              {Object.entries(contract.data.delegates_to).map(([key, where]) => (
                <li key={key} className="flex gap-2">
                  <Link
                    href={`/${key === "models" ? "ai-lab" : key}`}
                    className="w-24 shrink-0 text-accent hover:underline"
                  >
                    {key}
                  </Link>
                  <span className="text-muted">{where}</span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-mini font-semibold uppercase tracking-wide text-muted">
              This panel cannot
            </p>
            <ul className="mt-1 space-y-0.5 text-body text-muted">
              {contract.data.cannot.map((c) => (
                <li key={c}>— {c}</li>
              ))}
            </ul>
          </>
        )}
      </Panel>
      <p className="text-body text-muted">{d.authority}</p>
    </div>
  );
}

function DangerousDialog({
  title,
  expect,
  onConfirm,
  onCancel,
  pending,
  error,
}: {
  title: string;
  expect: string;
  onConfirm: (reason: string, confirm: string) => void;
  onCancel: () => void;
  pending: boolean;
  error: string | null;
}) {
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  return (
    <div
      role="dialog"
      aria-label={title}
      className="mt-2 space-y-2 rounded border border-critical bg-critical/5 p-3"
    >
      <p className="text-body font-semibold text-critical">{title}</p>
      <Field label="Reason (recorded in the audit trail)">
        <Input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Why is this happening?"
        />
      </Field>
      <Field label={`Type ${expect} to confirm`}>
        <Input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={expect} />
      </Field>
      {error ? <ErrorState message={error} /> : null}
      <div className="flex gap-2">
        <Button
          variant="danger"
          disabled={pending || reason.trim().length < 8 || confirm !== expect}
          onClick={() => onConfirm(reason, confirm)}
        >
          {pending ? "Working…" : "Confirm"}
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function Users() {
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<AdminUserRow | null>(null);
  const [action, setAction] = useState<"deactivate" | "activate" | "revoke" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["admin", "users", search],
    queryFn: () => adminService.users({ search: search || undefined, limit: 25 }),
    retry: false,
  });

  const act = useMutation({
    mutationFn: ({ reason, confirm }: { reason: string; confirm: string }) => {
      if (!selected || !action) throw new Error("nothing selected");
      if (action === "deactivate") return adminService.deactivateUser(selected.id, reason, confirm);
      if (action === "activate") return adminService.activateUser(selected.id, reason, confirm);
      return adminService.revokeSessions(selected.id, reason, confirm);
    },
    onSuccess: () => {
      setError(null);
      setAction(null);
      client.invalidateQueries({ queryKey: ["admin"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  return (
    <Panel title="Users" level={36}>
      <div className="mb-2 flex items-end gap-2">
        <Field label="Search">
          <Input
            aria-label="Search users"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="email or id"
          />
        </Field>
      </div>
      {list.isPending ? (
        <LoadingState what="users" />
      ) : list.isError ? (
        <ErrorState message={(list.error as Error).message} />
      ) : list.data.items.length === 0 ? (
        <EmptyState message="No users found." />
      ) : (
        <table className="w-full text-body">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="py-1 pr-2">Email</th>
              <th className="py-1 pr-2">Role</th>
              <th className="py-1 pr-2">Status</th>
              <th className="py-1 pr-2">Last login</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {list.data.items.map((u) => (
              <tr key={u.id} className="border-b border-line/50">
                <td className="py-1 pr-2">{u.email}</td>
                <td className="py-1 pr-2">
                  <Badge tone={u.role === "admin" ? "warning" : "neutral"}>{u.role}</Badge>
                </td>
                <td className="py-1 pr-2">
                  <Badge tone={u.is_active ? "good" : "neutral"}>
                    {u.is_active ? "active" : "inactive"}
                  </Badge>
                </td>
                <td className="py-1 pr-2 font-mono text-muted">{u.last_login_at ?? "never"}</td>
                <td className="py-1 text-right">
                  <button
                    type="button"
                    className="text-accent hover:underline"
                    onClick={() => {
                      setSelected(u);
                      setAction(u.is_active ? "deactivate" : "activate");
                      setError(null);
                    }}
                  >
                    {u.is_active ? "Deactivate" : "Activate"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selected && action ? (
        <DangerousDialog
          title={
            action === "deactivate"
              ? `Deactivate ${selected.email}. Their trades, journal and analytics are kept; nothing is closed or stopped.`
              : action === "activate"
                ? `Restore access for ${selected.email}.`
                : `Revoke every session for ${selected.email}.`
          }
          expect={selected.email}
          pending={act.isPending}
          error={error}
          onCancel={() => {
            setAction(null);
            setError(null);
          }}
          onConfirm={(reason, confirm) => act.mutate({ reason, confirm })}
        />
      ) : null}
    </Panel>
  );
}

function AuditTrail() {
  const [action, setAction] = useState("");
  const list = useQuery({
    queryKey: ["admin", "audit", action],
    queryFn: () => adminService.auditLogs({ action: action || undefined, limit: 25 }),
    retry: false,
  });
  const actions = useQuery({
    queryKey: ["admin", "audit-actions"],
    queryFn: adminService.auditActions,
    retry: false,
  });

  const reasonOf = (row: AuditRow) =>
    typeof row.details?.reason === "string" ? (row.details.reason as string) : null;

  return (
    <Panel title="Audit trail" level={36}>
      <div className="mb-2 flex items-end gap-2">
        <Field label="Action">
          <Select
            aria-label="Action"
            value={action}
            onChange={(e) => setAction(e.target.value)}
          >
            <option value="">All</option>
            {(actions.data?.actions ?? []).map((a) => (
              <option key={a.action} value={a.action}>
                {a.action} ({a.count})
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {list.isPending ? (
        <LoadingState what="audit trail" />
      ) : list.isError ? (
        <ErrorState message={(list.error as Error).message} />
      ) : list.data.items.length === 0 ? (
        <EmptyState message="No audit records match this filter." />
      ) : (
        <ul className="space-y-1 text-body">
          {list.data.items.map((row) => (
            <li key={row.id} className="border-b border-line/50 py-1">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-mono text-muted">{row.occurred_at}</span>
                <Badge tone="neutral">{row.action}</Badge>
                <span className="text-muted">
                  {row.resource_type}
                  {row.resource_id ? ` ${row.resource_id.slice(0, 8)}` : ""}
                </span>
              </div>
              {reasonOf(row) ? <p className="text-muted">{reasonOf(row)}</p> : null}
            </li>
          ))}
        </ul>
      )}
      {actions.data ? (
        <p className="mt-2 text-body text-muted">{actions.data.immutability}</p>
      ) : null}
    </Panel>
  );
}

function Integrations() {
  const q = useQuery({
    queryKey: ["admin", "integrations"],
    queryFn: adminService.integrations,
    retry: false,
  });
  if (q.isPending) return <LoadingState what="integrations" />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const d = q.data;
  return (
    <div className="space-y-4">
      <Panel title="Integrations" level={36}>
        <ul className="space-y-1 text-body">
          <li className="flex items-center gap-2">
            <span className="w-32 text-muted">Email</span>
            <Badge tone={d.email.configured ? "good" : "neutral"}>
              {d.email.configured ? "CONFIGURED" : "NOT_CONFIGURED"}
            </Badge>
          </li>
          <li className="flex items-center gap-2">
            <span className="w-32 text-muted">Discord</span>
            <Badge tone={d.discord.state === "CONFIGURED" ? "good" : "neutral"}>
              {d.discord.state}
            </Badge>
          </li>
          <li className="flex items-center gap-2">
            <span className="w-32 text-muted">TradingView</span>
            <Badge tone={d.tradingview_webhook.secret_configured ? "good" : "warning"}>
              {d.tradingview_webhook.secret_configured ? "SECRET SET" : "NO SECRET — REFUSING"}
            </Badge>
          </li>
          <li className="flex items-center gap-2">
            <span className="w-32 text-muted">Event bus</span>
            <Badge tone="neutral">{d.event_bus.kind}</Badge>
          </li>
          <li className="flex items-center gap-2">
            <span className="w-32 text-muted">Brokers</span>
            <Badge tone={d.brokers.adapters_registered > 0 ? "good" : "neutral"}>
              {d.brokers.adapters_registered} adapter(s)
            </Badge>
          </li>
        </ul>
        <p className="mt-2 text-body text-muted">{d.brokers.note}</p>
      </Panel>
      <Panel title="Never returned by this API" level={36}>
        <ul className="space-y-0.5 text-body text-muted">
          {d.never_returned.map((n) => (
            <li key={n}>— {n}</li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}

function Access() {
  const perms = useQuery({
    queryKey: ["admin", "permissions"],
    queryFn: adminService.permissions,
    retry: false,
  });
  const config = useQuery({
    queryKey: ["admin", "configuration"],
    queryFn: adminService.configuration,
    retry: false,
  });
  return (
    <div className="space-y-4">
      <Panel title="Roles and permissions" level={36}>
        {perms.isPending ? (
          <LoadingState what="permissions" />
        ) : perms.isError ? (
          <ErrorState message={(perms.error as Error).message} />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              {perms.data.roles.map((r) => (
                <div key={r.role} className="rounded border border-line p-2">
                  <p className="mb-1 text-mini font-semibold uppercase tracking-wide">
                    {r.role} · {r.count}
                  </p>
                  <ul className="space-y-0.5 font-mono text-micro text-muted">
                    {r.permissions.map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
            <p className="mt-2 text-body text-muted">{perms.data.note}</p>
            <p className="mt-1 text-body text-warning">{perms.data.mfa}</p>
          </>
        )}
      </Panel>

      <Panel title="Configuration" level={36}>
        {config.isPending ? (
          <LoadingState what="configuration" />
        ) : config.isError ? (
          <ErrorState message={(config.error as Error).message} />
        ) : (
          <>
            <p className="text-body text-muted">{config.data.runtime_editable_note}</p>
            <p className="mt-2 text-mini font-semibold uppercase tracking-wide text-muted">
              Live gates
            </p>
            <ul className="mt-1 grid gap-x-4 font-mono text-micro sm:grid-cols-2">
              {Object.entries(config.data.live_gates.gates).map(([name, built]) => (
                <li key={name} className="flex items-center gap-2">
                  <Badge tone={built ? "good" : "neutral"}>{built ? "BUILT" : "NOT BUILT"}</Badge>
                  <span>{name}</span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-body text-muted">{config.data.live_gates.note}</p>
            <p className="mt-2 text-body text-muted">
              {config.data.feature_flags.why_not}
            </p>
          </>
        )}
      </Panel>
    </div>
  );
}

export function AdminPanel() {
  const [tab, setTab] = useState<Tab>("overview");
  return (
    <div className="space-y-4">
      <EnvironmentBanner />
      <nav aria-label="Admin sections" className="flex flex-wrap gap-1 border-b border-line">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-current={tab === t.id ? "page" : undefined}
            className={`px-3 py-1.5 text-body ${
              tab === t.id
                ? "border-b-2 border-accent font-semibold text-ink"
                : "text-muted hover:text-ink"
            }`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {tab === "overview" && <Overview />}
      {tab === "users" && <Users />}
      {tab === "audit" && <AuditTrail />}
      {tab === "integrations" && <Integrations />}
      {tab === "access" && <Access />}
    </div>
  );
}
