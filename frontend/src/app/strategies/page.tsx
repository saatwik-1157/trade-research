import { PlannedPage } from "@/components/PlannedPage";

export default function Page() {
  return (
    <PlannedPage
      title="Strategies"
      level={12}
      description="Registry of strategies with ids, versions and the research verdict attached to each."
      sections={[
        { title: "Live-demo tier", level: 12, reason: "sma_cross, rsi_reversion and random, wrapped as Strategy objects that emit Signals.", today: "tools/mt5_paper.py RULES" },
        { title: "Research tier (41 candidates)", level: 12, reason: "RSI, MA crosses, Donchian, Bollinger, momentum and their inverses. None has cleared its null out of sample.", today: "tools/rule_search.py build_candidates()" },
        { title: "Visual strategy builder", level: 13, reason: "Parametric builder over the same factories. A built strategy is research-only until a search report is attached.", today: "tools/rule_search.py make_*()" },
        { title: "Promotion gate", level: 26, reason: "Permutation null, Bonferroni, era blocks, walk-forward and date clustering, all required before a strategy can trade.", today: "reports/rule_search*.json" },
      ]}
    />
  );
}
