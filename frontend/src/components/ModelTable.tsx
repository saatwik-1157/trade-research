"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import { modelService, type ModelRow } from "@/lib/services";

/**
 * The models this deployment can answer with.
 *
 * **An empty table is the correct state, not a broken one.** No model is loaded
 * by default: a deployment with none answers MODEL_UNAVAILABLE, and under
 * AI_REQUIRED that means no trade. The empty hint says so rather than implying
 * something failed to load.
 *
 * **No figure here is trading performance.** A model's metrics describe how it
 * scored on the data it was fitted and validated against, which is a different
 * question from what it would have earned — and the project's own record is
 * that nothing it has measured clears its null out of sample. The footer says
 * that plainly, because a table of models beside a table of trades invites
 * exactly the reading it rules out.
 */
export function ModelTable() {
  const query = useQuery({ queryKey: ["ai-models"], queryFn: modelService.list });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: ModelRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<ModelRow>[] = [
    { key: "model", header: "Model", render: (r) => `${r.model} v${r.version}` },
    { key: "kind", header: "Kind", render: (r) => r.kind },
    {
      key: "fitted",
      header: "Fitted",
      render: (r) =>
        r.fitted ? (
          <Badge tone="good">fitted</Badge>
        ) : (
          <Badge tone="critical" title="answers MODEL_UNAVAILABLE, never a default">
            no parameters
          </Badge>
        ),
    },
    {
      key: "features",
      header: "Reads",
      render: (r) => (
        <span className="font-mono text-micro text-ink-2">
          {r.contract.features.length} features · f{r.contract.feature_version}
        </span>
      ),
    },
    { key: "provenance", header: "Fitted on", render: (r) => <Provenance row={r} /> },
    { key: "calibration", header: "Calibrated", render: (r) => <Calibration row={r} /> },
    {
      key: "lookback",
      header: "Lookback",
      align: "right",
      render: (r) => `${r.contract.lookback} bars`,
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => `${r.model}:${r.version}`}
        loading={query.isPending}
        empty="no model is loaded"
        emptyHint="Expected: nothing loads a model by default. With none registered the AI layer answers MODEL_UNAVAILABLE, which under AI_REQUIRED means no trade."
        caption="Models"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        Advisory only. No model here can place, size or approve an order, raise a risk limit,
        modify a position or enable live trading — the verdict type has no field that could.
        Nothing on this page is trading performance: a model&rsquo;s metrics describe the data it
        was fitted on, and this project&rsquo;s own searches have yet to find a rule that clears
        its permutation null out of sample.
      </p>
    </div>
  );
}

/** What the model was fitted against, or that it has not been. */
function Provenance({ row }: { row: ModelRow }) {
  if (!row.fitted) {
    return <span className="text-muted">—</span>;
  }
  const dataset = row.dataset_version ? `d${row.dataset_version}` : "unversioned data";
  return (
    <span className="font-mono text-micro text-ink-2" title={row.dataset_fingerprint ?? undefined}>
      {dataset}
      {row.label_version ? ` · l${row.label_version}` : ""}
    </span>
  );
}

/**
 * Measured, or not claimed. Never assumed.
 *
 * An uncalibrated probability is a ranking rather than a frequency, and 0.80
 * read as an 80% success rate is the specific mistake this column prevents.
 */
function Calibration({ row }: { row: ModelRow }) {
  if (row.calibrated === undefined) {
    return <span className="text-muted">n/a</span>;
  }
  if (!row.calibrated) {
    return (
      <span className="text-warning" title="an uncalibrated probability is a ranking, not a frequency">
        not measured
      </span>
    );
  }
  return <span className="text-ink-2">measured</span>;
}
