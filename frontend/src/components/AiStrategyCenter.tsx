"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import {
  aiIntegrationService,
  type AiDecisionRow,
  type AiDecisionValue,
  type AiMode,
  type AiStrategyConfigRow,
} from "@/lib/services";

/**
 * The AI Strategy Center. Informational and auditable; no unsafe control. §37.
 *
 * **There is nothing on this page that changes what a bot does.** Configuring a
 * strategy's AI is a POST that requires `manage_ai_models`, and it is not
 * offered here: a page that shows what the AI decided and a page that changes
 * what it will decide are different surfaces, and mixing them is how a
 * threshold gets moved while reading a losing streak.
 *
 * **The pipeline is rendered from the backend's own list**, not restated here,
 * so this page cannot drift from the order the code actually enforces.
 *
 * **ACCEPT is not green.** It is `accent`: an ACCEPT means the AI layer did not
 * object, and what happened next is the `final_outcome` column — where
 * `risk_vetoed` is both common and correct. Colouring ACCEPT as success would
 * teach the reader that the AI approves trades, which is the one thing the
 * whole architecture is arranged to prevent.
 */
const DECISION_TONE: Record<AiDecisionValue, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  ACCEPT: "accent",
  REJECT: "warning",
  NEUTRAL: "neutral",
  ERROR: "critical",
};

const MODE_TONE: Record<AiMode, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  AI_DISABLED: "neutral",
  AI_ADVISORY: "neutral",
  AI_FILTER: "accent",
  AI_SCORING: "accent",
};

export function AiPipeline() {
  const query = useQuery({ queryKey: ["ai-contract"], queryFn: aiIntegrationService.contract });
  const contract = query.data;
  if (!contract) return <p className="text-body text-muted">loading…</p>;

  return (
    <div className="flex flex-col gap-3">
      <ol className="flex flex-wrap items-center gap-1 text-body">
        {contract.pipeline.map((stage, i) => (
          <li key={stage} className="flex items-center gap-1">
            <span
              className={
                stage === "AI STRATEGY FILTER"
                  ? "rounded border border-accent px-1.5 py-0.5 font-mono text-accent"
                  : "rounded border border-line px-1.5 py-0.5 font-mono text-muted"
              }
            >
              {stage}
            </span>
            {i < contract.pipeline.length - 1 && <span className="text-muted">→</span>}
          </li>
        ))}
      </ol>
      <p className="text-body leading-relaxed">
        <strong>The risk engine is final.</strong> {contract.risk_authority}
      </p>
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <p className="text-body font-medium">The AI layer can</p>
          <ul className="list-disc pl-4 text-body leading-relaxed text-muted">
            {contract.guarantees.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
        <div>
          <p className="text-body font-medium">The AI layer cannot</p>
          <ul className="list-disc pl-4 text-body leading-relaxed text-muted">
            {contract.does_not.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      </div>
      <p className="text-body leading-relaxed text-muted">{contract.default}</p>
    </div>
  );
}

export function AiStrategyConfigs() {
  const query = useQuery({ queryKey: ["ai-configs"], queryFn: aiIntegrationService.configs });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: AiStrategyConfigRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<AiStrategyConfigRow>[] = [
    { key: "strategy", header: "Strategy", render: (r) => r.strategy_key },
    {
      key: "mode",
      header: "Mode",
      render: (r) => (
        <Badge tone={r.enabled ? (MODE_TONE[r.mode] ?? "neutral") : "neutral"}>
          {r.enabled ? r.mode : "AI_DISABLED"}
        </Badge>
      ),
    },
    { key: "policy", header: "On no answer", render: (r) => r.policy },
    {
      key: "models",
      header: "Models",
      render: (r) => (
        <span className="font-mono text-body">
          {r.required_models.length === 0
            ? "—"
            : r.required_models.map((m) => `${m.key} v${m.version}`).join(", ")}
        </span>
      ),
    },
    {
      key: "threshold",
      header: "Min p",
      align: "right",
      render: (r) => r.thresholds.minimum_probability.toFixed(2),
    },
    {
      key: "latency",
      header: "Budget",
      align: "right",
      render: (r) => `${r.thresholds.maximum_latency_ms}ms`,
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no strategy uses AI"
        emptyHint="A strategy with no configuration runs exactly as the deterministic strategy does."
        caption="AI strategy configuration"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        A strategy may only name a model that has been <strong>validated</strong>, at an exact
        version — there is no <span className="font-mono">latest</span>. Changing a configuration is
        a permissioned write and is deliberately not offered on this page.
      </p>
    </div>
  );
}

export function AiDecisions() {
  const query = useQuery({ queryKey: ["ai-decisions"], queryFn: aiIntegrationService.decisions });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: AiDecisionRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<AiDecisionRow>[] = [
    {
      key: "decision",
      header: "Decision",
      render: (r) => <Badge tone={DECISION_TONE[r.decision] ?? "neutral"}>{r.decision}</Badge>,
    },
    { key: "strategy", header: "Strategy", render: (r) => r.strategy_key },
    {
      key: "symbol",
      header: "Symbol",
      render: (r) => `${r.symbol ?? "—"} ${r.timeframe ?? ""}`.trim(),
    },
    { key: "mode", header: "Mode", render: (r) => <span className="text-body">{r.mode}</span> },
    {
      key: "model",
      header: "Model",
      render: (r) => <span className="font-mono text-body">{r.model ?? "—"}</span>,
    },
    {
      key: "probability",
      header: "Probability",
      align: "right",
      render: (r) => (r.probability === null ? "—" : `${(r.probability * 100).toFixed(0)}%`),
    },
    { key: "regime", header: "Regime", render: (r) => r.regime ?? "—" },
    {
      key: "anomaly",
      header: "Anomaly",
      align: "right",
      render: (r) => (r.anomaly_score === null ? "—" : r.anomaly_score.toFixed(2)),
    },
    {
      key: "latency",
      header: "Latency",
      align: "right",
      render: (r) =>
        r.latency_ms.total === null ? "—" : `${r.latency_ms.total.toFixed(0)}ms`,
    },
    { key: "outcome", header: "Then", render: (r) => <Outcome row={r} /> },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no AI decisions recorded"
        emptyHint="Every decision is journalled, including the ones that changed nothing."
        caption="AI decision journal"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        An <strong>ACCEPT</strong> means the AI layer did not object. What happened next is in the{" "}
        <em>Then</em> column: the risk engine, position sizing and the OMS all ran afterwards and any
        of them could refuse. A <strong>NEUTRAL</strong> is an advisory reading and is never counted
        as agreement; an <strong>ERROR</strong> carries no probability at all.
      </p>
    </div>
  );
}

/** What happened after the AI layer. `risk_vetoed` here is correct, not a bug. */
function Outcome({ row }: { row: AiDecisionRow }) {
  if (!row.final_outcome) {
    return <span className="text-body text-muted">—</span>;
  }
  const vetoed = row.final_outcome.startsWith("risk_");
  // One text node, not two: a split label is invisible to a reader searching
  // for "risk_vetoed · max_daily_loss" and to a test looking for the same.
  const label = row.risk_verdict
    ? `${row.final_outcome} · ${row.risk_verdict}`
    : row.final_outcome;
  return (
    <span className={vetoed ? "text-body text-warning" : "text-body"}>{label}</span>
  );
}
