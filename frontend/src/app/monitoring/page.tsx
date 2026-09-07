import { PageHeader } from "@/components/ui";
import { RecoveryPanel } from "@/components/RecoveryPanel";
import { ServiceHealth } from "@/components/ServiceHealth";
import { SystemHealth } from "@/components/SystemHealth";

export default function MonitoringPage() {
  return (
    <div>
      <PageHeader
        title="System Monitoring"
        level={37}
        description="What the backend confirms about itself. Nothing reads HEALTHY unless it was observed, nothing reads CONNECTED unless the adapter said so, and nothing reads RECOVERED unless the reconciliation actually cleared."
      />
      {/* L37 and L38 extend this page rather than adding two more: L03's
          `ServiceHealth` is kept, L37's collected view sits above it, and
          L38's recovery state sits between them because safe mode is the
          first thing an operator needs to see when something is wrong. */}
      <SystemHealth />
      <div className="mt-4">
        <RecoveryPanel />
      </div>
      <div className="mt-4">
        <ServiceHealth />
      </div>
    </div>
  );
}
