import { PageHeader, Panel } from "@/components/ui";
import { PositionsTable } from "@/components/terminal/Tables";

export default function PositionsPage() {
  return (
    <div>
      <PageHeader
        title="Positions"
        level={10}
        description="Open positions as the broker reports them. An empty table is not a flat book until a broker is connected."
      />
      <Panel title="Open positions" level={10}>
        <PositionsTable />
      </Panel>
    </div>
  );
}
