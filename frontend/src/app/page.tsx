import { PageHeader, Panel, Unavailable } from "@/components/ui";
import { DashboardAccount } from "@/components/DashboardAccount";
import { RecentTrades } from "@/components/RecentTrades";
import { ServiceHealth } from "@/components/ServiceHealth";
import { SystemStatus } from "@/components/SystemStatus";
import { BotStatus } from "@/components/terminal/BotStatus";
import { OrdersTable, PositionsTable } from "@/components/terminal/Tables";

/**
 * The front page.
 *
 * It used to be eight fixed "not built" notices. Four of them had stopped
 * being true -- positions, orders, bots and the trade journal are all served
 * and all already have working components on their own pages -- so the front
 * door was reporting a platform several levels behind the one running behind
 * it. Understating what exists is not the safe direction of that error: it is
 * the same screen-disagrees-with-system failure as overstating it, and it is
 * the one nobody files a bug about.
 *
 * The two notices that remain are still accurate, and each renders through the
 * component that owns the question rather than through a sentence written
 * here, so the next one to come true corrects itself.
 */
export default function DashboardPage() {
  return (
    <div>
      <PageHeader
        title="Dashboard"
        level={3}
        description="What the platform can measure right now. Anything it cannot is labelled, not estimated."
      />
      <SystemStatus />

      <section aria-label="account" className="mt-4">
        <DashboardAccount />
      </section>

      <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Panel title="Open positions" level={21}>
          <PositionsTable />
        </Panel>
        <Panel title="Active orders" level={19}>
          <OrdersTable />
        </Panel>
        <Panel title="Active bots" level={22}>
          <BotStatus />
        </Panel>
        <Panel title="Recent trades" level={31}>
          <RecentTrades />
        </Panel>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <Panel title="Market overview" level={8}>
          <Unavailable
            level={8}
            reason="No live quote subscription exists. Bars are normalised and validated; a streaming feed is not built, and a cached last price shown as a quote would be the stalest possible lie."
            today="python tools/cost_profile.py"
          />
        </Panel>
        <Panel title="Research verdicts" level={14}>
          <Unavailable
            level={14}
            reason="Rule searches, exit searches and cost profiles exist as JSON reports and are not served read-only yet."
            today="reports/rule_search*.json"
          />
        </Panel>
      </div>

      <div className="mt-4">
        <ServiceHealth />
      </div>
    </div>
  );
}
