"use client";

import { cn } from "@/lib/utils";
import { Check } from "lucide-react";

export function Checkbox({
  checked,
  onChange,
  label,
  className,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  className?: string;
}) {
  return (
    <label className={cn("inline-flex items-center gap-2 cursor-pointer select-none", className)}>
      <span
        onClick={() => onChange(!checked)}
        className={cn(
          "w-5 h-5 rounded-md border flex items-center justify-center transition",
          checked ? "bg-primary border-primary" : "bg-white border-neutral-300",
        )}
      >
        {checked && <Check className="w-3.5 h-3.5 text-white" />}
      </span>
      <span className="text-sm text-neutral-700">{label}</span>
    </label>
  );
}

export function ChoicePill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "px-3 py-1.5 rounded-full text-sm border transition",
        active
          ? "bg-primary text-white border-primary"
          : "bg-white text-neutral-700 border-neutral-200 hover:bg-neutral-50",
      )}
    >
      {children}
    </button>
  );
}
