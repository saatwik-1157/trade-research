import { PlannedPage } from "@/components/PlannedPage";

export default function Page() {
  return (
    <PlannedPage
      title="Webhooks"
      level={9}
      description="TradingView alerts enter here and become Signals. They never reach the broker directly."
      sections={[
        { title: "Receiver", level: 9, reason: "Body-secret auth, redaction and IP allowlist exist; schema validation and idempotency are added and the receiver becomes an API route.", today: "python tools/tv_webhook.py --secret ..." },
        { title: "Recent alerts", level: 9, reason: "Served from the alert log once the route exists.", today: "python tools/tv_webhook.py --show" },
        { title: "Idempotency", level: 9, reason: "A replayed alert produces one Signal. Weak-auth (plain-text) alerts are tagged and never reach the OMS." },
        { title: "TradingView CSV import", level: 9, reason: "Strategy Tester and broker history exports with column detection.", today: "python tools/tv_import.py trades.csv --inspect" },
      ]}
    />
  );
}
