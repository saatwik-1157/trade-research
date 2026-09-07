import { PageHeader } from "@/components/ui";
import { AnalyticsDashboard } from "@/components/AnalyticsDashboard";

export default function Page() {
  return (
    <>
      <PageHeader
        title="Analytics"
        level={32}
        description="Performance over completed trades, from the journal. A dash means the figure could not be computed from the sample, never that it is zero — and nothing here is a sample row."
      />
      <AnalyticsDashboard />
    </>
  );
}
