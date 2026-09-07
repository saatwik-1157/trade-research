"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import {
  validationService,
  type ValidationRunRow,
  type ValidationSeverity,
  type ValidationVerdict,
} from "@/lib/services";

/**
 * Validation runs, and what a verdict does and does not authorise.
 *
 * **There is no score on this page, and there is nowhere to add one.** The
 * backend does not produce one, deliberately: a weighted composite can always
 * be tuned until it hides the check that mattered. So the row shows the verdict
 * and the counts, and the detail shows every check by name.
 *
 * **PASS is not green.** It is `accent`, the same tone `validation_pending`
 * uses on the training table, because a passing report means a candidate may be
 * CONSIDERED by the registry — not that anything is approved or deployed.
 * `good` here would be the single most expensive colour choice in the app.
 *
 * **BLOCKED is not critical.** It is a warning tone: BLOCKED means a check
 * could not be evaluated, which is a statement about the evidence and not about
 * the model. Rendering it in the same red as FAIL would teach the exact
 * confusion §26 exists to prevent.
 *
 * Nothing here promotes, activates or deploys a model. There is no such
 * control, and no such route.
 */
const VERDICT_TONE: Record<ValidationVerdict, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  PASS: "accent",
  CONDITIONAL: "warning",
  FAIL: "critical",
  BLOCKED: "warning",
};

const SEVERITY_TONE: Record<ValidationSeverity, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  PASS: "accent",
  WARNING: "warning",
  FAIL: "critical",
  BLOCKED: "warning",
};

export function ValidationRuns() {
  const [open, setOpen] = useState<string | null>(null);
  const query = useQuery({ queryKey: ["validation-runs"], queryFn: validationService.list });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: ValidationRunRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<ValidationRunRow>[] = [
    {
      key: "verdict",
      header: "Verdict",
      render: (r) =>
        r.verdict ? (
          <Badge tone={VERDICT_TONE[r.verdict] ?? "neutral"}>{r.verdict}</Badge>
        ) : (
          <Badge tone="neutral">{r.status}</Badge>
        ),
    },
    { key: "summary", header: "Summary", render: (r) => <Summary row={r} /> },
    { key: "checks", header: "Checks", render: (r) => <Counts row={r} /> },
    {
      key: "candidate",
      header: "Candidate",
      render: (r) => (
        <span className="font-mono text-body">{r.model_version_id.slice(0, 12)}</span>
      ),
    },
    {
      key: "data",
      header: "Data",
      render: (r) => (
        <span className="font-mono text-body">
          {r.dataset_fingerprint ? r.dataset_fingerprint.slice(0, 12) : "—"}
        </span>
      ),
    },
    { key: "finished", header: "Finished", render: (r) => r.finished_at?.slice(0, 19) ?? "—" },
    {
      key: "report",
      header: "",
      render: (r) => (
        <button
          type="button"
          className="text-body underline decoration-dotted underline-offset-2 text-muted hover:text-fg disabled:opacity-40"
          disabled={r.status !== "completed"}
          onClick={() => setOpen(open === r.id ? null : r.id)}
        >
          {open === r.id ? "hide" : "report"}
        </button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no validation runs"
        emptyHint="A candidate is validated on request. Validation runs in the background."
        caption="Validation runs"
      />
      {open && <Report runId={open} />}
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        A <strong>PASS</strong> means the candidate satisfies the validation requirements that were
        checked, under the thresholds the report records. It does <strong>not</strong> mean deploy,
        allocate or trade — it means the candidate may be <em>considered</em> by the model registry.{" "}
        <strong>BLOCKED is not FAIL</strong>: it means a check could not be evaluated, so no
        conclusion about the model may be drawn from it.
      </p>
    </div>
  );
}

/** The one-line summary, which is the verdict in words rather than a number. */
function Summary({ row }: { row: ValidationRunRow }) {
  if (row.error) {
    return <span className="text-body text-critical">{row.error.slice(0, 90)}</span>;
  }
  if (!row.summary) {
    return (
      <span className="text-body text-muted">
        {row.stage ?? "queued"}
        {row.progress === null ? "" : ` · ${Math.round(row.progress * 100)}%`}
      </span>
    );
  }
  return <span className="text-body">{row.summary}</span>;
}

/** Counts by severity. BLOCKED is shown even at zero, so its absence is visible. */
function Counts({ row }: { row: ValidationRunRow }) {
  const { PASS, WARNING, FAIL, BLOCKED } = row.checks;
  if (PASS === null) return <span className="text-muted">—</span>;
  return (
    <span className="font-mono text-body">
      <span className="text-accent">{PASS}P</span>{" "}
      <span className={WARNING ? "text-warning" : "text-muted"}>{WARNING ?? 0}W</span>{" "}
      <span className={FAIL ? "text-critical" : "text-muted"}>{FAIL ?? 0}F</span>{" "}
      <span className={BLOCKED ? "text-warning" : "text-muted"}>{BLOCKED ?? 0}B</span>
    </span>
  );
}

/**
 * The full report: every check, its severity and what it measured.
 *
 * Rendered as the table §27 asks for. The evidence is not summarised into a
 * headline here — the reason a check failed is the useful part, and a reader
 * who disagrees with a threshold can see the number it was compared against.
 */
function Report({ runId }: { runId: string }) {
  const query = useQuery({
    queryKey: ["validation-report", runId],
    queryFn: () => validationService.report(runId),
  });
  const report = query.data?.report;

  if (query.isPending) return <p className="text-body text-muted">loading report…</p>;
  if (!report) {
    return <p className="text-body text-muted">{query.data?.note ?? "no report"}</p>;
  }

  return (
    <div className="flex flex-col gap-2 rounded border border-line bg-surface p-3">
      <div className="flex items-baseline gap-2">
        <Badge tone={VERDICT_TONE[report.verdict] ?? "neutral"}>{report.verdict}</Badge>
        <span className="text-body text-muted">{report.summary}</span>
      </div>
      <p className="text-body leading-relaxed text-muted">{report.means}</p>
      <table className="w-full text-body">
        <thead>
          <tr className="text-left text-muted">
            <th className="py-1 pr-3 font-normal">Check</th>
            <th className="py-1 pr-3 font-normal">Result</th>
            <th className="py-1 font-normal">Detail</th>
          </tr>
        </thead>
        <tbody>
          {report.checks.map((check) => (
            <tr key={check.check} className="border-t border-line/50 align-top">
              <td className="py-1 pr-3 font-mono">{check.check}</td>
              <td className="py-1 pr-3">
                <Badge tone={SEVERITY_TONE[check.severity] ?? "neutral"}>{check.severity}</Badge>
              </td>
              <td className="py-1 leading-relaxed">{check.summary}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.recommendations.length > 0 && (
        <div>
          <p className="text-body font-medium">What would have to change</p>
          <ul className="list-disc pl-4 text-body leading-relaxed text-muted">
            {report.recommendations.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-body leading-relaxed text-muted">{report.method.scoring}</p>
    </div>
  );
}
