"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { ME_KEY } from "@/hooks/useMe";
import { ApiError, login, register } from "@/lib/api";

const MIN_PASSWORD = 10;

const field =
  "mt-1 w-full rounded border border-line bg-surface-2 px-2 py-1.5 text-sm text-ink transition-colors placeholder:text-muted hover:border-baseline";

function safeNext(raw: string | null): string {
  // Only same-origin paths; never an absolute URL from the query string.
  return raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
}

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reveal, setReveal] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (mode === "register" && password.length < MIN_PASSWORD) {
      setError(`Password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    setBusy(true);
    try {
      await (mode === "login" ? login(email, password) : register(email, password));
      await qc.invalidateQueries({ queryKey: ME_KEY });
      router.replace(safeNext(params.get("next")));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the API.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      aria-label={mode === "login" ? "sign in" : "create account"}
      className="w-full max-w-sm rounded-md border border-line bg-surface p-5 shadow-raised"
    >
      <div className="mb-4 flex items-center gap-2">
        <span className="h-2.5 w-2.5 rounded-sm bg-accent" aria-hidden />
        <span className="text-sm font-semibold">trade-research</span>
      </div>
      <h1 className="text-lg font-semibold">{mode === "login" ? "Sign in" : "Create account"}</h1>
      <p className="mb-4 text-body text-muted">
        {mode === "login"
          ? "Sessions are server-side and expire after 12 hours."
          : "New accounts start as USER. An admin grants TRADER."}
      </p>
      <label className="mb-3 block text-mini uppercase tracking-wider text-muted">
        Email
        <input
          className={field}
          type="email"
          name="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label className="mb-4 block text-mini uppercase tracking-wider text-muted">
        Password
        <span className="relative block">
          <input
            className={`${field} pr-14`}
            type={reveal ? "text" : "password"}
            name="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={mode === "register" ? MIN_PASSWORD : undefined}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button
            type="button"
            onClick={() => setReveal((v) => !v)}
            aria-pressed={reveal}
            aria-label={reveal ? "hide password" : "show password"}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-mini uppercase text-muted hover:text-ink"
          >
            {reveal ? "Hide" : "Show"}
          </button>
        </span>
      </label>
      {error && (
        <p
          role="alert"
          className="mb-3 rounded border border-critical/40 bg-critical/10 px-2 py-1.5 text-body text-critical"
        >
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="w-full rounded bg-accent py-1.5 text-sm font-semibold text-plane transition-[filter] hover:brightness-110 disabled:opacity-60"
      >
        {busy ? "…" : mode === "login" ? "Sign in" : "Create account"}
      </button>
      <p className="mt-4 text-body text-muted">
        {mode === "login" ? (
          <>
            No account?{" "}
            <Link href="/register" className="text-accent hover:underline">
              Create one
            </Link>
          </>
        ) : (
          <>
            Have an account?{" "}
            <Link href="/login" className="text-accent hover:underline">
              Sign in
            </Link>
          </>
        )}
      </p>
    </form>
  );
}
