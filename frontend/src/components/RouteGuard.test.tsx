import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouteGuard } from "./RouteGuard";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/terminal",
}));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function stubMe(status: number, body?: object) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body ? JSON.stringify(body) : "{}", { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
});

const user = (role: string) => ({
  id: "u1",
  email: "alice@tr-platform.io",
  role,
  is_active: true,
  created_at: "2026-09-02T00:00:00",
});

describe("RouteGuard", () => {
  it("redirects to /login with next= when there is no session", async () => {
    stubMe(401, { detail: "not signed in" });
    wrap(
      <RouteGuard minRole="user">
        <p>secret</p>
      </RouteGuard>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login?next=%2Fterminal"));
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("refuses a USER on a TRADER page and says which role is needed", async () => {
    stubMe(200, user("user"));
    wrap(
      <RouteGuard minRole="trader">
        <p>secret</p>
      </RouteGuard>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("trader");
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("renders children for a sufficient role", async () => {
    stubMe(200, user("admin"));
    wrap(
      <RouteGuard minRole="trader">
        <p>secret</p>
      </RouteGuard>,
    );
    expect(await screen.findByText("secret")).toBeInTheDocument();
  });
});
