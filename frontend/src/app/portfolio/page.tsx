import { PageHeader } from "@/components/ui";
import { PortfolioDashboard } from "@/components/PortfolioDashboard";

export default function Page() {
  return (
    <>
      <PageHeader
        title="Portfolio"
        level={30}
        description="Balance, equity, exposure and drawdown, for one account at a time. Paper and live are never pooled, and an unavailable figure is a dash rather than a zero."
      />
      <PortfolioDashboard />
    </>
  );
}
