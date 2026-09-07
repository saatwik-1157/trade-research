"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useMe } from "@/hooks/useMe";
import { ApiError } from "@/lib/api";
import { type Role, roleAtLeast } from "@/lib/roles";

/**
 * Gate a page on a session and a minimum role. This is a convenience for the
 * UI; the API enforces the same table server-side, so nothing here is a
 * security boundary on its own.
 */
export function RouteGuard({ minRole, children }: { minRole: Role; children: React.ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();

  const signedOut = me.isError && me.error instanceof ApiError && me.error.status === 401;

  useEffect(() => {
    if (signedOut) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [signedOut, router, pathname]);

  if (me.isPending) {
    return (
      <p className="text-body text-muted" role="status">
        Checking session…
      </p>
    );
  }
  if (signedOut) {
    return (
      <p className="text-body text-muted" role="status">
        Redirecting to sign in…
      </p>
    );
  }
  if (me.isError) {
    return (
      <p className="text-body text-critical" role="alert">
        Cannot verify the session: {me.error.message}
      </p>
    );
  }
  if (!roleAtLeast(me.data.role, minRole)) {
    return (
      <div role="alert" className="rounded border border-critical/50 bg-surface p-4 text-sm">
        <p className="font-medium text-critical">Not permitted</p>
        <p className="mt-1 text-body text-muted">
          This page needs the <span className="font-mono text-ink-2">{minRole}</span> role; you
          are signed in as <span className="font-mono text-ink-2">{me.data.role}</span>. An admin
          can change roles.
        </p>
      </div>
    );
  }
  return <>{children}</>;
}
