/**
 * Frontend configuration.
 *
 * The browser bundle sees only NEXT_PUBLIC_* variables. No broker credential,
 * database URL or secret is ever read here, and nothing in this module should
 * grow a field that would hold one.
 */
export type PublicEnv = Partial<Record<"NEXT_PUBLIC_API_URL", string | undefined>>;

const DEFAULT_API_URL = "http://127.0.0.1:8000";

/** Base URL of the backend API, without a trailing slash. */
export function apiBaseUrl(env: PublicEnv = process.env as PublicEnv): string {
  const raw = env.NEXT_PUBLIC_API_URL?.trim();
  const url = raw && raw.length > 0 ? raw : DEFAULT_API_URL;
  return url.replace(/\/+$/, "");
}

/** Join a path onto the API base URL. */
export function apiUrl(path: string, env?: PublicEnv): string {
  const base = apiBaseUrl(env);
  return `${base}/${path.replace(/^\/+/, "")}`;
}

/**
 * The WebSocket URL for a backend path, derived from the one API base URL.
 *
 * Derived rather than configured separately: a second variable would be a
 * second thing to get wrong, and a socket pointing at a different host from the
 * REST calls would authenticate against a session cookie it was never sent.
 * http -> ws and https -> wss, and nothing else about the URL changes.
 */
export function wsUrl(path: string, env?: PublicEnv): string {
  return apiUrl(path, env).replace(/^http/, "ws");
}
