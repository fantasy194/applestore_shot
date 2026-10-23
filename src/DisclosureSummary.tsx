import type { ComponentPropsWithoutRef } from "react";
import { ChevronRight } from "lucide-react";

export function DisclosureSummary({
  children,
  className = "",
  ...props
}: ComponentPropsWithoutRef<"summary">) {
  return (
    <summary {...props} className={`disclosure-summary ${className}`.trim()}>
      {children}
      <ChevronRight
        size={16}
        className="disclosure-chevron"
        aria-hidden="true"
      />
    </summary>
  );
}
