import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCurrency(value: number) {
  return value.toLocaleString("ko-KR") + "원";
}

export function statusToKo(status: string) {
  const map: Record<string, string> = {
    received: "접수됨",
    generating: "작성 중",
    reviewing: "발행 전 검수",
    scheduled: "발행 예약",
    drafted: "임시저장",
    published: "발행 완료",
    failed: "재시도 필요",
    done: "완료",
  };
  return map[status] ?? status;
}

export function statusColor(status: string) {
  const map: Record<string, string> = {
    received: "bg-neutral-100 text-neutral-700",
    generating: "bg-info/10 text-info",
    reviewing: "bg-warning/10 text-warning",
    scheduled: "bg-primary-50 text-primary-700",
    drafted: "bg-neutral-100 text-neutral-700",
    published: "bg-success/10 text-success",
    failed: "bg-error/10 text-error",
    done: "bg-success/10 text-success",
  };
  return map[status] ?? "bg-neutral-100 text-neutral-700";
}
