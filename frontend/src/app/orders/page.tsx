import { PageHeader, Panel } from "@/components/ui";
import { OrdersTable } from "@/components/terminal/Tables";

export default function OrdersPage() {
  return (
    <div>
      <PageHeader
        title="Orders"
        level={19}
        description="Every order and its state, as the venue reported it. Requested and filled are separate columns: a requested quantity is not an executed one until a fill confirms it."
      />
      <Panel title="Orders" level={19}>
        <OrdersTable />
      </Panel>
    </div>
  );
}
