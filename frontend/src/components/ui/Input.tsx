import type { InputHTMLAttributes, SelectHTMLAttributes } from "react";

/**
 * `bg-surface-2` is what makes an input look like an input. It measured
 * 1.09:1 against the panel behind it, which is to say the field was invisible
 * and only the border said anything; at the lifted 1.35:1 the well reads on
 * its own. The focus ring comes from the one rule in `globals.css`.
 */
const base =
  "w-full rounded border border-line bg-surface-2 px-2 py-1.5 text-sm text-ink transition-colors placeholder:text-muted hover:border-baseline disabled:cursor-not-allowed disabled:opacity-60";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-mini font-semibold uppercase tracking-wider text-muted">
      {label}
      <span className="mt-1 block">{children}</span>
      {hint && <span className="mt-1 block normal-case text-micro font-normal text-muted">{hint}</span>}
    </label>
  );
}

export function Input({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${base} ${className}`} {...rest} />;
}

export function Select({ className = "", ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`${base} ${className}`} {...rest} />;
}
