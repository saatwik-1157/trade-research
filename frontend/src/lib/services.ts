/**
 * The service layer. Components call these, never `fetch`.
 *
 * Every domain the platform will have is declared here now, so a component
 * can be written against a stable shape before the endpoint exists. A service
 * whose backend is not built returns a `Unavailable` result naming the level
 * that builds it — it never returns empty data, because "no positions" and
 * "no positions endpoint" must not look the same on screen.
 */
import { ApiError, request } from "./api";
import type { Health, User } from "./types";
import type { Role } from "./roles";

/** What a service returns when its endpoint does not exist yet. */
export interface Unavailable {
  available: false;
  level: number;
  reason: string;
  today?: string;
}

export interface Available<T> {
  available: true;
  data: T;
}

export type ServiceResult<T> = Available<T> | Unavailable;

function unavailable(level: number, reason: string, today?: string): Unavailable {
  return { available: false, level, reason, today };
}

/** Levels that own each endpoint group, mirrored from MIGRATION_STATUS.md. */
export const OWNING_LEVEL = {
  marketData: 8,
  signals: 16,
  orders: 19,
  positions: 10,
  portfolio: 30,
  bots: 22,
  strategies: 12,
  ai: 24,
  datasets: 23,
  models: 28,
  training: 25,
  notifications: 34,
  journal: 31,
  analytics: 32,
  backtests: 14,
  replay: 15,
} as const;

// ------------------------------------------------------------------ system

export const systemService = {
  health: (): Promise<Health> => request<Health>("/health"),
  ready: (): Promise<ReadyReport> => request<ReadyReport>("/health/ready"),
};

export interface ReadyCheck {
  name: string;
  status: "healthy" | "degraded" | "unavailable";
  detail: string;
  latency_ms: number;
  critical: boolean;
  ok: boolean;
}

export interface ReadyReport {
  status: "healthy" | "degraded" | "unavailable";
  critical_unavailable: string[];
  checks: Record<string, ReadyCheck>;
}

// -------------------------------------------------------------------- auth

export const authService = {
  me: (): Promise<User> => request<User>("/v1/auth/me"),
};

// ------------------------------------------------- not built yet, by level

export interface Quote {
  symbol: string;
  bid: number;
  ask: number;
  last?: number;
  change?: number;
  changePercent?: number;
  spread: number;
  marketOpen?: boolean;
  at: string;
}

/**
 * The seven position states, mirrored from `app/models/execution.py`. Four
 * were added at L21, each because a real situation had nowhere to go — and a
 * UI that collapsed them would hide the difference between a position the
 * venue has confirmed and one it has not.
 */
export type PositionState =
  | "opening"
  | "open"
  | "partially_closed"
  | "closing"
  | "closed"
  | "unknown"
  | "reconciling";

export interface PositionRow {
  id: string;
  symbol: string | null;
  side: "long" | "short";
  /** What is OPEN now. Not what it opened at. */
  quantity: string;
  initial_quantity: string;
  closed_quantity: string;
  entry_price: string;
  /** What this platform INTENDS the protective levels to be. */
  stop_loss: string | null;
  take_profit: string | null;
  /**
   * What the VENUE last reported them to be. `null` means never read, which
   * is not the same as absent — a table that showed the two as one field
   * could not display a position running unprotected while the record says
   * otherwise, which is the disagreement L21 exists to surface.
   */
  broker_stop_loss: string | null;
  broker_take_profit: string | null;
  broker_synced_at: string | null;
  realized_pnl: string | null;
  status: PositionState;
  broker_position_id: string | null;
  mode: string;
  opened_at: string;
  closed_at: string | null;
}

/**
 * The order states, mirrored from `app/oms/state.py`. Twelve as of L19 — the
 * four added there each exist because a real outcome had nowhere to go, and a
 * UI that collapsed them would hide the difference that decides whether a
 * re-send is safe.
 */
export type OrderState =
  | "intent"
  | "submitting"
  | "submitted"
  | "accepted"
  | "partially_filled"
  | "filled"
  | "cancel_requested"
  | "cancelled"
  | "rejected"
  | "expired"
  | "failed"
  | "unknown";

export interface OrderRow {
  id: string;
  intent_id: string;
  symbol: string | null;
  side: "buy" | "sell";
  order_type: "market" | "limit" | "stop";
  quantity: string;
  /** What the venue has actually done. Never assumed from `quantity`. */
  filled_quantity: string;
  average_fill_price: string | null;
  requested_price: string | null;
  stop_loss: string | null;
  take_profit: string | null;
  status: OrderState;
  broker_order_id: string | null;
  reject_reason: string | null;
  error_code: string | null;
  risk_decision_id: string | null;
  mode: string;
  source: string;
  time_in_force: string;
  created_at: string;
  submitted_at: string | null;
  filled_at: string | null;
  updated_at: string | null;
}

/**
 * The nine bot states, mirrored from `app/bots/state.py`. Three were added at
 * L22 and one of them fixed a defect: a paused bot used to be recorded as
 * `stopping`, so a paused bot and a bot shutting down were the same row.
 */
export type BotState =
  | "starting"
  | "running"
  | "paused"
  | "stopping"
  | "stopped"
  | "crashed"
  | "recovering"
  | "halted"
  | "disabled";

export interface BotRow {
  bot_id: string;
  name: string;
  mode: string;
  enabled: boolean;
  disabled: boolean;
  disabled_reason: string | null;
  strategy_version_id: string | null;
  paper_account_id: string | null;
  broker_account_id: string | null;
  limits: {
    max_positions: number | null;
    max_daily_trades: number | null;
    max_daily_loss: string | null;
    max_risk_per_trade: string | null;
    cooldown_seconds: number | null;
  };
  run_id: string | null;
  status: BotState | null;
  started_at: string | null;
  ended_at: string | null;
  stop_reason: string | null;
  last_heartbeat_at: string | null;
  /** Measured against the clock. A run whose row says RUNNING and whose
   *  heartbeat stopped is exactly what the supervisor exists to catch. */
  heartbeat_age_seconds: number | null;
  heartbeat_stale: boolean;
}

// ------------------------------------------------------------------ market

export const marketService = {
  /**
   * Deliberately not implemented. There is no live tick subscription: L08
   * built the normalised bar store and the adapters, and a quote stream is
   * the piece still missing. Returning empty data here would make "no feed"
   * and "a flat market" look the same.
   */
  quotes: async (): Promise<ServiceResult<Quote[]>> =>
    unavailable(
      OWNING_LEVEL.marketData,
      "No live quote subscription exists. L08 normalises bars and validates them; a streaming feed that publishes MARKET_UPDATE is not built, and a cached last price presented as a quote would be the stalest possible lie.",
      "python tools/market.py",
    ),
};

// --------------------------------------------------------------- positions

