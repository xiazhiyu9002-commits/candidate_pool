import type { ReactNode } from "react";


export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}


export function LoadingButton({
  loading = false,
  disabled = false,
  children,
  onClick,
  type = "button",
  className = "detail-button",
}: {
  loading?: boolean;
  disabled?: boolean;
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  className?: string;
}) {
  return (
    <button
      type={type}
      className={className}
      disabled={disabled || loading}
      onClick={onClick}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}


export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: lines }).map((_, index) => (
        <div key={index} className="skeleton__line" />
      ))}
    </div>
  );
}


export function LongTaskProgress({
  message,
  percent,
  error,
}: {
  message?: string | null;
  percent?: number | null;
  error?: string | null;
}) {
  if (!message && error == null && percent == null) return null;
  return (
    <div className="long-task" role="status">
      {message && (
        <span className="long-task__message">
          <Spinner />
          {message}
        </span>
      )}
      {percent != null && <progress max={100} value={percent} />}
      {error && <span className="long-task__error">{error}</span>}
    </div>
  );
}
