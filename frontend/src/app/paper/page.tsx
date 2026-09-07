import { PlannedPage } from "@/components/PlannedPage";

export default function Page() {
  return (
    <PlannedPage
      title="Paper Trading"
      level={16}
      description="PAPER is the internal simulator; DEMO is the MT5 demo account. Both route through Risk, Sizing and the OMS once those exist."
      sections={[
        { title: "Paper session", level: 16, reason: "Strategy-driven simulated venue on the fake broker, spread charged, ambiguous bars booked as losses.", today: "tools/rule_backtest.simulate()" },
        { title: "Demo session (MT5)", level: 16, reason: "The existing demo loop, routed through the pipeline. 252 closed demo trades are already on file.", today: "python tools/mt5_paper.py --rule random --live --once" },
        { title: "Session controls", level: 22, reason: "Start, stop and heartbeat come from the bot manager." },
        { title: "Results", level: 31, reason: "Read the R-multiple and the date-clustered t, never the balance.", today: "python tools/track_record.py --merge" },
      ]}
    />
  );
}
