import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";

export interface FieldProps {
  label?: ReactNode;
  htmlFor?: string;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  children: ReactNode;
}

export function Field({ label, htmlFor, hint, error, required, children }: FieldProps) {
  return (
    <div className="ui-field">
      {label && (
        <label className="ui-field__label" htmlFor={htmlFor}>
          {label}
          {required && <span aria-hidden="true"> *</span>}
        </label>
      )}
      {children}
      {hint && !error && <span className="ui-field__hint">{hint}</span>}
      {error && (
        <span className="ui-field__error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

function join(...parts: Array<string | undefined | null>): string {
  return parts.filter(Boolean).join(" ");
}

export function Input({
  error,
  className,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { error?: boolean }) {
  return (
    <input
      className={join("ui-input", error ? "ui-input--invalid" : undefined, className)}
      {...rest}
    />
  );
}

export function Select({
  error,
  className,
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement> & { error?: boolean }) {
  return (
    <select
      className={join("ui-select", error ? "ui-select--invalid" : undefined, className)}
      {...rest}
    >
      {children}
    </select>
  );
}

export function Textarea({
  error,
  className,
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { error?: boolean }) {
  return (
    <textarea
      className={join("ui-textarea", error ? "ui-textarea--invalid" : undefined, className)}
      {...rest}
    />
  );
}
