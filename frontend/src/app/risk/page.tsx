import { PageHeader } from "@/components/PageHeader";
import { Panel } from "@/components/Panel";
import { SizingCalculator } from "@/components/SizingCalculator";
import { SystemStatus } from "@/components/SystemStatus";
import { Unavailable } from "@/components/Unavailable";

export default function RiskPage() {
  return (
    <div>
      <PageHeader
        title="Risk"
        level={17}
        description="The gates that stand between a signal and an order. Today only the mode gates exist; the engine itself is L17."
      />
      <SystemStatus />
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <Panel title="Risk engine" level={17}>
          <Unavailable
            level={17}
            reason="Veto object, per-currency exposure caps and the kill switch. The daily-loss and max-position caps exist inline in the demo loop today."
            today="tools/mt5_paper.py --max-positions --max-daily-loss"
          />
        </Panel>
        <Panel title="Kill switch" level={17}>
          <Unavailable
            level={17}
            reason="File, setting and API switch checked before every order. Today the only stop is Ctrl+C on the running loop."
          />
        </Panel>
        <Panel title="Position sizing" level={18}>
          {/* Real backend calculation. Every figure comes from
              POST /v1/position-sizing/calculate; nothing here is computed in
              the browser, and the panel creates no order. */}
          <SizingCalculator />
        </Panel>
        <Panel title="Demo fence" level={2}>
          <p className="text-body text-ink-2">
            Real accounts are refused in code before any order is constructed
            (<code className="font-mono">tools/mt5_paper.assert_demo</code>). No setting on this
            platform overrides it.
          </p>
        </Panel>
      </div>
    </div>
  );
}
