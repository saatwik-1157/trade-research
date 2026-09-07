import { PageHeader } from "@/components/ui";
import { NotificationCenter } from "@/components/NotificationCenter";

export default function Page() {
  return (
    <>
      <PageHeader
        title="Alerts"
        level={34}
        description="Every notification the platform has recorded for you, with the event that caused it. Trading alerts carry their environment; nothing here is a sample."
      />
      <NotificationCenter />
    </>
  );
}
