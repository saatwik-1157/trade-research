/**
 * Realtime client abstraction.
 *
 * The backend endpoint is `/v1/realtime/ws`, built at L07. Authentication is
 * the session cookie the REST calls already carry, so there is no second
 * token to manage here and no credential in this file.
 *
 * `connect()` is still never called automatically, and the connection state
 * still starts UNKNOWN rather than DISCONNECTED, because we have not tried.
 *
 * Nothing here fabricates an event. Most types in the catalogue have no
 * producer yet -- there is no OMS to emit ORDER_FILLED -- so subscribing to
 * one is legal and will simply never fire. `GET /v1/realtime/catalogue`
 * reports which types are actually produced today, so "quiet" can be told
 * apart from "not built".
 */

/**
 * Mirrors `app/realtime/catalogue.py`. A backend test asserts the two lists
 * are the same set, so a type added on one side and not the other is a test
 * failure rather than a frame silently dropped by `parseEvent`.
 */
export const EVENT_TYPES = [
  "MARKET_UPDATE",
  "SIGNAL_CREATED",
  "SIGNAL_UPDATED",
  "ORDER_CREATED",
  "ORDER_UPDATED",
  "ORDER_SUBMITTED",
  "ORDER_ACKNOWLEDGED",
  "ORDER_PARTIALLY_FILLED",
  "ORDER_FILLED",
  "ORDER_CANCELLED",
  "ORDER_REJECTED",
  "ORDER_FAILED",
  "ORDER_UNKNOWN",
  "POSITION_OPENED",
  "POSITION_UPDATED",
  "POSITION_CLOSED",
  "BOT_STARTED",
  "BOT_PAUSED",
  "BOT_STOPPED",
  "BOT_ERROR",
  "BOT_RECOVERING",
  "RISK_APPROVED",
  "RISK_REJECTED",
  "RISK_ALERT",
  "BROKER_CONNECTED",
  "BROKER_DISCONNECTED",
  "BROKER_RECONNECTING",
  // AI models (L25, L26, L28). Two progress types rather than one per stage:
  // a training run publishes a dozen stage changes, and cataloguing each would
  // make the vocabulary a log format.
  "TRAINING_JOB_UPDATED",
  "VALIDATION_RUN_UPDATED",
  "MODEL_REGISTERED",
  "MODEL_PAPER_ACTIVATED",
  "MODEL_ACTIVATED",
  "MODEL_ROLLED_BACK",
  "MODEL_RETIRED",
  "MODEL_REJECTED",
  // L29 monitoring.
  "MODEL_HEALTH_CHANGED",
  "MODEL_ALERT_CREATED",
  "MODEL_ALERT_RECOVERED",
  // L30 portfolio. Account-scoped; `changes_between` publishes only what moved.
  "PORTFOLIO_UPDATED",
  "EXPOSURE_UPDATED",
  "DRAWDOWN_ALERT",
  "PORTFOLIO_HEALTH_CHANGED",
  // L31 trade journal. Three, not six: `trade.opened` and
  // `trade.partially_closed` are POSITION events that already exist above.
  "TRADE_RECORDED",
  "TRADE_UPDATED",
  "TRADE_RECONCILIATION_REQUIRED",
  // L33 trade review. The interface L34 will consume; L33 sends no notification.
  "TRADE_REVIEW_COMPLETED",
  "TRADE_REVIEW_FAILED",
  "TRADE_PATTERN_DETECTED",
  "SYSTEM_ALERT",
  "NOTIFICATION_CREATED",
  // L39. SECURITY_ALERT is system-scoped and carries a count and a class,
  // never a subject; ACCOUNT_SECURITY_ALERT is user-scoped and carries the
  // subject because it goes only to them. The split is an authorization
  // decision, not a taxonomy -- see backend app/realtime/catalogue.py.
  "SECURITY_ALERT",
  "ACCOUNT_SECURITY_ALERT",
] as const;

/** Control frames the server sends. Not events; never dispatched to handlers. */
export const CONTROL_TYPES = ["SUBSCRIBED", "UNSUBSCRIBED", "PONG"] as const;
export type ControlType = (typeof CONTROL_TYPES)[number];

export interface ControlFrame {
  type: ControlType;
  channels?: string[];
  accepted?: string[];
  refused?: { channel: string; reason: string }[];
  at?: string;
}

export type EventType = (typeof EVENT_TYPES)[number];

export interface RealtimeEvent<T = unknown> {
  id: string;
  type: EventType;
  at: string;
  source: string;
  /** `scope:id` the event was delivered on, e.g. "account:abc". */
  channel: string | null;
  /** The request or run id that caused it, for tracing across services. */
  correlationId: string | null;
  payload: T;
}

export type RealtimeState = "unknown" | "connecting" | "connected" | "disconnected";

type Handler = (event: RealtimeEvent) => void;

/** Reads a control frame, or null if this is not one. */
export function parseControl(raw: string): ControlFrame | null {
  try {
    const data = JSON.parse(raw) as { type?: unknown };
    if (typeof data.type !== "string") return null;
    if (!CONTROL_TYPES.includes(data.type as ControlType)) return null;
    return data as ControlFrame;
  } catch {
    return null;
  }
}

/** Validates an inbound frame. An unknown type is dropped, never coerced. */
export function parseEvent(raw: string): RealtimeEvent | null {
  try {
    const data = JSON.parse(raw) as Partial<RealtimeEvent>;
    if (!data.type || !EVENT_TYPES.includes(data.type as EventType)) return null;
    if (typeof data.payload !== "object" || data.payload === null) return null;
    const raw_ = data as Record<string, unknown>;
    return {
      id: String(data.id ?? ""),
      type: data.type as EventType,
      at: String(data.at ?? ""),
      source: String(data.source ?? "unknown"),
      // Absent stays null. A frame that named no channel must not be shown
      // as belonging to one.
      channel: typeof raw_.channel === "string" ? raw_.channel : null,
      correlationId: typeof raw_.correlation_id === "string" ? raw_.correlation_id : null,
      payload: data.payload,
    };
  } catch {
    return null;
  }
}

