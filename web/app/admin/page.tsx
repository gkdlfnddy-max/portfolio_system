import { AdminUsers } from "@/components/admin/AdminUsers";
import { AdminAuthEvents } from "@/components/admin/AdminAuthEvents";
import { ShieldAlert } from "lucide-react";

export const dynamic = "force-dynamic";

// 관리자 콘솔. 비관리자 접근 차단은 **서버(A 소유 authz)** 가 한다.
// 이 화면의 각 섹션은 admin API 가 403 이면 "권한 없음"을 표시한다(프론트 숨김에 의존하지 않음).
export default function AdminPage() {
  return (
    <div className="max-w-5xl mx-auto px-5 py-10 space-y-8">
      <header className="space-y-1">
        <h1 className="text-2xl font-bold text-neutral-900 flex items-center gap-2">
          <ShieldAlert className="w-6 h-6 text-primary" /> 관리자 콘솔
        </h1>
        <p className="text-sm text-neutral-400">
          사용자·계좌 권한·비밀번호 초기화·인증 이벤트를 관리합니다. 접근 권한은 서버에서 검증됩니다.
        </p>
      </header>

      <AdminUsers />
      <AdminAuthEvents />
    </div>
  );
}
