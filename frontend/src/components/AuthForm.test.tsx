import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthForm } from "./AuthForm";

const replace = vi.fn();
let next: string | null = null;
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => ({ get: () => next }),
}));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
  next = null;
});

describe("AuthForm", () => {
  it("posts credentials with cookies enabled and follows a safe next=", async () => {
    next = "/terminal";
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ role: "user" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    wrap(<AuthForm mode="login" />);
    fireEvent.change(document.querySelector('input[name="email"]')!, { target: { value: "a@tr-platform.io" } });
    fireEvent.change(document.querySelector('input[name="password"]')!, { target: { value: "correct horse battery" } });
    fireEvent.submit(screen.getByRole("form", { name: "sign in" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/terminal"));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/auth\/login$/);
    expect(init.credentials).toBe("include");
    expect(init.method).toBe("POST");
  });

  it("never follows an off-site next=", async () => {
    next = "https://evil.example/phish";
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    wrap(<AuthForm mode="login" />);
    fireEvent.change(document.querySelector('input[name="email"]')!, { target: { value: "a@tr-platform.io" } });
    fireEvent.change(document.querySelector('input[name="password"]')!, { target: { value: "correct horse battery" } });
    fireEvent.submit(screen.getByRole("form", { name: "sign in" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("shows the API's error and does not navigate", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "invalid email or password" }), { status: 401 })),
    );
    wrap(<AuthForm mode="login" />);
    fireEvent.change(document.querySelector('input[name="email"]')!, { target: { value: "a@tr-platform.io" } });
    fireEvent.change(document.querySelector('input[name="password"]')!, { target: { value: "wrong wrong wrong" } });
    fireEvent.submit(screen.getByRole("form", { name: "sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid email or password");
    expect(replace).not.toHaveBeenCalled();
  });

  it("rejects a short password on registration before calling the API", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    wrap(<AuthForm mode="register" />);
    fireEvent.change(document.querySelector('input[name="email"]')!, { target: { value: "a@tr-platform.io" } });
    fireEvent.change(document.querySelector('input[name="password"]')!, { target: { value: "short" } });
    fireEvent.submit(screen.getByRole("form", { name: "create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 10");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("password visibility", () => {
  it("toggles between hidden and visible without changing the value", () => {
    wrap(<AuthForm mode="login" />);
    const field = document.querySelector('input[name="password"]')! as HTMLInputElement;
    fireEvent.change(field, { target: { value: "correct horse battery" } });
    expect(field.type).toBe("password");

    fireEvent.click(screen.getByRole("button", { name: "show password" }));
    expect((document.querySelector('input[name="password"]') as HTMLInputElement).type).toBe("text");
    expect((document.querySelector('input[name="password"]') as HTMLInputElement).value).toBe(
      "correct horse battery",
    );

    fireEvent.click(screen.getByRole("button", { name: "hide password" }));
    expect((document.querySelector('input[name="password"]') as HTMLInputElement).type).toBe(
      "password",
    );
  });
});
