import { PlannedPage } from "@/components/PlannedPage";

export default function Page() {
  return (
    <PlannedPage
      title="Backtesting"
      level={14}
      description="The research harness is complete on the command line. This page will serve its reports and launch runs."
      sections={[
        { title: "Rule search", level: 14, reason: "41 candidates against a permutation null, cut into era blocks, walked forward and clustered by entry date.", today: "python tools/rule_search.py --timeframe D1 --blocks 5" },
        { title: "Exit search", level: 14, reason: "25 entries against 8 exit structures, with era blocks.", today: "python tools/exit_search.py --timeframe D1" },
        { title: "Cost profile and swap", level: 14, reason: "Spread hurdle by symbol, hour and timeframe; overnight financing in points, refusing units it cannot convert.", today: "python tools/cost_profile.py; python tools/swap.py" },
        { title: "Reports", level: 14, reason: "Existing JSON results served read-only. Verdicts are quoted verbatim.", today: "reports/rule_search*.json, exit_search*.json, cost_*.json" },
      ]}
    />
  );
}
