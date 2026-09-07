"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Badge, EmptyState, ErrorState, LoadingState, StatTile } from "@/components/ui";
import { accountsService, portfolioService } from "@/lib/services";

/**
 * The account figures on the front page.
 *
 * It replaces six hard-coded empty tiles and a fixed "no broker adapter is
 * wired to the API" notice. That notice had stopped being true: `/v1/accounts`,
 * `/v1/portfolio/summary` and the broker registration route all exist and
 * serve. A front page that reports the platform cannot do what it can do is
 * the same class of error as one that reports a figure it does not have --
 * both are the screen disagreeing with the system.
 *
 * **A dash is never a zero**, the rule `PortfolioDashboard` already states:
 * every monetary field arrives as a string or `null`, and `StatTile` renders
 * `null` as an em dash rather than as `0.00`.
 *
 * **The empty case is a fact, not a limitation.** No account registered is a
 * thing the reader can fix, and it links to where; it is not the same
 * sentence as "this was never built".
 */
export function DashboardAccount() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: accountsService.list });

  // The first account, deliberately, and named on screen. Summing across
  // accounts is what §41 forbids -- a paper balance and a demo balance are
  // not one number -- and silently picking one without saying so would be a
  // figure whose provenance the reader cannot see.
  const account = accounts.data?.[0];

  const summary = useQuery({
    queryKey: ["portfolio", "summary", account?.id],
    queryFn: () => portfolioService.summary(account!.id),
    enabled: Boolean(account),
  });

  if (accounts.isPending) return <LoadingState what="accounts" />;
  if (accounts.isError) {
    return (
      <ErrorState
        message="Accounts could not be read."
        onRetry={() => void accounts.refetch()}
      />
    );
  }
  if (!account) {
    return (
      <EmptyState
        message="No account is registered."
        hint="Register a paper or broker account to see balance, equity and margin here."
      />
    );
  }

  const s = summary.data;
  const acct = s?.account;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-body text-muted">
        <span className="text-ink-2">{account.name}</span>
        <Badge tone={account.environment === "live" ? "warning" : "neutral"}>
          {account.environment}
        </Badge>
        {s && (
          <Badge tone={s.health === "HEALTHY" ? "good" : s.health === "ERROR" ? "critical" : "warning"}>
            {s.health}
          </Badge>
        )}
        {accounts.data.length > 1 && (
          <span>
            +{accounts.data.length - 1} more ·{" "}
            <Link href="/portfolio" className="text-accent hover:underline">
              switch on Portfolio
            </Link>
          </span>
        )}
      </div>

      {summary.isError ? (
        <ErrorState
          message="This account's portfolio summary could not be read."
          onRetry={() => void summary.refetch()}
        />
      ) : (
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-6">
          <StatTile label="Balance" value={acct?.balance} note={acct?.currency ?? undefined} />
          <StatTile label="Equity" value={acct?.equity} note={acct?.currency ?? undefined} />
          <StatTile label="Available margin" value={acct?.margin_free} />
          <StatTile label="Used margin" value={acct?.margin_used} />
          <StatTile
            label="Unrealized P&L"
            value={s?.pnl?.unrealized}
            note={
              s?.pnl?.unrealized_unavailable.length
                ? `${s.pnl.unrealized_unavailable.length} position(s) unpriced`
                : undefined
            }
          />
          <StatTile
            label="Realized P&L"
            value={s?.pnl?.realized}
            note={s?.pnl ? `${s.pnl.realized_trades} closed` : undefined}
          />
        </div>
      )}

      {/* Real figures that are old are not a fault and not fine either -- the
          same distinction PortfolioDashboard draws, kept here so the front
          page cannot be the one screen that quietly drops it. */}
      {acct?.freshness && acct.freshness !== "FRESH" && (
        <p className="text-body text-warning">
          {acct.freshness === "STALE"
            ? `These figures are real and no longer current${acct.as_of ? ` (as of ${acct.as_of})` : ""}.`
            : "How current these figures are is not known."}
        </p>
      )}
      {acct?.unavailable_reason && (
        <p className="text-body text-muted">{acct.unavailable_reason}</p>
      )}
    </div>
  );
}
