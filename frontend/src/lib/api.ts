import { apiUrl } from "./config";
import type { Health, User } from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * The backend answers every failure as
 * `{"error": {code, detail, request_id}}`. Older shapes and FastAPI's raw
 * `detail` are still accepted so a version skew degrades to a worse message
 * rather than an unhandled crash.
 */
async function describe(res: Response, fallback: string): Promise<[string, string | undefined]> {
  try {
    const body = (await res.json()) as {
      error?: { detail?: string; request_id?: string };
      detail?: unknown;
    };
    if (body.error?.detail) return [body.error.detail, body.error.request_id];
    if (typeof body.detail === "string") return [body.detail, undefined];
    if (Array.isArray(body.detail)) {
      const first = body.detail[0] as { msg?: string } | undefined;
      if (first?.msg) return [first.msg, undefined];
    }
  } catch {
    /* not JSON */
  }
  return [fallback, undefined];
}

export const CSRF_COOKIE = "tr_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

/**
 * Read the CSRF token the API set beside the session cookie.
 *
 * The session cookie is HttpOnly and unreadable by design; this one is
 * deliberately readable so the page can echo it in a header, which is what
 * proves the request came from our own origin.
 */
export function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const match = document.cookie.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : undefined;
}

const STATE_CHANGING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

// credentials: "include" carries the HttpOnly session cookie to the API.
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const token = STATE_CHANGING.has(method) ? csrfToken() : undefined;
  const res = await fetch(apiUrl(path), {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(token ? { [CSRF_HEADER]: token } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    const [detail, requestId] = await describe(res, `${path} -> ${res.status}`);
    throw new ApiError(res.status, detail, requestId);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function postJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/**
 * Paths are relative to the API root (`NEXT_PUBLIC_API_URL`), which behind
 * nginx is `/api`. So `/v1/auth/me` is `/api/v1/auth/me` in the browser and
 * `http://127.0.0.1:8000/v1/auth/me` against the backend directly.
 *
 * `/health` deliberately stays outside the version prefix: it is an
 * infrastructure contract shared with the Compose healthcheck and nginx.
 */
export function fetchHealth(): Promise<Health> {
  return request<Health>("/health");
}

export function fetchMe(): Promise<User> {
  return request<User>("/v1/auth/me");
}

export function login(email: string, password: string): Promise<User> {
  return postJson<User>("/v1/auth/login", { email, password });
}

export function register(email: string, password: string): Promise<User> {
  return postJson<User>("/v1/auth/register", { email, password });
}

export function logout(): Promise<void> {
  return postJson<void>("/v1/auth/logout");
}
