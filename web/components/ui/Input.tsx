import { cn } from "@/lib/utils";
import { InputHTMLAttributes, TextareaHTMLAttributes, forwardRef } from "react";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        className={cn(
          "w-full px-3 py-2 rounded-xl border border-neutral-200 bg-white text-sm",
          "focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary",
          "placeholder:text-neutral-400",
          className,
        )}
        {...props}
      />
    );
  },
);

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...props }, ref) {
    return (
      <textarea
        ref={ref}
        className={cn(
          "w-full px-3 py-2 rounded-xl border border-neutral-200 bg-white text-sm",
          "focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary",
          "placeholder:text-neutral-400 min-h-[100px]",
          className,
        )}
        {...props}
      />
    );
  },
);
