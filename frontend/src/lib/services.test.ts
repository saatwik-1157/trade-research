import { describe, expect, it } from "vitest";
import { botService, marketService, orderService, positionService } from "./services";

/**
 * The service layer's contract: a domain whose backend does not exist returns
 * an explicit refusal naming its level. It must never return empty data,
 * because "no positions" and "no positions endpoint" must not look the same.
 */
describe("services that are not built yet", () => {
  it("refuse with a level rather than returning empty data", async () => {
    // `orderService.list` left this set at L19, `positionService.list` at L21
    // and `botService.list` at L22: all three are bound to real endpoints now,
    // so calling them here would be a network request rather than a contract
    // check. Their behaviour is tested against the backend instead.
    //
    // `marketService.quotes` is the last one still genuinely unbuilt, and it
    // is the point of this test: a domain with no backend must refuse with a
    // level, never return empty data.
    for (const call of [marketService.quotes()]) {
      const result = await call;
      expect(result.available).toBe(false);
      if (!result.available) {
        expect(result.level).toBeGreaterThan(0);
        expect(result.reason.length).toBeGreaterThan(10);
      }
    }
  });

  it("the bot manager is bound and exposes no route that trades", () => {
    // CHANGED AT L22. `botService` is a control plane: list, inspect,
    // preflight, disable, enable. There is no method by which it could
    // submit an order, and preflight deliberately starts nothing.
    expect(Object.keys(botService).sort()).toEqual([
      "disable",
      "enable",
      "get",
      "list",
      "preflight",
    ]);
  });

  it("position management is bound and cannot silently remove a stop", () => {
    // CHANGED AT L21. `protect` takes the levels to SET; there is no argument
    // by which it could clear one, so removing protection is not something a
    // caller can do by accident from here.
    expect(typeof positionService.close).toBe("function");
    expect(typeof positionService.protect).toBe("function");
    expect(positionService.protect.length).toBe(2);
  });

  it("order submission requires an idempotency key in its signature", () => {
    // CHANGED AT L19: submission is built. What replaces "there is no submit
    // path" is that the path cannot be called without the thing that stops a
    // retry from becoming a second order — the key is a required positional
    // argument, not an option a caller can forget.
    expect(orderService.submit.length).toBe(2);
  });
});
