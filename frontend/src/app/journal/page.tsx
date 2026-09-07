import { PageHeader } from "@/components/ui";
import { TradeJournal } from "@/components/TradeJournal";

export default function Page() {
  return (
    <>
      <PageHeader
        title="Journal"
        level={31}
        description="Every completed trade, with the strategy, AI, risk and execution context recorded when it opened. Read R rather than net currency; nothing here is a sample."
      />
      <TradeJournal />
    </>
  );
}
