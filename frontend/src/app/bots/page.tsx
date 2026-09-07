import { PageHeader, Panel } from "@/components/ui";
import { BotStatus } from "@/components/terminal/BotStatus";

export default function Page() {
  return (
    <div>
      <PageHeader
        title="Bots"
        level={22}
        description="Automated sessions run in backend workers. Closing this page stops nothing, and health is measured from each run's heartbeat rather than read off its status column."
      />
      <Panel title="Bot manager" level={22}>
        <BotStatus />
      </Panel>
    </div>
  );
}
