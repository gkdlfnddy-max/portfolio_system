import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import UserViewsForm from "@/components/UserViewsForm";
import InvestorObjectiveForm from "@/components/InvestorObjectiveForm";

export const dynamic = "force-dynamic";

// 사용자(CEO) 투자 견해 입력 — 1급 입력. 계좌별 격리. 자동 적용 없음(저장만).
// RBAC 는 API 라우트(requireAccountAccess)에서 강제 — 권한 없는 계좌는 데이터 403.
export default function AccountViewsPage({ params }: { params: { id: string } }) {
  const index = parseInt(params.id, 10);
  if (!Number.isInteger(index) || index < 1) notFound();

  return (
    <div className="max-w-3xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${index}`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 계좌로 돌아가기
      </Link>
      <div>
        <h1 className="text-2xl font-bold text-neutral-900">내 투자 목적·견해</h1>
        <p className="text-sm text-neutral-500 mt-1">
          먼저 <b>투자 목적·성향</b>(“최선”의 기준)을 정하고, 분야별 <b>견해</b>를 적으면
          포트폴리오 판단의 1급 입력이 됩니다. 모두 저장만 되며 자동 적용되지 않습니다.
        </p>
      </div>
      <InvestorObjectiveForm accountId={index} />
      <UserViewsForm accountId={index} />
    </div>
  );
}
