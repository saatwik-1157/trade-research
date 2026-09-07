import { describe, expect, it } from "vitest";
import { apiBaseUrl, apiUrl } from "./config";

describe("apiBaseUrl", () => {
  it("defaults to the local API when unset", () => {
    expect(apiBaseUrl({})).toBe("http://127.0.0.1:8000");
  });

  it("uses NEXT_PUBLIC_API_URL and strips trailing slashes", () => {
    expect(apiBaseUrl({ NEXT_PUBLIC_API_URL: "https://x.example/api///" })).toBe(
      "https://x.example/api",
    );
  });

  it("treats a blank value as unset", () => {
    expect(apiBaseUrl({ NEXT_PUBLIC_API_URL: "   " })).toBe("http://127.0.0.1:8000");
  });
});

describe("apiUrl", () => {
  it("joins without doubling slashes", () => {
    expect(apiUrl("/health", { NEXT_PUBLIC_API_URL: "/api/" })).toBe("/api/health");
    expect(apiUrl("health/ready", {})).toBe("http://127.0.0.1:8000/health/ready");
  });
});
