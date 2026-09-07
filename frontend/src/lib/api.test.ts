import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, csrfToken, fetchHealth, login } from "./api";

const health = {
  status: "ok",
  time: "2026-09-02T00:00:00+00:00",
  service: "trade-research-platform",
  version: "0.2.0",
  environment: "development",
  trading_mode: "paper",
  live_trading: false,
  live_execution_allowed: false,
  live_execution_blockers: ["LIVE_TRADING is false"],
};

afterEach(() => vi.unstubAllGlobals());

describe("fetchHealth", () => {
  it("returns the parsed body on 200", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(health), { status: 200 })),
    );
    const h = await fetchHealth();
    expect(h.trading_mode).toBe("paper");
    expect(h.live_execution_allowed).toBe(false);
  });

  it("throws ApiError on a non-2xx status instead of returning a default", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("down", { status: 503 })));
    await expect(fetchHealth()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("CSRF", () => {
  it("sends the token on state-changing requests and omits it on reads", async () => {
    document.cookie = "tr_csrf=token-abc";
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await login("a@b.io", "correct horse battery");
    const [, postInit] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((postInit.headers as Record<string, string>)["X-CSRF-Token"]).toBe("token-abc");

    await fetchHealth();
    const [, getInit] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    // A read cannot change state, so it carries no token.
    expect((getInit.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
  });

  it("reads the token from the cookie the API set", () => {
    document.cookie = "tr_csrf=abc123";
    expect(csrfToken()).toBe("abc123");
  });
});
