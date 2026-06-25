"use client";

import { cn } from "@/lib/utils";
import { createContext, useContext, useState, ReactNode } from "react";

type TabsContext = { value: string; setValue: (v: string) => void };
const Ctx = createContext<TabsContext | null>(null);

export function Tabs({
  defaultValue,
  children,
  className,
}: {
  defaultValue: string;
  children: ReactNode;
  className?: string;
}) {
  const [value, setValue] = useState(defaultValue);
  return (
    <Ctx.Provider value={{ value, setValue }}>
      <div className={cn("w-full", className)}>{children}</div>
    </Ctx.Provider>
  );
}

export function TabsList({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-wrap gap-1 border-b border-neutral-200 mb-4", className)}>
      {children}
    </div>
  );
}

export function TabsTrigger({ value, children }: { value: string; children: ReactNode }) {
  const ctx = useContext(Ctx)!;
  const active = ctx.value === value;
  return (
    <button
      onClick={() => ctx.setValue(value)}
      className={cn(
        "px-4 py-2 text-sm font-medium border-b-2 -mb-px transition",
        active
          ? "border-primary text-primary"
          : "border-transparent text-neutral-400 hover:text-neutral-700",
      )}
    >
      {children}
    </button>
  );
}

export function TabsContent({ value, children }: { value: string; children: ReactNode }) {
  const ctx = useContext(Ctx)!;
  if (ctx.value !== value) return null;
  return <div>{children}</div>;
}
