import { PageHeader } from "@/components/ui";
import { AdminPanel } from "@/components/AdminPanel";

export default function Page() {
  return (
    <>
      <PageHeader
        title="Admin"
        level={36}
        description="Operational visibility, user access, roles, integrations and the audit trail. Every trading control lives on the surface that owns it; this panel reports and links, it does not wrap them."
      />
      <AdminPanel />
    </>
  );
}
