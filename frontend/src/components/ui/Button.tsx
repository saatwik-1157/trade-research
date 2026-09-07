import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "buy" | "sell" | "danger";

/**
 * `primary` and `danger` carry a DARK label on their fill, which looks like a
 * style choice and is not one. White on this blue measures 3.64:1 and white on
 * this red 3.87:1 -- both under the 4.5 a button label needs. The same two
 * fills with the page colour on top measure 5.34 and 5.02. The alternative was
 * darkening the accent, which would have dropped it below 4.5 everywhere it is
 * used as a link.
 */
const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-accent text-plane hover:brightness-110 active:brightness-95",
  secondary:
    "border border-line bg-surface-2 text-ink-2 hover:border-baseline hover:text-ink active:brightness-95",
  ghost: "text-muted hover:bg-surface-2 hover:text-ink",
  buy: "border border-good text-good hover:bg-good/10 active:bg-good/20",
  sell: "border border-critical text-critical hover:bg-critical/10 active:bg-critical/20",
  danger: "bg-critical text-plane hover:brightness-110 active:brightness-95",
};

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "sm" | "md";
}

export function Button({
  variant = "secondary",
  size = "md",
  className = "",
  type = "button",
  ...rest
}: Props) {
  const pad = size === "sm" ? "px-2.5 py-1 text-body" : "px-3 py-1.5 text-sm";
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center gap-1.5 rounded font-medium transition-[color,background-color,border-color,filter] duration-150 disabled:cursor-not-allowed disabled:opacity-50 ${pad} ${VARIANTS[variant]} ${className}`}
      {...rest}
    />
  );
}
