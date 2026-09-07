"use client";

import { useQuery } from "@tanstack/react-query";
import { DataTable, type Column, Unavailable } from "@/components/ui";
import { datasetService, type FeatureSpecRow } from "@/lib/services";

/**
 * The feature registry, served by the backend rather than mirrored here.
 *
 * A formula written down in two places is a formula that will eventually
 * disagree with itself, and the version on each row is what a model checks
 * before it consumes anything — so this table shows what the pipeline actually
 * computed, not a copy of it maintained by hand.
 */
export function FeatureRegistry() {
  const query = useQuery({ queryKey: ["dataset-features"], queryFn: datasetService.features });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: FeatureSpecRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<FeatureSpecRow>[] = [
    { key: "name", header: "Feature", render: (r) => <span className="font-mono">{r.name}</span> },
    {
      key: "formula",
      header: "Formula",
      render: (r) => <span className="font-mono text-micro text-ink-2">{r.formula}</span>,
    },
    { key: "unit", header: "Unit", render: (r) => r.unit },
    { key: "lookback", header: "Lookback", align: "right", render: (r) => `${r.lookback} bars` },
    {
      key: "timing",
      header: "Reads",
      render: (r) => (
        <span className="font-mono text-micro text-ink-2">
          {r.timestamp_policy === "at_or_before_T" ? "bars ≤ T" : r.timestamp_policy}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.name}
        loading={query.isPending}
        empty="no features registered"
        caption="Feature registry"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        Every feature is dimensionless. No raw price level is offered: a model trained on a moving
        average learns the price of the instrument, and pooling price-scaled quantities across
        symbols is the arithmetic error this project has already measured — a +4,236 headline in the
        metals run that was a unit mistake, not a finding.
      </p>
    </div>
  );
}
