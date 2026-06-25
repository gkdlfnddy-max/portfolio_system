import { cn } from "@/lib/utils";

// 로딩/대기 상태용 스켈레톤 (벤치마크: Vercel/Linear 대시보드 로딩 패턴).
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded bg-neutral-100", className)} />;
}

export function SkeletonRow() {
  return (
    <div className="flex items-center gap-3 py-2">
      <Skeleton className="h-4 w-24" />
      <Skeleton className="h-4 flex-1" />
      <Skeleton className="h-4 w-16" />
    </div>
  );
}
