import { PageHeader } from "@/components/ui";
import { PlannedSections } from "@/components/PlannedPage";
import { DiscordSettings } from "@/components/DiscordSettings";

export default function Page() {
  return (
    <>
      <PageHeader
        title="Community"
        level={35}
        description="Discord delivery for the platform's own notifications, and shared research. Discord is a destination: it receives what the notification engine already created and decides nothing."
      />
      <div className="space-y-4">
        <DiscordSettings />
        <PlannedSections
          sections={[
            {
              title: "Shared reports",
              level: 35,
              reason: "Research notes that passed the verifier, published read-only.",
              today: "python tools/verify.py report.md snapshot.json --strict",
            },
          ]}
        />
      </div>
    </>
  );
}