/**
 * Bounded set of event ids already delivered.
 *
 * Redis pub/sub may deliver the same event twice to a reconnecting
 * subscriber, and a reconnect replays subscriptions. Receiving ORDER_FILLED
 * twice must not render two fills, so the id is checked before dispatch. It
 * is bounded, so an id can age out on a long-lived tab -- which is why a
 * consumer that must never act twice also checks its own state. This is the
 * cheap first line, not the only one.
 */
export class SeenEvents {
  private ids: string[] = [];
  private set = new Set<string>();

  constructor(private capacity = 512) {}

  seen(id: string): boolean {
    if (!id) return false; // an unidentified frame is never deduplicated away
    if (this.set.has(id)) return true;
    this.set.add(id);
    this.ids.push(id);
    if (this.ids.length > this.capacity) {
      const evicted = this.ids.shift();
      if (evicted !== undefined) this.set.delete(evicted);
    }
    return false;
  }

  get size(): number {
    return this.set.size;
  }
}

export class RealtimeClient {
  private socket: WebSocket | null = null;
  private handlers = new Map<EventType | "*", Set<Handler>>();
  private stateHandlers = new Set<(s: RealtimeState) => void>();
  private controlHandlers = new Set<(f: ControlFrame) => void>();
  private attempts = 0;
  private closedByUs = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private seen = new SeenEvents();

  /** What we asked for, so a reconnect can restore it without the caller. */
  readonly channels = new Set<string>();

  /** Starts UNKNOWN: we have not attempted a connection, so we cannot claim one. */
  state: RealtimeState = "unknown";

  constructor(
    private url: string,
    private maxAttempts = 6,
    private heartbeatMs = 20_000,
  ) {}

  /**
   * Ask the server for these channels.
   *
   * Recorded whether or not the socket is open, so `connect()` can replay
   * them. The server authorizes every one against the database on arrival --
   * being in this set is a request, never a grant, and a refusal comes back
   * on a SUBSCRIBED frame naming the channel.
   */
  subscribe(...channels: string[]): void {
    for (const c of channels) this.channels.add(c);
    this.send({ action: "subscribe", channels });
  }

  unsubscribe(...channels: string[]): void {
    for (const c of channels) this.channels.delete(c);
    this.send({ action: "unsubscribe", channels });
  }

  onControl(handler: (frame: ControlFrame) => void): () => void {
    this.controlHandlers.add(handler);
    return () => this.controlHandlers.delete(handler);
  }

  private send(frame: Record<string, unknown>): boolean {
    if (!this.socket || this.socket.readyState !== 1) return false;
    this.socket.send(JSON.stringify(frame));
    return true;
  }

  on(type: EventType | "*", handler: Handler): () => void {
    const set = this.handlers.get(type) ?? new Set<Handler>();
    set.add(handler);
    this.handlers.set(type, set);
    return () => set.delete(handler);
  }

  onState(handler: (s: RealtimeState) => void): () => void {
    this.stateHandlers.add(handler);
    return () => this.stateHandlers.delete(handler);
  }

  private setState(state: RealtimeState): void {
    this.state = state;
    for (const h of this.stateHandlers) h(state);
  }

  /** Exponential backoff, capped. Returns the delay it will wait. */
  backoffMs(attempt: number): number {
    return Math.min(30_000, 500 * 2 ** Math.max(0, attempt - 1));
  }

  connect(): void {
    if (typeof WebSocket === "undefined") {
      this.setState("disconnected");
      return;
    }
    this.closedByUs = false;
    this.setState("connecting");
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.onopen = () => {
      this.attempts = 0;
      this.setState("connected");
      // Restore what we had. The server re-authorizes each one, so a channel
      // whose permission was revoked while we were away comes back refused
      // rather than silently re-granted.
      if (this.channels.size > 0) {
        this.send({ action: "subscribe", channels: [...this.channels] });
      }
      this.startHeartbeat();
    };
    socket.onmessage = (message: MessageEvent) => {
      const raw = String(message.data);
      const control = parseControl(raw);
      if (control) {
        for (const h of this.controlHandlers) h(control);
        return; // control frames are never dispatched as events
      }
      const event = parseEvent(raw);
      if (!event) return; // malformed or unknown type: dropped, never guessed
      if (this.seen.seen(event.id)) return; // a redelivery is not a second fill
      for (const h of this.handlers.get(event.type) ?? []) h(event);
      for (const h of this.handlers.get("*") ?? []) h(event);
    };
    socket.onclose = () => {
      this.stopHeartbeat();
      this.setState("disconnected");
      if (!this.closedByUs) this.scheduleReconnect();
    };
    socket.onerror = () => this.setState("disconnected");
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    if (this.heartbeatMs <= 0) return;
    this.heartbeat = setInterval(() => this.send({ action: "ping" }), this.heartbeatMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = null;
  }

  private scheduleReconnect(): void {
    this.attempts += 1;
    if (this.attempts > this.maxAttempts) return;
    this.timer = setTimeout(() => this.connect(), this.backoffMs(this.attempts));
  }

  close(): void {
    this.closedByUs = true;
    if (this.timer) clearTimeout(this.timer);
    this.stopHeartbeat();
    this.socket?.close();
    this.socket = null;
    this.setState("disconnected");
  }
}
