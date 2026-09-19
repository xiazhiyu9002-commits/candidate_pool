import type { HTMLAttributes } from "react";

export function Toolbar({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={["ui-toolbar", className].filter(Boolean).join(" ")} {...rest}>
      {children}
    </div>
  );
}
