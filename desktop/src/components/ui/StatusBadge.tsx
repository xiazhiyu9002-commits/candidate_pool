import type { ReactNode } from "react";

export type StatusTone = "neutral" | "primary" | "success" | "warning" | "danger" | "info";

const TONE_CLASS: Record<StatusTone, string> = {
  neutral: "badge",
  primary: "badge badge--ai",
  success: "badge badge--success",
  warning: "badge badge--manual",
  danger: "badge badge--stale",
  info: "badge badge--ai",
};

export function StatusBadge({
  tone = "neutral",
  children,
}: {
  tone?: StatusTone;
  children: ReactNode;
}) {
  return (
    <span className={TONE_CLASS[tone]}>
      <span className="dot" aria-hidden="true" />
      {children}
    </span>
  );
}
