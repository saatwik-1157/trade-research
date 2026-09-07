import { PlannedPage } from "@/components/PlannedPage";

export default function Page() {
  return (
    <PlannedPage
      title="Broker Connections"
      level={10}
      description="One BrokerAdapter interface; MT5 is the first implementation and the fake broker is the second."
      sections={[
        { title: "MetaTrader 5", level: 10, reason: "Connect, account, positions, orders, history, place, modify, cancel, close, wrapped from the existing modules. Demo accounts only, enforced in code.", today: "tools/mt5_account.py; tools/mt5_paper.py" },
        { title: "Connection state", level: 10, reason: "Disconnect: stop new orders, alert, reconnect, reconcile, resume only if safe. Today each call handles a missing result locally." },
        { title: "Symbol mapping", level: 11, reason: "TradingView ticker, MT5 symbol, ccxt pair and yfinance ticker in one table, with point size and unit class.", today: "tools/rule_backtest.py SYMBOLS; tools/universe.txt" },
        { title: "Credentials", level: 4, reason: "MT5 connects to the local terminal over IPC; no credentials are stored by this platform. Any future broker credential lives in the backend only." },
      ]}
    />
  );
}
