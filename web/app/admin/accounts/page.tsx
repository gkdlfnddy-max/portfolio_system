import { AdminAccountsOverview } from "@/components/admin/AdminAccountsOverview";
import { LayoutGrid } from "lucide-react";

export const dynamic = "force-dynamic";

// Track K — 관리자 전체 계좌 overview. /admin (B 콘솔)과 분리된 라우트.
// 비관리자 접근 차단은 **서버(/api/admin/dashboard 의 requireAdmin)** 가 한다.
// 이 화면은 API 가 403 이면 "관리자 전용"을 표시한다(프론트 숨김에 의존하지 않음).
export default function AdminAccountsPage() {
  return (
    <div className="max-w-6xl mx-auto px-5 py-10 space-y-8">
      <header className="space-y-1">
        <h1 className="text-2xl font-bold text-neutral-900 flex items-center gap-2">
          <LayoutGrid className="w-6 h-6 text-primary" /> 전체 계좌 현황
        </h1>
        <p className="text-sm text-neutral-400">
          모든 계좌의 동기화 상태·총자산·drift 를 한눈에 봅니다. 접근 권한은 서버에서 검증됩니다(관리자 전용).
        </p>
      </header>

      <AdminAccountsOverview />
    </div>
  );
}
