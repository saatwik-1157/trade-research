"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import { trainingService, type TrainingJobRow, type TrainingStatus } from "@/lib/services";

/**
 * Training jobs, and what a finished one actually means.
 *
 * **`validation_pending` is success.** It is deliberately not called
 * "completed": a finished run means a CANDIDATE was trained, and reading that
 * as "the model is approved" is the single most expensive misunderstanding
 * this page could invite. The tone is `accent`, not `good`, for the same
 * reason — it is a handoff, not a result.
 *
 * Nothing here promotes a model. There is no such control, and no such route.
 */
const TONE: Record<TrainingStatus, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  queued: "neutral",
  running: "accent",
  cancelling: "warning",
  cancelled: "neutral",
  failed: "critical",
  finished: "neutral",
  // A candidate exists and is waiting for L26. Not "good": nothing is approved.
  validation_pending: "accent",
};

export function TrainingJobs() {
  const query = useQuery({ queryKey: ["training-jobs"], queryFn: trainingService.list });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: TrainingJobRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<TrainingJobRow>[] = [
    {
      key: "model",
      header: "Model",
      render: (r) => `${r.model ?? "—"} v${r.model_version ?? "?"}`,
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <Badge tone={TONE[r.status] ?? "neutral"}>{r.status}</Badge>,
    },
    { key: "stage", header: "Stage", render: (r) => <Stage row={r} /> },
    { key: "dataset", header: "Dataset", render: (r) => <Dataset row={r} /> },
    {
      key: "seed",
      header: "Seed",
      align: "right",
      render: (r) => (r.random_seed === null ? "—" : r.random_seed),
    },
    { key: "candidate", header: "Candidate", render: (r) => <Candidate row={r} /> },
    { key: "started", header: "Started", render: (r) => r.started_at?.slice(0, 19) ?? "—" },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no training jobs"
        emptyHint="Training runs in the background. Closing this page stops nothing."
        caption="Training jobs"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        A successful run ends at <span className="font-mono">validation_pending</span>, which means
        a candidate model was trained. It does <strong>not</strong> mean the model is approved for
        trading: L26 validates it and L28 promotes it, and nothing on this page can do either.
      </p>
    </div>
  );
}

/** The stage, with the progress derived from it rather than reported beside it. */
function Stage({ row }: { row: TrainingJobRow }) {
  if (!row.stage) return <span className="text-muted">—</span>;
  const percent = row.progress === null ? null : Math.round(row.progress * 100);
  return (
    <span className="font-mono text-micro text-ink-2">
      {row.stage}
      {percent === null ? "" : ` · ${percent}%`}
    </span>
  );
}

/**
 * Which data the job LOCKED, by fingerprint rather than by name.
 *
 * A version string records what the data was called; the fingerprint covers
 * the bars themselves, which is what makes "the dataset changed under this
 * job" detectable rather than merely forbidden.
 */
function Dataset({ row }: { row: TrainingJobRow }) {
  if (!row.dataset) return <span className="text-muted">—</span>;
  return (
    <span className="font-mono text-micro text-ink-2" title={row.dataset_fingerprint ?? undefined}>
      {row.dataset}
      {row.dataset_fingerprint ? ` · ${row.dataset_fingerprint.slice(0, 8)}` : ""}
    </span>
  );
}

function Candidate({ row }: { row: TrainingJobRow }) {
  if (row.status === "failed") {
    return (
      <span className="text-critical" title={row.error ?? undefined}>
        none — failed
      </span>
    );
  }
  if (!row.model_version_id) {
    return <span className="text-muted">not yet</span>;
  }
  return (
    <span className="font-mono text-micro text-ink-2" title={row.model_version_id}>
      draft · {row.model_version_id.slice(0, 8)}
    </span>
  );
}