export const positionService = {
  /**
   * The platform's own position record, bound at L21. It is NOT a read of the
   * broker's book: `broker_*` fields say what the venue last reported, and
   * `broker_synced_at` says when — a row that has never been synced shows
   * those as unknown rather than implying agreement.
   */
  list: async (): Promise<ServiceResult<PositionRow[]>> => {
    const page = await request<{ items: PositionRow[] }>("/v1/positions?limit=100");
    return { available: true, data: page.items };
  },

  /** Close all of it, or the part `quantity` names. The backend refuses a
   *  quantity larger than what is open rather than clamping it. */
  close: (positionId: string, body: { quantity?: string; reason?: string } = {}) =>
    request<Record<string, unknown>>(`/v1/positions/${positionId}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  /** Set the levels the platform intends. Omitting one leaves it alone; there
   *  is no way to remove a protective stop from here. */
  protect: (positionId: string, body: { stop_loss?: string; take_profit?: string }) =>
    request<Record<string, unknown>>(`/v1/positions/${positionId}/protect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

// ------------------------------------------------------------------ orders

export const orderService = {
  /**
   * The real order record. Bound at L19: `GET /v1/orders` serves the imported
   * demo ledger plus every order the OMS has since created, each carrying its
   * own mode so simulator and broker orders are never pooled by omission.
   */
  list: async (): Promise<ServiceResult<OrderRow[]>> => {
    const page = await request<{ items: OrderRow[] }>("/v1/orders?limit=100");
    return { available: true, data: page.items };
  },

  events: (orderId: string): Promise<OrderEventRow[]> =>
    request<OrderEventRow[]>(`/v1/orders/${orderId}/events`),

  /**
   * Submission goes through the backend's gate chain — sizing, risk, OMS,
   * adapter — and the browser computes none of it. `Idempotency-Key` is what
   * stops a double-clicked button from becoming two orders, so it is required
   * rather than optional.
   */
  submit: (body: Record<string, unknown>, idempotencyKey: string): Promise<OrderRow> =>
    request<OrderRow>("/v1/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    }),

  cancel: (orderId: string, accountId: string): Promise<OrderRow> =>
    request<OrderRow>(
      `/v1/orders/${orderId}/cancel?account_id=${encodeURIComponent(accountId)}`,
      { method: "POST" },
    ),
};

export interface OrderEventRow {
  id: string;
  order_id: string;
  event_type: string;
  occurred_at: string;
  comment: string | null;
}

export const botService = {
  /**
   * Every bot this user owns, with its latest run and MEASURED health. Bound
   * at L22: `heartbeat_stale` is computed against the clock rather than read
   * off `status`, because "the database says RUNNING" is not evidence that
   * anything is running.
   */
  list: async (): Promise<ServiceResult<BotRow[]>> => {
    const page = await request<{ items: BotRow[] }>("/v1/bots");
    return { available: true, data: page.items };
  },

  get: (botId: string) => request<Record<string, unknown>>(`/v1/bots/${botId}`),

  /** Would this bot start? Runs every gate a start runs, and starts nothing. */
  preflight: (botId: string) =>
    request<Record<string, unknown>>(`/v1/bots/${botId}/preflight`, { method: "POST" }),

  /**
   * Take a bot out of service. Stronger than stop, and it closes NO position:
   * everything the bot holds stays under the position manager.
   */
  disable: (botId: string, reason: string) =>
    request<Record<string, unknown>>(`/v1/bots/${botId}/disable`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    }),

  enable: (botId: string) =>
    request<Record<string, unknown>>(`/v1/bots/${botId}/enable`, { method: "POST" }),
};

/** One account a portfolio can be read for. Paper and live are kept apart. */
export type AccountOption = {
  id: string;
  name: string;
  environment: "paper" | "demo" | "live";
  currency: string | null;
};

export type PortfolioHealthState =
  | "HEALTHY"
  | "WARNING"
  | "STALE"
  | "RECONCILIATION_REQUIRED"
  | "ERROR";

/**
 * Every monetary field is a string or null, never a number.
 *
 * A string because a float balance is a rounded balance; null because an
 * absent figure is an absent figure. A `0` here would be a measurement, and
 * the whole level exists to stop one being invented.
 */
export type AccountStateRow = {
  account_id: string;
  environment: string;
  currency: string | null;
  broker: string | null;
  balance: string | null;
  equity: string | null;
  margin_used: string | null;
  margin_free: string | null;
  unrealized_pnl: string | null;
  realized_pnl: string | null;
  as_of: string | null;
  source: string;
  freshness: "FRESH" | "STALE" | "UNKNOWN";
  unavailable_reason: string | null;
};

export type BucketRow = {
  long: string;
  short: string;
  gross: string;
  net: string;
  long_positions: number;
  short_positions: number;
  positions: number;
  uncomputable: number;
};

export type PortfolioSummaryRow = {
  at: string;
  environment: string;
  health: PortfolioHealthState;
  health_reasons: string[];
  freshness: string;
  account: AccountStateRow;
  pnl: {
    realized: string | null;
    unrealized: string | null;
    total: string | null;
    realized_today: string | null;
    trades_today: number | null;
    realized_trades: number;
    open_positions: number;
    unrealized_unavailable: string[];
  } | null;
  drawdown: {
    peak_equity: string | null;
    current: string | null;
    current_pct: number | null;
    max_drawdown: string | null;
    recovered: boolean | null;
  } | null;
  margin: { used: string | null; equity: string | null; ratio: number | null } | null;
  exposure: { gross: string; net: string; positions: number; uncomputable: number } | null;
  position_count: number;
};

export type PortfolioPositionRow = {
  position_id: string;
  symbol: string;
  side: string;
  quantity: string;
  entry_price: string;
  current_price: string | null;
  unrealized_pnl: string | null;
  stop_loss: string | null;
  take_profit: string | null;
  strategy_id: string | null;
  bot_id: string | null;
  asset_class: string | null;
  notional: { value: string | null; computable: boolean; reason: string; priced_at: string };
};

export type ExposureReportRow = {
  total: BucketRow;
  by_symbol: Record<string, BucketRow>;
  by_strategy: Record<string, BucketRow>;
  by_bot: Record<string, BucketRow>;
  by_asset_class: Record<string, BucketRow>;
  by_currency: Record<string, string> | null;
  currency_note: string;
  concentration: {
    by_symbol: Record<string, number>;
    largest: { symbol: string; share: number } | null;
    note: string;
  };
  uncomputable: { position_id: string; symbol: string; reason: string }[];
};

export const accountsService = {
  /**
   * Every account the signed-in user owns, paper and broker together in one
   * LIST but never pooled in a figure: each row carries its environment, and
   * the portfolio is read one account at a time.
   */
  list: async (): Promise<AccountOption[]> => {
    const [paper, broker] = await Promise.all([
      request<{ items: { id: string; name: string; currency: string }[] }>(
        "/v1/accounts/paper?limit=100",
      ),
      request<{
        items: { id: string; name: string; account_mode: string; currency: string | null }[];
      }>("/v1/accounts/broker?limit=100"),
    ]);
    return [
      ...paper.items.map((a) => ({
        id: a.id,
        name: a.name,
        environment: "paper" as const,
        currency: a.currency,
      })),
      ...broker.items.map((a) => ({
        id: a.id,
        name: a.name,
        environment: (a.account_mode === "live" ? "live" : "demo") as "demo" | "live",
        currency: a.currency,
      })),
    ];
  },
};

export const portfolioService = {
  summary: (accountId: string) =>
    request<PortfolioSummaryRow>(
      `/v1/portfolio/summary?account_id=${encodeURIComponent(accountId)}`,
    ),

  positions: (accountId: string) =>
    request<{ environment: string; positions: PortfolioPositionRow[]; count: number; unmarked: string[] }>(
      `/v1/portfolio/positions?account_id=${encodeURIComponent(accountId)}`,
    ),

  exposure: (accountId: string) =>
    request<{
      environment: string;
      exposure: ExposureReportRow | null;
      open_risk: {
        total: string;
        unstopped_positions: number;
        uncomputable_positions: number;
        complete: boolean;
        note: string;
      };
      correlation: { available: boolean; reason: string; closest_available: string };
    }>(`/v1/portfolio/exposure?account_id=${encodeURIComponent(accountId)}`),

  reconciliation: (accountId: string) =>
    request<{
      environment: string;
      reconciliation: {
        checked: boolean;
        agrees: boolean;
        internal_positions: number;
        broker_positions: number;
        mismatches: string[];
        note: string;
      };
    }>(`/v1/portfolio/reconciliation?account_id=${encodeURIComponent(accountId)}`),

  history: (accountId: string, limit = 50) =>
    request<{
      items: {
        taken_at: string;
        balance: string;
        equity: string;
        open_positions: number;
        drawdown_pct: number | null;
      }[];
      page: { total: number };
    }>(`/v1/portfolio/history?account_id=${encodeURIComponent(accountId)}&limit=${limit}`),
};

export type TradeStatus =
  | "open"
  | "partially_closed"
  | "closed"
  | "reconciliation_required"
  | "unknown";

export type TradeRow = {
  id: string;
  mode: string;
  status: TradeStatus;
  symbol: string | null;
  side: string;
  volume: string;
  entry_price: string;
  exit_price: string;
  opened_at: string;
  closed_at: string;
  gross_profit: string;
  commission: string;
  swap: string;
  fees: string | null;
  net_profit: string;
  r_multiple: string | null;
  currency: string | null;
  exit_reason: string | null;
  strategy_version_id: string | null;
  bot_id: string | null;
  position_id: string | null;
  order_id: string | null;
  broker_position_id: string | null;
  source: string;
  data_quality: {
    checked: boolean;
    findings: { code: string; detail: string; severity: string }[];
    errors: number;
    warnings: number;
  } | null;
};

export type TradeTimeline = {
  trade_id: string;
  events: {
    at: string;
    source: string;
    source_note: string;
    kind: string;
    detail: Record<string, unknown>;
  }[];
  count: number;
  gaps: string[];
  note: string;
};

export type AbsentBlock = { available: false; what: string; why: string };

export type TradeDecisions = {
  trade_id: string;
  environment: string;
  strategy: AbsentBlock | Record<string, unknown>;
  ai: AbsentBlock | Record<string, unknown>;
  risk: AbsentBlock | Record<string, unknown>;
  sizing: AbsentBlock | Record<string, unknown>;
  execution: Record<string, unknown>;
  costs: Record<string, string | null>;
  holding: { seconds: number | null; hours: number | null; note: string };
};

export type TradeStatistics = {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  net_profit: string | null;
  gross_profit: string | null;
  commission: string | null;
  swap: string | null;
  r_multiple: { sum: string | null; mean: string | null; trades_with_r: number; note: string };
  by_exit_reason: Record<string, number>;
  by_mode: Record<string, number>;
  reconciliation_required: number;
  not_computed: Record<string, string>;
};

export type TradeFilters = {
  mode?: string;
  symbol?: string;
  side?: string;
  result?: string;
  exit_reason?: string;
  status?: string;
  account_id?: string;
  search?: string;
  limit?: number;
  offset?: number;
};

function query(filters: TradeFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export const journalService = {
  /**
   * The trade list. No mode filter is applied unless one is asked for.
   *
   * Every row carries its own `mode`, and defaulting to paper would hide live
   * trades from somebody who asked for all of them -- the worse of the two
   * failures section 34 is guarding against.
   */
  list: (filters: TradeFilters = {}) =>
    request<{ items: TradeRow[]; page: { total: number; limit: number; offset: number } }>(
      `/v1/trades${query(filters)}`,
    ),

  get: (tradeId: string) => request<TradeRow>(`/v1/trades/${encodeURIComponent(tradeId)}`),

  timeline: (tradeId: string) =>
    request<TradeTimeline>(`/v1/trades/${encodeURIComponent(tradeId)}/timeline`),

  decisions: (tradeId: string) =>
    request<TradeDecisions>(`/v1/trades/${encodeURIComponent(tradeId)}/decisions`),

  executions: (tradeId: string) =>
    request<{
      trade_id: string;
      available: boolean;
      why?: string;
      closes?: {
        at: string;
        quantity: string;
        fill_price: string;
        reason: string;
        realized_running: string | null;
        broker_deal_id: string | null;
        fill_source: string | null;
      }[];
      exit?: { price: string | null; quantity: string; closes: number; note: string };
      entry_price?: string;
      entry_note?: string;
    }>(`/v1/trades/${encodeURIComponent(tradeId)}/executions`),

  analysis: (tradeId: string) =>
    request<{
      trade_id: string;
      status: string;
      recorded: TradeRow["data_quality"];
      recomputed: NonNullable<TradeRow["data_quality"]> & { note: string };
      holding: { seconds: number | null; hours: number | null };
      note: string;
    }>(`/v1/trades/${encodeURIComponent(tradeId)}/analysis`),

  statistics: (filters: Pick<TradeFilters, "mode" | "account_id"> = {}) =>
    request<TradeStatistics>(`/v1/trades/statistics${query(filters)}`),

  /** The CSV export URL. Fetched by the browser so the session cookie applies. */
  exportUrl: (filters: TradeFilters = {}) => `/v1/trades/export${query(filters)}`,
};

export type EvidenceKind = "OBSERVED" | "INTERPRETED" | "HYPOTHESIS";
export type ReviewRating = "GOOD" | "FAIR" | "POOR" | "UNKNOWN";
export type ReviewStatus =
  | "PENDING"
  | "PROCESSING"
  | "COMPLETED"
  | "FAILED"
  | "RETRYING"
  | "CANCELLED";

export type ReviewEvidence = {
  kind: EvidenceKind;
  statement: string;
  source: string | null;
};

export type ReviewSection = {
  rating: ReviewRating;
  evidence: ReviewEvidence[];
  warnings: string[];
  unavailable_reason: string | null;
};

export type TradeReviewRow = {
  id: string;
  trade_id: string;
  environment: string;
  review_version: number;
  status: ReviewStatus;
  summary: string | null;
  outcome: string | null;
  compliance: string | null;
  strategy_alignment: ReviewSection | null;
  entry_quality: ReviewSection | null;
  exit_quality: ReviewSection | null;
  risk_quality: ReviewSection | null;
  execution_quality: ReviewSection | null;
  market_context: Record<string, unknown> | null;
  ai_context: Record<string, unknown> | null;
  key_factors: ReviewEvidence[];
  warnings: string[];
  lessons: ReviewEvidence[];
  follow_up_questions: string[];
  confidence: number | null;
  completeness: { available: string[]; missing: string[]; fraction: number } | null;
  attribution: {
    review_model: string | null;
    review_model_version: string | null;
    prediction_model: string | null;
    prediction_model_version: string | null;
  };
  schema_version: string;
  prompt_version: string | null;
  validation: { valid: boolean; errors: string[] } | null;
  attempts: number;
  error: string | null;
  duration_ms: number | null;
  created_at: string | null;
  completed_at: string | null;
};

export type ReviewEnvelope = {
  trade_id: string;
  available: boolean;
  eligible?: boolean;
  why?: string;
  review?: TradeReviewRow;
};

export type PatternBlock = {
  observations: {
    dimension: string;
    group: string;
    trades: number;
    statement: string;
    reliable: boolean;
    sample_note: string;
  }[];
  reliable_count: number;
  total_observations: number;
  minimum_sample: number;
  method: string;
  authority: string;
  caution: string;
};

export const reviewService = {
  /** What a review contains, and what deliberately produces it. */
  contract: () =>
    request<{
      schema_version: string;
      prompt_version: string;
      provider: { name: string; version: string; external: boolean; note: string };
      evidence_kinds: Record<string, string>;
      unknown_means: string;
      future_leakage: string;
      does_not: string[];
    }>("/v1/trade-reviews/contract"),

  forTrade: (tradeId: string) =>
    request<ReviewEnvelope>(`/v1/trades/${encodeURIComponent(tradeId)}/review`),

  generate: (tradeId: string) =>
    request<{ created: boolean; reason: string; review: TradeReviewRow | null }>(
      `/v1/trades/${encodeURIComponent(tradeId)}/review`,
      { method: "POST" },
    ),

  regenerate: (reviewId: string) =>
    request<{ created: boolean; previous_version: number; review: TradeReviewRow | null }>(
      `/v1/trade-reviews/${encodeURIComponent(reviewId)}/regenerate`,
      { method: "POST" },
    ),

  versions: (tradeId: string) =>
    request<{ items: TradeReviewRow[]; page: { total: number } }>(
      `/v1/trade-reviews?trade_id=${encodeURIComponent(tradeId)}&limit=50`,
    ),

  patterns: (environment?: string) =>
    request<PatternBlock>(
      `/v1/trade-reviews/patterns${environment ? `?environment=${environment}` : ""}`,
    ),

  summary: (environment?: string) =>
    request<{
      by_status: Record<string, number>;
      by_outcome: Record<string, number>;
      reviews: number;
      reviewable_trades: number;
      note: string;
    }>(`/v1/trade-reviews/summary${environment ? `?environment=${environment}` : ""}`),
};

export const strategyService = {
  list: async (): Promise<ServiceResult<never>> =>
    unavailable(
      OWNING_LEVEL.strategies,
      "The strategy registry is not built. Rules exist in the toolkit and have no measured edge.",
      "python tools/rule_search.py",
    ),
};

/** A metric that could not be computed from the sample available. */
export const INSUFFICIENT_DATA = "INSUFFICIENT_DATA";

/** Every metric is a number OR the sentinel. Never silently one of them. */
export type Metric = number | typeof INSUFFICIENT_DATA;

export type MetricBlock = {
  unit: string;
  poolable_across_instruments: boolean;
  trades: number;
  winning_trades: number;
  losing_trades: number;
  breakeven_trades: number;
  win_rate: Metric;
  loss_rate: Metric;
  gross_profit: number;
  gross_loss: number;
  net_profit: number;
  average_trade: Metric;
  average_win: Metric;
  average_loss: Metric;
  largest_win: Metric;
  largest_loss: Metric;
  payoff_ratio: Metric;
  profit_factor: Metric;
  expectancy: Metric;
  standard_deviation: Metric;
  sharpe_per_trade: Metric;
  sortino_per_trade: Metric;
  t_statistic: Metric;
  streaks: {
    longest_win: number;
    longest_loss: number;
    current_win: number;
    current_loss: number;
    note: string;
  };
  distribution: Record<string, unknown>;
  durations: { available: boolean; average_seconds?: number; why?: string };
  sample_note: string;
  empty_reason?: string;
};

export type AnalyticsSummary = {
  scope: Record<string, unknown>;
  trade_count: number;
  currency: MetricBlock;
  r_multiple: MetricBlock;
  costs: {
    gross_profit: number;
    commission: number;
    swap: number;
    fees: number;
    total_costs: number;
    net_profit: number;
    reconciles: boolean;
    note: string;
    slippage: string;
  };
  environments: Record<string, number>;
  which_to_read: string;
  sample_warning?: string;
};

export type EquityBlock = {
  realized: {
    environment: string;
    kind: string;
    points: { at: string; value: number }[];
    count: number;
    final_value: number;
    note: string;
  };
  account: { available: boolean; why?: string; points?: { at: string; value: number }[]; note?: string };
  note: string;
};

export type DrawdownBlock = {
  available: boolean;
  why?: string;
  peak_equity?: number;
  current_drawdown?: number;
  in_drawdown?: boolean;
  max_drawdown?: number;
  max_drawdown_pct?: Metric;
  recovery_factor?: Metric;
  period_count?: number;
  periods?: {
    peak_at: string;
    trough_at: string;
    depth: number;
    recovered: boolean;
    recovery_seconds: Metric;
  }[];
  note?: string;
};

export type BreakdownBlock = {
  dimension: string;
  total_trades: number;
  group_count: number;
  groups: Record<
    string,
    { trades: number; currency: MetricBlock; r_multiple: MetricBlock; below_comparison_floor: boolean }
  >;
  note: string;
};

export type ExecutionBlock = {
  orders: number;
  by_status: Record<string, number>;
  fill_ratio: Metric;
  rejection_ratio: Metric;
  partial_fills: number;
  latency_seconds: {
    create_to_submit: Record<string, unknown>;
    submit_to_fill: Record<string, unknown>;
    measured_from: string;
  };
  slippage_points: Record<string, unknown>;
  slippage_note: string;
  fills: number;
};

export type AnalyticsFilters = {
  environment?: string;
  account_id?: string;
  strategy_version_id?: string;
  symbol?: string;
  bot_id?: string;
  model_key?: string;
  model_version?: string;
  ai_mode?: string;
  period?: string;
};

function analyticsQuery(filters: AnalyticsFilters & Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export const analyticsService = {
  /** What analytics measures, and what it deliberately will not. */
  contract: () =>
    request<{
      source_of_truth: Record<string, string>;
      definitions: Record<string, string>;
      sample_floors: { ratio: number; comparison: number; note: string };
      dimensions: string[];
      units: Record<string, string>;
      does_not: string[];
    }>("/v1/analytics"),

  summary: (filters: AnalyticsFilters = {}) =>
    request<AnalyticsSummary>(`/v1/analytics/summary${analyticsQuery(filters)}`),

  equity: (filters: AnalyticsFilters = {}) =>
    request<EquityBlock>(`/v1/analytics/equity${analyticsQuery(filters)}`),

  drawdown: (filters: AnalyticsFilters = {}) =>
    request<DrawdownBlock>(`/v1/analytics/drawdown${analyticsQuery(filters)}`),

  breakdown: (dimension: string, filters: AnalyticsFilters = {}) =>
    request<BreakdownBlock>(
      `/v1/analytics/breakdown${analyticsQuery({ ...filters, dimension })}`,
    ),

  timeBreakdown: (bucket: string, filters: AnalyticsFilters = {}) =>
    request<{ bucket: string; buckets: Record<string, { trades: number; currency: MetricBlock }> }>(
      `/v1/analytics/time-breakdown${analyticsQuery({ ...filters, bucket })}`,
    ),

  execution: (filters: AnalyticsFilters = {}) =>
    request<ExecutionBlock>(`/v1/analytics/execution${analyticsQuery(filters)}`),

  exposure: (filters: AnalyticsFilters = {}) =>
    request<{
      available: boolean;
      why?: string;
      exposure?: Record<string, unknown> | null;
      source?: string;
      authority?: string;
    }>(`/v1/analytics/exposure${analyticsQuery(filters)}`),
};

export const aiService = {
  models: async (): Promise<ServiceResult<never>> =>
    unavailable(
      OWNING_LEVEL.ai,
      "No model exists. Monitoring and validation are ready for one; nothing has been trained.",
    ),
};

// ----------------------------------------------------------- notifications

/** Mirrors `app/notifications/contract.py`. Five, ordered by importance. */
export type NotificationSeverity = "INFO" | "SUCCESS" | "WARNING" | "ERROR" | "CRITICAL";

/** Mirrors `Category`. What a preference is expressed against. */
export type NotificationCategory =
  | "TRADING"
  | "RISK"
  | "PORTFOLIO"
  | "BOTS"
  | "STRATEGIES"
  | "AI"
  | "MONITORING"
  | "BROKER"
  | "SYSTEM"
  | "SECURITY";

export type NotificationChannel = "IN_APP" | "EMAIL" | "DISCORD";

export type DeliveryStatus =
  | "PENDING"
  | "PROCESSING"
  | "DELIVERED"
  | "FAILED"
  | "RETRYING"
  | "SKIPPED";

export interface NotificationRow {
  id: string;
  event_id: string | null;
  event_type: string;
  category: NotificationCategory | null;
  severity: NotificationSeverity;
  /**
   * `null` means this is not about a trading environment — a model
   * registration is neither paper nor live. `"unknown"` is different: it means
   * the event was about trading and did not say where. Neither is ever
   * rendered as "paper".
   */
  environment: string | null;
  title: string;
  body: string | null;
  entity_type: string | null;
  entity_id: string | null;
  read: boolean;
  read_at: string | null;
  created_at: string;
  context: Record<string, unknown>;
}

export interface NotificationDeliveryRow {
  id: string;
  notification_id: string;
  channel: NotificationChannel;
  status: DeliveryStatus;
  attempt_count: number;
  next_attempt_at: string | null;
  last_attempt_at: string | null;
  delivered_at: string | null;
  failure_reason: string | null;
  provider_message_id: string | null;
  duration_ms: number | null;
  created_at: string;
}

export interface NotificationPreferenceRow {
  category: NotificationCategory;
  channel: NotificationChannel;
  enabled: boolean;
  min_severity: NotificationSeverity;
  /** "user" when it was saved, "default" when the platform is deciding. */
  source: "user" | "default";
  /** The in-app channel: it can be quietened, never switched off. */
  locked: boolean;
}

export interface ChannelStatus {
  channel: NotificationChannel;
  state: "CONFIGURED" | "NOT_CONFIGURED" | "DISABLED";
  available: boolean;
  detail?: string;
  [key: string]: unknown;
}

export interface DiscordStatus {
  channel: "DISCORD";
  state: "CONFIGURED" | "NOT_CONFIGURED" | "DISABLED";
  available: boolean;
  detail: string;
  transport?: string;
  /** "system": one destination for the deployment. See how_to_enable. */
  scope?: string;
  commands?: boolean;
  sent?: number;
  failed?: number;
  rate_limited?: number;
  how_to_enable: string;
  rotation: string;
}

export interface DiscordTestResult {
  status: string;
  delivered: boolean;
  detail: string;
  retryable: boolean;
  note: string;
}

export interface NotificationFilters {
  category?: NotificationCategory | "";
  severity?: NotificationSeverity | "";
  environment?: string;
  unread?: boolean;
  limit?: number;
  offset?: number;
}

function notificationQuery(f: NotificationFilters): string {
  const q = new URLSearchParams();
  if (f.category) q.set("category", f.category);
  if (f.severity) q.set("severity", f.severity);
  if (f.environment) q.set("environment", f.environment);
  if (f.unread !== undefined) q.set("unread", String(f.unread));
  if (f.limit !== undefined) q.set("limit", String(f.limit));
  if (f.offset !== undefined) q.set("offset", String(f.offset));
  const s = q.toString();
  return s ? `?${s}` : "";
}

/**
 * The notification centre's data. Built at L34.
 *
 * There is no `create` here and there is none on the backend either: a
 * notification exists only as a consequence of a domain event the platform
 * published, which is what makes a fabricated trading alert unrepresentable
 * rather than merely discouraged.
 */
export const notificationService = {
  list: (filters: NotificationFilters = {}) =>
    request<{
      items: NotificationRow[];
      page: { total: number; limit: number; offset: number; has_more: boolean };
    }>(`/v1/notifications${notificationQuery(filters)}`),

  unreadCount: () =>
    request<{ unread: number; by_severity: Record<string, number> }>(
      "/v1/notifications/unread-count",
    ),

  markRead: (id: string) =>
    request<{ notification: NotificationRow; unread: number }>(
      `/v1/notifications/${encodeURIComponent(id)}/read`,
      { method: "PATCH" },
    ),

  markAllRead: () =>
    request<{ marked_read: number; unread: number }>("/v1/notifications/read-all", {
      method: "POST",
    }),

  preferences: () =>
    request<{
      preferences: NotificationPreferenceRow[];
      locked: { channel: NotificationChannel; severities: string[]; why: string };
      safety_note: string;
    }>("/v1/notifications/preferences"),

  savePreferences: (
    updates: {
      category: NotificationCategory;
      channel: NotificationChannel;
      enabled: boolean;
      min_severity: NotificationSeverity;
    }[],
  ) =>
    request<{ preferences: NotificationPreferenceRow[] }>("/v1/notifications/preferences", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates }),
    }),

  deliveries: (limit = 50) =>
    request<{ items: NotificationDeliveryRow[]; page: { total: number } }>(
      `/v1/notifications/deliveries?limit=${limit}`,
    ),

  channels: () => request<{ channels: ChannelStatus[] }>("/v1/notifications/channels"),

  /**
   * Discord's configuration state. Status only — the API never returns the
   * webhook URL, not even redacted, so there is nothing here that could be
   * rendered into a page or logged by a browser extension.
   */
  discordStatus: () => request<DiscordStatus>("/v1/notifications/discord"),

  /**
   * Send a message that says it is a test. Administrators only, enforced by
   * the backend: it creates no notification, publishes no event, and names no
   * trade, symbol, price or account.
   */
  sendDiscordTest: () =>
    request<DiscordTestResult>("/v1/notifications/discord/test", { method: "POST" }),

  contract: () =>
    request<{
      severities: NotificationSeverity[];
      categories: NotificationCategory[];
      channels: NotificationChannel[];
      environments: string[];
      always_delivered_in_app: string[];
      events: {
        event_type: string;
        category: string;
        severity: string;
        entity_type: string | null;
        cooldown_seconds: number | null;
        producing_now: boolean;
      }[];
      not_notified: Record<string, string>;
      channel_status: ChannelStatus[];
      environment_note: string;
      does_not: string[];
    }>("/v1/notifications/contract"),
};

/**
 * Where a notification's entity lives in this frontend. Section 35.
 *
 * The backend names the entity and never the URL, so this table is the only
 * place a route is written down — renaming a page changes one line here rather
 * than invalidating every notification already stored. An entity with no
 * destination returns null and the row renders without a link, which is
 * honest; a link to a page that does not exist is not.
 */
export function notificationHref(row: NotificationRow): string | null {
  if (!row.entity_type || !row.entity_id) return null;
  switch (row.entity_type) {
    case "trade":
    case "trade_review":
      return "/journal";
    case "order":
      return "/orders";
    case "position":
      return "/positions";
    case "bot":
      return "/bots";
    case "account":
      return row.category === "RISK" ? "/risk" : "/portfolio";
    case "model_version":
      return "/ai-lab";
    case "system":
      return "/monitoring";
    default:
      return null;
  }
}

// ------------------------------------------------------------------- admin

export interface AdminEnvironment {
  environment: string;
  trading_mode: string;
  live_trading: boolean;
  live_execution_allowed: boolean;
  live_execution_blockers: string[];
  note: string;
}

export interface AdminDashboard {
  environment: AdminEnvironment;
  users: { total: number; active: number; by_role: Record<string, number> };
  accounts: { paper: number; broker: number; mt5_connections: number };
  bots: { total: number; enabled: number; runs_live: number };
  strategies: { total: number; active: number };
  models: { versions: number; deployments_active: number };
  trading: {
    trades: number;
    positions_open: number;
    orders_working: number;
    orders_needing_reconciliation: number;
  };
  notifications: { total: number; deliveries_queued: number; deliveries_failed: number };
  authority: string;
}

export interface AdminUserRow {
  id: string;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface AdminUserDetail extends Omit<AdminUserRow, "role"> {
  role: string;
  updated_at: string;
  accounts: {
    paper: { id: string; name: string; status: string }[];
    broker: {
      id: string;
      name: string;
      broker: string;
      environment: string;
      is_active: boolean;
    }[];
  };
  bots: { id: string; name: string; environment: string; enabled: boolean }[];
  sessions_active: number;
  note: string;
}

export interface AdminSession {
  id: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  active: boolean;
  user_agent: string | null;
}

export interface AuditRow {
  id: string;
  actor_user_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  occurred_at: string;
  ip: string | null;
  request_id: string | null;
  details: Record<string, unknown> | null;
}

export interface AdminIntegrations {
  notifications: { enabled: boolean; channels: ChannelStatus[]; consumer: unknown };
  email: { configured: boolean; enabled: boolean };
  discord: ChannelStatus;
  tradingview_webhook: { secret_configured: boolean; restrict_to_tradingview_ips: boolean };
  event_bus: { kind: string; enabled: boolean };
  brokers: {
    adapters_registered: number;
    order_managers_registered: number;
    note: string;
  };
  never_returned: string[];
}

export interface AdminConfiguration {
  read_only: Record<string, unknown>;
  live_gates: { gates: Record<string, boolean>; all_built: boolean; note: string };
  runtime_editable: string[];
  runtime_editable_note: string;
  feature_flags: { implemented: boolean; why_not: string };
}

export interface AdminUserFilters {
  search?: string;
  role?: string;
  active?: boolean;
  limit?: number;
  offset?: number;
}

export interface AuditFilters {
  action?: string;
  resource_type?: string;
  actor_user_id?: string;
  environment?: string;
  limit?: number;
  offset?: number;
}

// Named apart from the `query` the trade filters already use: two functions
// with one name is a duplicate implementation, and the one that wins is
// whichever the bundler saw last.
function adminQuery(params: Record<string, string | number | boolean | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

/**
 * The administration surface. Built at L36.
 *
 * There is deliberately no `pauseBot`, `promoteModel` or `reconcileBroker`
 * here, and none on the backend either: those controls live on the surface
 * that owns them, already gated by the same permissions. A second client-side
 * door would be a second thing to keep in step with the first.
 */
export const adminService = {
  dashboard: () => request<AdminDashboard>("/v1/admin/dashboard"),

  contract: () =>
    request<{
      environment: Record<string, unknown>;
      delegates_to: Record<string, string>;
      delegation_note: string;
      writes_here: string[];
      dangerous_actions_require: string[];
      cannot: string[];
      roles: string[];
      mfa: string;
    }>("/v1/admin/contract"),

  permissions: () =>
    request<{
      roles: { role: string; permissions: string[]; count: number }[];
      permissions: { permission: string; min_role: string; dangerous: boolean }[];
      note: string;
      mfa: string;
    }>("/v1/admin/permissions"),

  integrations: () => request<AdminIntegrations>("/v1/admin/integrations"),

  configuration: () => request<AdminConfiguration>("/v1/admin/configuration"),

  users: (filters: AdminUserFilters = {}) =>
    request<{
      items: AdminUserRow[];
      page: { total: number; limit: number; offset: number; has_more: boolean };
    }>(`/v1/admin/users/search${adminQuery({ ...filters })}`),

  user: (id: string) => request<AdminUserDetail>(`/v1/admin/users/${encodeURIComponent(id)}`),

  sessions: (id: string) =>
    request<{ sessions: AdminSession[] }>(
      `/v1/admin/users/${encodeURIComponent(id)}/sessions`,
    ),

  /**
   * Every one of these takes a reason and a confirmation phrase, and the
   * backend refuses without both. The phrase is the subject's own email:
   * a confirmation you can click without reading is not a confirmation.
   */
  deactivateUser: (id: string, reason: string, confirm: string) =>
    request<Record<string, unknown>>(
      `/v1/admin/users/${encodeURIComponent(id)}/deactivate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason, confirm }),
      },
    ),

  activateUser: (id: string, reason: string, confirm: string) =>
    request<Record<string, unknown>>(`/v1/admin/users/${encodeURIComponent(id)}/activate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, confirm }),
    }),

  revokeSessions: (id: string, reason: string, confirm: string) =>
    request<{ sessions_revoked: number; note: string }>(
      `/v1/admin/users/${encodeURIComponent(id)}/revoke-sessions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason, confirm }),
      },
    ),

  setRole: (id: string, role: Role) =>
    request<AdminUserRow>(`/v1/admin/users/${encodeURIComponent(id)}/role`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    }),

  auditLogs: (filters: AuditFilters = {}) =>
    request<{
      items: AuditRow[];
      page: { total: number; limit: number; offset: number; has_more: boolean };
    }>(`/v1/admin/audit-logs${adminQuery({ ...filters })}`),

  auditActions: () =>
    request<{
      actions: { action: string; count: number }[];
      retention: string;
      immutability: string;
    }>("/v1/admin/audit-logs/actions"),
};

// -------------------------------------------------------------- monitoring

/** Mirrors `app/observability/contract.py`. Five, not two. */
export type ComponentState =
  | "HEALTHY"
  | "DEGRADED"
  | "UNHEALTHY"
  | "UNKNOWN"
  | "NOT_CONFIGURED";

export type TradingSafety = "SAFE" | "DEGRADED" | "BLOCKED" | "UNKNOWN";

export interface ComponentRow {
  name: string;
  layer: string;
  status: ComponentState;
  criticality: "CRITICAL" | "IMPORTANT" | "OPTIONAL";
  detail: string;
  last_checked: string;
  latency_ms: number | null;
  error_count: number;
  last_error: string | null;
  facts: Record<string, unknown>;
}

export interface MonitoringSummary {
  status: ComponentState;
  trading_safety: TradingSafety;
  trading_safety_reasons: string[];
  trading_safety_note?: string;
  environment: {
    environment: string;
    trading_mode: string;
    live_trading: boolean;
    live_execution_allowed: boolean;
    live_execution_blockers: string[];
  };
  collected_at: string | null;
  duration_ms?: number;
  components: number;
  not_healthy?: ComponentRow[];
  aggregation?: string;
  note?: string;
}

export interface IncidentRow {
  id: string;
  component: string;
  event_type: string;
  level: string;
  occurred_at: string;
  correlation_id: string | null;
  payload: Record<string, unknown>;
}

/**
 * Platform monitoring, built at L37.
 *
 * `summary` is readable by any signed-in user; everything else needs
 * `manage_system_settings` and answers 403 without it. The frontend never
 * decides a component's state and never derives trading safety: both come from
 * the backend, which reads them from the systems that own them.
 */
export const monitoringService = {
  summary: () => request<MonitoringSummary>("/v1/monitoring/summary"),

  components: () =>
    request<{
      collected_at: string | null;
      layers: Record<string, ComponentRow[]>;
      components: ComponentRow[];
    }>("/v1/monitoring/components"),

  component: (name: string) =>
    request<ComponentRow>(`/v1/monitoring/components/${encodeURIComponent(name)}`),

  events: (limit = 25) =>
    request<{ events: IncidentRow[]; retention: string }>(
      `/v1/monitoring/events?limit=${limit}`,
    ),

  metrics: () =>
    request<{
      metrics: {
        name: string;
        kind: string;
        help: string;
        series: { labels: Record<string, string>; value: number }[];
      }[];
      cardinality: { max_series_per_metric: number; forbidden_labels: string[]; note: string };
    }>("/v1/monitoring/metrics"),

  thresholds: () => request<Record<string, unknown>>("/v1/monitoring/thresholds"),

  uptime: () =>
    request<{ available: boolean; why: string; collections_this_process: number }>(
      "/v1/monitoring/uptime",
    ),

  contract: () =>
    request<{
      component_states: ComponentState[];
      state_meanings: Record<string, string>;
      trading_safety_derived_from: string[];
      aggregation: string;
      does_not: string[];
      recovery: string;
    }>("/v1/monitoring/contract"),

  /** Runs the same read-only pass the worker runs. Administrators only. */
  collect: () =>
    request<MonitoringSummary & { incidents: unknown[] }>("/v1/monitoring/collect", {
      method: "POST",
    }),
};

// ---------------------------------------------------------------- recovery

export type RecoveryStateName =
  | "NORMAL"
  | "DEGRADED"
  | "UNAVAILABLE"
  | "RECONNECTING"
  | "RECONCILING"
  | "RECOVERED"
  | "SAFE_MODE"
  | "UNKNOWN";

export interface SafeModeLatch {
  reason: string;
  detail: string;
  at: string;
  actor_user_id: string | null;
}

export interface SafeModeStatus {
  engaged: boolean;
  reasons: SafeModeLatch[];
  blocks: string[];
  still_allowed: string[];
  authority: string;
  persistence: string;
  history: Record<string, unknown>[];
}

export interface RecoveryStep {
  step: string;
  status: "OK" | "ATTENTION" | "SKIPPED" | "FAILED";
  detail: string;
  at: string;
  blocking: boolean;
  facts: Record<string, unknown>;
}

export interface RecoveryReport {
  kind: string;
  at: string;
  clean: boolean;
  steps: RecoveryStep[];
  needs_attention: RecoveryStep[];
  safe_mode_engaged: string[];
  authority: string;
}

export interface RecoveryStatus {
  state: RecoveryStateName;
  safe_mode: SafeModeStatus;
  startup: RecoveryReport | null;
  last_reconciliation: RecoveryReport | null;
  environment: {
    trading_mode: string;
    live_trading: boolean;
    live_execution_allowed: boolean;
  };
  sequence: { step: string; what: string }[];
  rules: string[];
}

/**
 * Recovery, built at L38.
 *
 * The frontend never decides that recovery succeeded: `state`, `safe_mode` and
 * every step come from the backend, which derived them from the reconcilers
 * that own each piece of state. Releasing safe mode re-runs the sequence
 * server-side and refuses while a condition still holds — this client cannot
 * clear a latch by asking nicely.
 */
export const recoveryService = {
  status: () => request<RecoveryStatus>("/v1/recovery/status"),

  sequence: () =>
    request<{ sequence: { step: string; what: string }[]; rule: string; live_trading: string }>(
      "/v1/recovery/sequence",
    ),

  reconciliation: () => request<RecoveryReport>("/v1/recovery/reconciliation"),

  contract: () =>
    request<{
      reconcilers: Record<string, string>;
      reconcilers_note: string;
      safe_mode_reasons: string[];
      settling_an_unknown_order: string;
      cannot: string[];
      rules: string[];
    }>("/v1/recovery/contract"),

  /** Read-only: it compares and reports, and repairs nothing. */
  reconcile: (reason: string) =>
    request<RecoveryReport>("/v1/recovery/reconcile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    }),

  enterSafeMode: (reason: string) =>
    request<RecoveryStatus>("/v1/recovery/safe-mode/enter", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    }),

  exitSafeMode: (reason: string) =>
    request<RecoveryStatus & { report: RecoveryReport }>("/v1/recovery/safe-mode/exit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    }),
};

export { ApiError };

// -------------------------------------------------------- position sizing

/** One sizing answer, exactly as `POST /v1/position-sizing/calculate` returns it. */
export interface SizingResult {
  status: "VALID" | "REFUSED";
  sizing_mode: string;
  symbol: string | null;
  side: string | null;
  entry_price: string | null;
  stop_loss: string | null;
  stop_distance: string | null;
  risk_amount: string | null;
  risk_per_unit: string | null;
  raw_quantity: string | null;
  final_quantity: string | null;
  actual_risk: string | null;
  reason: string;
  gap: string | null;
  warnings: string[];
  broker_constraints: Record<string, string> | null;
  authority: string;
  risk?: { outcome?: string; reason?: string; evaluated?: boolean; note?: string };
}

export interface SizingInput {
  symbol: string;
  side: "buy" | "sell";
  sizing_mode: string;
  entry_price?: string;
  stop_loss?: string;
  equity?: string;
  risk_percent?: string;
  risk_amount?: string;
  quantity?: string;
}

export const sizingService = {
  /**
   * Every figure on the sizing panel comes from here. The browser does not
   * compute a quantity: a size calculated in two places is two sizes, and the
   * one the user reads would not be the one an order would carry.
   */
  calculate: (input: SizingInput): Promise<SizingResult> =>
    request<SizingResult>("/v1/position-sizing/calculate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
};

// ---------------------------------------------------------------- datasets

export type DatasetStatus = "RAW" | "CLEAN" | "READY";

export interface DatasetRow {
  id: string;
  key: string;
  version: string;
  status: DatasetStatus;
  ready: boolean;
  provider: string;
  timeframe: string;
  rows: number;
  start: string | null;
  end: string | null;
  quality_score: number | null;
  fingerprint: string | null;
  feature_set_version: string | null;
  label_set_version: string | null;
  leakage_passed: boolean | null;
  leakage_failed_checks: number | null;
  blocked_reason: string | null;
  created_at: string | null;
}

export interface FeatureSpecRow {
  name: string;
  version: string;
  description: string;
  formula: string;
  inputs: string[];
  lookback: number;
  unit: string;
  timestamp_policy: string;
}

export const datasetService = {
  /**
   * Every dataset with the verdict validation reached. Bound at L23.
   *
   * `ready` is NOT a property the browser decides or a caller can set: the
   * builder marks a dataset READY only when every leakage check passed, and
   * there is no route that overrides it. A blocked dataset carries the reason.
   */
  list: async (): Promise<ServiceResult<DatasetRow[]>> => {
    const page = await request<{ items: DatasetRow[] }>("/v1/datasets");
    return { available: true, data: page.items };
  },

  /**
   * The feature registry: name, formula, inputs, lookback and unit. Served by
   * the backend rather than mirrored here, because a formula written down in
   * two places is a formula that will disagree with itself.
   */
  features: async (): Promise<ServiceResult<FeatureSpecRow[]>> => {
    const body = await request<{ features: FeatureSpecRow[] }>("/v1/datasets/features");
    return { available: true, data: body.features };
  },

  /** The whole recipe, for explaining a dataset without reading the code. */
  manifest: (datasetId: string) =>
    request<Record<string, unknown>>(`/v1/datasets/${datasetId}/manifest`),

  /** Every validation finding, including the ones that passed. */
  checks: (datasetId: string) =>
    request<Record<string, unknown>>(`/v1/datasets/${datasetId}/checks`),
};

// -------------------------------------------------------------- ai models

export interface ModelContract {
  features: string[];
  feature_version: string;
  lookback: number;
  compatible_feature_versions: string[];
  policy: string;
}

export interface ModelRow {
  model: string;
  version: string;
  kind: string;
  feature_version: string;
  label_version: string | null;
  dataset_version: string | null;
  dataset_fingerprint: string | null;
  trained_at: string | null;
  fitted: boolean;
  contract: ModelContract;
  deterministic: boolean;
  authority: string;
  calibrated?: boolean;
  classes?: string[];
}

export const modelService = {
  /**
   * Every model this deployment can answer with. Bound at L24.
   *
   * An empty list is the expected state: no model is loaded by default, and a
   * deployment with none answers MODEL_UNAVAILABLE rather than a default —
   * which under AI_REQUIRED means no trade.
   */
  list: async (): Promise<ServiceResult<ModelRow[]>> => {
    const page = await request<{ items: ModelRow[] }>("/v1/ai/models");
    return { available: true, data: page.items };
  },

  /** What each family may say, and the four things a number here is not. */
  vocabulary: () => request<Record<string, unknown>>("/v1/ai/vocabulary"),
};

// -------------------------------------------------------------- training

export type TrainingStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "cancelled"
  | "failed"
  | "finished"
  | "validation_pending";

export interface TrainingJobRow {
  id: string;
  model: string | null;
  model_version: string | null;
  dataset: string | null;
  dataset_fingerprint: string | null;
  config_fingerprint: string | null;
  status: TrainingStatus;
  stage: string | null;
  progress: number | null;
  random_seed: number | null;
  model_version_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  candidate_only: string;
}

export const trainingService = {
  /**
   * Training jobs, newest first. Bound at L25.
   *
   * `validation_pending` is where a SUCCESSFUL run ends. It means a candidate
   * exists - not that a model is approved for trading, which is L26's verdict
   * and L28's promotion.
   */
  list: async (): Promise<ServiceResult<TrainingJobRow[]>> => {
    const page = await request<{ items: TrainingJobRow[] }>("/v1/ai/training/jobs");
    return { available: true, data: page.items };
  },

  metrics: (jobId: string) =>
    request<Record<string, unknown>>(`/v1/ai/training/jobs/${jobId}/metrics`),

  /** Cooperative: the job stops at the next boundary and is marked cancelled. */
  cancel: (jobId: string) =>
    request<Record<string, unknown>>(`/v1/ai/training/jobs/${jobId}/cancel`, { method: "POST" }),
};


// ------------------------------------------------------------ validation

export type ValidationStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "cancelled"
  | "failed"
  | "completed";

/**
 * What a report concluded. Deliberately four values and not a number.
 *
 * BLOCKED is a real outcome, not an error: it means a check could not be
 * evaluated, and reporting that as PASS or FAIL would be a claim about the
 * model the evidence does not support.
 */
export type ValidationVerdict = "PASS" | "FAIL" | "CONDITIONAL" | "BLOCKED";

export type ValidationSeverity = "PASS" | "WARNING" | "FAIL" | "BLOCKED";

export interface ValidationCheck {
  check: string;
  severity: ValidationSeverity;
  summary: string;
  evidence: Record<string, unknown>;
}

export interface ValidationReport {
  validation_engine_version: string;
  verdict: ValidationVerdict;
  summary: string;
  /** What this verdict authorises, in the backend's own words. Rendered verbatim. */
  means: string;
  authority: string;
  checks: ValidationCheck[];
  counts: Record<ValidationSeverity, number>;
  recommendations: string[];
  config: Record<string, unknown>;
  context: Record<string, unknown>;
  method: { scoring: string; blocked: string; separation: string; reproducibility: string };
}

export interface ValidationRunRow {
  id: string;
  model_version_id: string;
  training_run_id: string | null;
  dataset_id: string | null;
  dataset_fingerprint: string | null;
  status: ValidationStatus;
  verdict: ValidationVerdict | null;
  summary: string | null;
  stage: string | null;
  progress: number | null;
  checks: Record<ValidationSeverity, number | null>;
  validation_engine_version: string | null;
  config_fingerprint: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  authority: string;
}

export const validationService = {
  /**
   * Validation runs, newest first. Bound at L26.
   *
   * There is no `promote` here and no `activate`, because the API has neither:
   * a PASS makes a candidate eligible for CONSIDERATION by the registry, which
   * is L28's and a human decision.
   */
  list: async (): Promise<ServiceResult<ValidationRunRow[]>> => {
    const page = await request<{ items: ValidationRunRow[] }>("/v1/ai/validation/runs");
    return { available: true, data: page.items };
  },

  report: (runId: string) =>
    request<{ run_id: string; status: ValidationStatus; report: ValidationReport | null; note?: string }>(
      `/v1/ai/validation/runs/${runId}/report`,
    ),

  /** Cooperative: the run stops at a stage boundary and is marked cancelled. */
  cancel: (runId: string) =>
    request<Record<string, unknown>>(`/v1/ai/validation/runs/${runId}/cancel`, { method: "POST" }),
};


// ------------------------------------------------ AI strategy integration

/**
 * How much the AI layer may affect a strategy signal. Four values and no fifth.
 *
 * There is deliberately no mode in which the AI originates a trade: every mode
 * takes a signal the strategy already produced, and the strongest thing any of
 * them can do is decline it.
 */
export type AiMode = "AI_DISABLED" | "AI_ADVISORY" | "AI_FILTER" | "AI_SCORING";

/** What happens when the AI cannot answer. Never implicit. */
export type AiPolicy = "AI_REQUIRED" | "AI_OPTIONAL";

export type AiDecisionValue = "ACCEPT" | "REJECT" | "NEUTRAL" | "ERROR";

export interface AiModelRef {
  key: string;
  version: string;
  optional?: boolean;
}

export interface AiStrategyConfigRow {
  id: string;
  strategy_key: string;
  account_id: string | null;
  mode: AiMode;
  policy: AiPolicy;
  enabled: boolean;
  required_models: AiModelRef[];
  optional_models: AiModelRef[];
  feature_version: string | null;
  thresholds: {
    minimum_probability: number;
    maximum_anomaly_score: number | null;
    allowed_regimes: string[];
    minimum_expected_return: number | null;
    maximum_latency_ms: number;
    scoring_method: string;
    ai_weight: number;
    minimum_combined_score: number;
  };
  notes: string | null;
  updated_at: string | null;
  authority: string;
}

export interface AiDecisionRow {
  id: string;
  strategy_key: string;
  strategy_version: number | null;
  symbol: string | null;
  timeframe: string | null;
  bar_time: string | null;
  side: string | null;
  mode: AiMode;
  policy: AiPolicy;
  decision: AiDecisionValue;
  /** Whether inference RAN, separate from what the mode concluded. */
  status: string;
  reason: string | null;
  model: string | null;
  model_key: string | null;
  model_version: string | null;
  feature_version: string | null;
  probability: number | null;
  predicted_class: string | null;
  regime: string | null;
  anomaly_score: number | null;
  confidence: number | null;
  strategy_score: number | null;
  combined_score: number | null;
  latency_ms: { features: number | null; inference: number | null; total: number | null };
  /** What happened AFTER the AI layer. `risk_vetoed` is common and correct. */
  final_outcome: string | null;
  risk_verdict: string | null;
  order_id: string | null;
  execution_id: string | null;
  created_at: string | null;
  authority: string;
}

export interface AiEligibleModel {
  eligible: boolean;
  reason: string;
  model_key: string | null;
  model_version: string | null;
  status: string | null;
  verdict: string | null;
}

export const aiIntegrationService = {
  /**
   * What the AI layer may do to a strategy signal, and what it may not. L27.
   *
   * Served by the backend rather than restated here, so the page cannot drift
   * from the guarantees the code actually enforces.
   */
  contract: () =>
    request<{
      modes: Record<AiMode, string>;
      policies: Record<AiPolicy, string>;
      decisions: AiDecisionValue[];
      scoring_methods: Record<string, string>;
      pipeline: string[];
      guarantees: string[];
      does_not: string[];
      risk_authority: string;
      default: string;
    }>("/v1/ai/integration"),

  configs: async (): Promise<ServiceResult<AiStrategyConfigRow[]>> => {
    const page = await request<{ items: AiStrategyConfigRow[] }>("/v1/ai/integration/strategies");
    return { available: true, data: page.items };
  },

  decisions: async (): Promise<ServiceResult<AiDecisionRow[]>> => {
    const page = await request<{ items: AiDecisionRow[] }>("/v1/ai/integration/decisions");
    return { available: true, data: page.items };
  },

  /** Versions a strategy may legitimately name, with the reason either way. */
  models: () => request<{ items: AiEligibleModel[]; eligible: number }>("/v1/ai/integration/models"),
};


// -------------------------------------------------------- model registry

/**
 * Where a model version is in its life. Eight values and no more.
 *
 * `promoted` is the brief's ACTIVE under the spelling this table has used since
 * L05. `rejected` and `retired` are TERMINAL — and neither is a deletion: a
 * historical version is needed for audit, backtesting, trade review and
 * reproducibility.
 */
export type ModelStatus =
  | "draft"
  | "validated"
  | "registered"
  | "paper"
  | "promoted"
  | "rejected"
  | "rolled_back"
  | "retired";

export type DeploymentStatus = "active" | "superseded" | "rolled_back" | "stopped";

export interface ModelArtifactInfo {
  sha256: string | null;
  bytes: number | null;
  kind: string | null;
}

export interface ModelVersionRow {
  id: string;
  model: string | null;
  sequence: number;
  artifact_ref: string | null;
  status: ModelStatus;
  features: string[] | null;
  feature_version: string | null;
  label_version: string | null;
  dataset_version: string | null;
  dataset_fingerprint: string | null;
  code_version: string | null;
  metrics: Record<string, unknown> | null;
  calibrated: boolean;
  training_period: string[] | null;
  test_period: string[] | null;
  created_at: string | null;
  artifact: ModelArtifactInfo;
  registered_at: string | null;
  promoted_at: string | null;
  retired_at: string | null;
  /** Whether a version in this status may answer a prediction at all. */
  serves_inference: boolean;
}

export interface ModelDeploymentRow {
  id: string;
  model: string;
  version: string | null;
  model_version_id: string;
  strategy_key: string | null;
  symbol: string | null;
  timeframe: string | null;
  environment: string;
  status: DeploymentStatus;
  activated_at: string | null;
  deactivated_at: string | null;
  previous_version_id: string | null;
  reason: string | null;
}

export interface ModelLifecycleEventRow {
  id: string;
  model: string;
  version: string | null;
  from: string | null;
  to: string;
  at: string | null;
  actor_user_id: string | null;
  reason: string | null;
  environment: string | null;
}

export const modelRegistryService = {
  /**
   * The lifecycle, its transitions, and what the registry will not do. L28.
   *
   * Served by the backend rather than restated here, so a page cannot drift
   * from the transition table the code enforces.
   */
  contract: () =>
    request<{
      statuses: Record<ModelStatus, string>;
      transitions: Record<string, string[]>;
      serving: string[];
      terminal: string[];
      declined: Record<string, string>;
      guarantees: string[];
      does_not: string[];
      artifact: { storage: string; integrity: string; security: string; kinds: string[] };
      authorization: Record<string, string>;
      trading_safety: string;
    }>("/v1/ai/registry"),

  versions: async (modelKey: string): Promise<ServiceResult<ModelVersionRow[]>> => {
    const page = await request<{ items: ModelVersionRow[] }>(
      `/v1/ai/models/${modelKey}/versions`,
    );
    return { available: true, data: page.items };
  },

  deployments: async (modelKey: string): Promise<ServiceResult<ModelDeploymentRow[]>> => {
    const page = await request<{ items: ModelDeploymentRow[] }>(
      `/v1/ai/models/${modelKey}/deployments`,
    );
    return { available: true, data: page.items };
  },

  history: async (modelKey: string): Promise<ServiceResult<ModelLifecycleEventRow[]>> => {
    const page = await request<{ items: ModelLifecycleEventRow[] }>(
      `/v1/ai/models/${modelKey}/history`,
    );
    return { available: true, data: page.items };
  },

  /** Which version a scope resolves to, and every check that answer passed. */
  resolve: (modelKey: string, query: Record<string, string> = {}) =>
    request<{
      request: Record<string, unknown>;
      resolved: Record<string, unknown> | null;
      reason?: string;
      note?: string;
    }>(`/v1/ai/models/${modelKey}/resolve?${new URLSearchParams(query).toString()}`),
};


// ------------------------------------------------------ AI model monitoring

/**
 * How a deployed model version is behaving. Six values.
 *
 * The two that are not about the model matter most: `INSUFFICIENT_DATA` says the
 * sample could not support a conclusion, and `OFFLINE` says nothing is
 * deployed. Both are commonly rendered as healthy, and both mean the opposite.
 */
export type ModelHealthState =
  | "HEALTHY"
  | "WARNING"
  | "DEGRADED"
  | "CRITICAL"
  | "INSUFFICIENT_DATA"
  | "OFFLINE";

export type AlertStatus = "firing" | "acknowledged" | "resolved";

export interface MonitoringFinding {
  check: string;
  subject: string;
  severity: string;
  summary: string;
  statistic: number | null;
  current_n: number;
  reference_n: number;
}

export interface MonitoringBlock {
  findings: MonitoringFinding[];
  n: number;
  severity: string;
}

export interface MonitoringSnapshotRow {
  id: string;
  model: string;
  version: string | null;
  model_version_id: string;
  scope: {
    strategy_key: string | null;
    symbol: string | null;
    timeframe: string | null;
    environment: string;
  };
  health: ModelHealthState;
  /** §5: never silently changed, so the snapshot records which one it used. */
  baseline: { kind: string | null; id: string | null; period: (string | null)[] };
  window: { start: string; end: string };
  sample_count: number;
  metrics: {
    feature: MonitoringBlock | null;
    prediction: MonitoringBlock | null;
    calibration: MonitoringBlock | null;
    performance: MonitoringBlock | null;
    latency: MonitoringBlock | null;
    availability: MonitoringBlock | null;
    trading: Record<string, unknown> | null;
    regime: MonitoringBlock | null;
  };
  health_detail: Record<string, unknown> | null;
  created_at: string | null;
  note: string | null;
}

export interface ModelHealthRow {
  model: string;
  version: string | null;
  model_version_id: string;
  environment: string;
  scope: { strategy_key: string | null; symbol: string | null; timeframe: string | null };
  health: ModelHealthState;
  measured_at: string | null;
  sample_count: number;
  note: string | null;
}

export interface ModelAlertRow {
  id: string;
  fingerprint: string;
  model: string;
  version: string | null;
  check: string;
  subject: string;
  severity: string;
  status: AlertStatus;
  title: string;
  body: string | null;
  metric: string | null;
  current_value: number | null;
  baseline_value: number | null;
  threshold: number | null;
  sample_size: number | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  resolved_at: string | null;
  occurrences: number;
  authority: string;
}

export const modelMonitoringService = {
  /** What monitoring measures and what it will not do. Served, not restated. */
  contract: () =>
    request<{
      monitoring_engine_version: string;
      health_states: Record<ModelHealthState, string>;
      baselines: Record<string, string>;
      drift_kinds: Record<string, string>;
      windows: Record<string, unknown>;
      thresholds: Record<string, unknown>;
      does_not: string[];
      statistical_caution: string;
    }>("/v1/ai/monitoring"),

  health: async (): Promise<ServiceResult<ModelHealthRow[]>> => {
    const page = await request<{ items: ModelHealthRow[] }>("/v1/ai/monitoring/health");
    return { available: true, data: page.items };
  },

  snapshots: async (): Promise<ServiceResult<MonitoringSnapshotRow[]>> => {
    const page = await request<{ items: MonitoringSnapshotRow[] }>(
      "/v1/ai/monitoring/snapshots",
    );
    return { available: true, data: page.items };
  },

  alerts: async (): Promise<ServiceResult<ModelAlertRow[]>> => {
    const page = await request<{ items: ModelAlertRow[] }>("/v1/ai/monitoring/alerts");
    return { available: true, data: page.items };
  },
};
