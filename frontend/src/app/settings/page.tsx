import { ConfigView } from "@/components/ConfigView";
import { PageHeader } from "@/components/PageHeader";
import { Panel } from "@/components/Panel";
import { Unavailable } from "@/components/Unavailable";

export default function SettingsPage() {
  return (
    <div>
      <PageHeader
        title="Settings"
        level={3}
        description="Read-only view of the running configuration. Changing it requires authentication (L04)."
      />
      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="Running configuration" level={2}>
          <ConfigView />
        </Panel>
        <Panel title="Edit settings" level={4}>
          <Unavailable
            level={4}
            reason="Mode changes, risk limits and broker credentials are protected actions. They arrive with authentication and roles."
            today=".env (TRADING_MODE, LIVE_TRADING) — restart to apply"
          />
        </Panel>
      </div>
    </div>
  );
}
