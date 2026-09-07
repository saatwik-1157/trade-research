import { describe, expect, it } from "vitest";
import {
  CONTROL_TYPES,
  EVENT_TYPES,
  RealtimeClient,
  SeenEvents,
  parseControl,
  parseEvent,
} from "./realtime";

describe("event catalogue", () => {
  it("declares every event the platform will publish", () => {
    for (const type of [
      "MARKET_UPDATE",
      "SIGNAL_CREATED",
      "ORDER_CREATED",
      "ORDER_UPDATED",
      "ORDER_FILLED",
      "ORDER_REJECTED",
      "POSITION_OPENED",
      "POSITION_UPDATED",
      "POSITION_CLOSED",
      "BOT_STARTED",
      "BOT_STOPPED",
      "RISK_ALERT",
      "BROKER_CONNECTED",
      "BROKER_DISCONNECTED",
      "SYSTEM_ALERT",
    ]) {
      expect(EVENT_TYPES).toContain(type);
    }
    // 29 through L27; the eight AI types joined at L28, which is also when
    // they started being produced -- `Hub.publish` had been called with two
    // arguments since L25 and the TypeError was swallowed. Ten more joined at
    // L30 (portfolio), L31 (journal) and L33 (review), and each has a real
    // producer.
    // 52 since L39, which added SECURITY_ALERT and ACCOUNT_SECURITY_ALERT.
    // Two rather than one because the split is an authorization decision: the
    // system-scoped one carries a count and a class and never a subject, so a
    // burst of failed logins cannot publish an email address to every signed-in
    // browser. The per-account one is user-scoped and goes only to them.
    expect(EVENT_TYPES).toHaveLength(52);
    for (const type of [
      "PORTFOLIO_UPDATED",
      "TRADE_RECORDED",
      "TRADE_REVIEW_COMPLETED",
    ]) {
      expect(EVENT_TYPES).toContain(type);
    }
  });

  it("covers the full order lifecycle, including the one we cannot resolve", () => {
    // ORDER_UNKNOWN is not an error case to tidy away: an IPC timeout after
    // order_send looks exactly like a rejection, and the OMS reconciles it
    // rather than retrying.
    for (const type of [
      "ORDER_SUBMITTED",
      "ORDER_ACKNOWLEDGED",
      "ORDER_PARTIALLY_FILLED",
      "ORDER_CANCELLED",
      "ORDER_FAILED",
      "ORDER_UNKNOWN",
    ]) {
      expect(EVENT_TYPES).toContain(type);
    }
  });

  it("records approvals as well as vetoes", () => {
    // An approval that leaves no trace is indistinguishable from a check that
    // never ran.
    expect(EVENT_TYPES).toContain("RISK_APPROVED");
    expect(EVENT_TYPES).toContain("RISK_REJECTED");
  });

  it("keeps control frames out of the event catalogue", () => {
    for (const control of CONTROL_TYPES) {
      expect(EVENT_TYPES).not.toContain(control as unknown as (typeof EVENT_TYPES)[number]);
    }
  });
});

describe("parseControl", () => {
  it("reads a subscription reply", () => {
    const frame = parseControl(
      JSON.stringify({ type: "SUBSCRIBED", channels: ["system"], refused: [] }),
    );
    expect(frame?.type).toBe("SUBSCRIBED");
    expect(frame?.channels).toEqual(["system"]);
  });

  it("is not confused by an event", () => {
    expect(parseControl(JSON.stringify({ type: "ORDER_FILLED", payload: {} }))).toBeNull();
  });
});

describe("SeenEvents", () => {
  it("reports a repeat, so a redelivery is not a second fill", () => {
    const seen = new SeenEvents(3);
    expect(seen.seen("e1")).toBe(false);
    expect(seen.seen("e1")).toBe(true);
  });

  it("never deduplicates an unidentified frame away", () => {
    const seen = new SeenEvents(3);
    expect(seen.seen("")).toBe(false);
    expect(seen.seen("")).toBe(false);
  });

  it("is bounded", () => {
    const seen = new SeenEvents(2);
    seen.seen("a");
    seen.seen("b");
    seen.seen("c");
    expect(seen.size).toBe(2);
  });
});

describe("parseEvent", () => {
  it("accepts a well-formed frame", () => {
    const event = parseEvent(
      JSON.stringify({
        id: "e1",
        type: "ORDER_FILLED",
        at: "2026-09-02T00:00:00Z",
        source: "worker",
        payload: { order_id: "1" },
      }),
    );
    expect(event?.type).toBe("ORDER_FILLED");
    expect(event?.payload).toEqual({ order_id: "1" });
  });

  it("carries the channel and correlation id, and leaves them null when absent", () => {
    const withRouting = parseEvent(
      JSON.stringify({
        type: "ORDER_FILLED",
        payload: {},
        channel: "account:a1",
        correlation_id: "req-9",
      }),
    );
    expect(withRouting?.channel).toBe("account:a1");
    expect(withRouting?.correlationId).toBe("req-9");

    // A frame that named no channel must not be shown as belonging to one.
    const without = parseEvent(JSON.stringify({ type: "ORDER_FILLED", payload: {} }));
    expect(without?.channel).toBeNull();
    expect(without?.correlationId).toBeNull();
  });

  it("drops an unknown type rather than coercing it", () => {
    expect(parseEvent(JSON.stringify({ type: "MADE_UP", payload: {} }))).toBeNull();
  });

  it("drops malformed input", () => {
    expect(parseEvent("not json")).toBeNull();
    expect(parseEvent(JSON.stringify({ type: "ORDER_FILLED" }))).toBeNull();
  });
});

describe("RealtimeClient", () => {
  it("starts UNKNOWN, because no connection has been attempted", () => {
    // Reporting "disconnected" before trying would claim knowledge we lack.
    expect(new RealtimeClient("ws://localhost/ws").state).toBe("unknown");
  });

  it("backs off exponentially and caps", () => {
    const client = new RealtimeClient("ws://localhost/ws");
    expect(client.backoffMs(1)).toBe(500);
    expect(client.backoffMs(2)).toBe(1000);
    expect(client.backoffMs(3)).toBe(2000);
    expect(client.backoffMs(20)).toBe(30_000);
  });

  it("registers and removes handlers", () => {
    const client = new RealtimeClient("ws://localhost/ws");
    const off = client.on("ORDER_FILLED", () => {});
    expect(typeof off).toBe("function");
    off();
  });

  it("records requested channels even with no socket, so a reconnect can replay them", () => {
    const client = new RealtimeClient("ws://localhost/ws");
    client.subscribe("system", "account:a1");
    expect([...client.channels]).toEqual(["system", "account:a1"]);
    client.unsubscribe("account:a1");
    expect([...client.channels]).toEqual(["system"]);
  });

  it("being in the requested set is a request, never a grant", () => {
    // The server authorizes every channel against the database on arrival.
    const client = new RealtimeClient("ws://localhost/ws");
    client.subscribe("account:not-mine");
    expect(client.channels.has("account:not-mine")).toBe(true);
    expect(client.state).toBe("unknown"); // nothing was sent; there is no socket
  });
});
