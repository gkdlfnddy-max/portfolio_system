import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { PerspectiveAnalysis } from "@/components/PerspectiveAnalysis";

export const dynamic = "force-dynamic";

// 관점 분석(6축/관점/후보) 조회 전용 화면 — Track C.
//   · 실 DB 기반(mock chart 0): 데이터 없는 축은 "미연동"으로 정직 표기.
//   · 자동 주문/적용 0: 사용자 승인 전 policy 미반영.
//   · RBAC 는 API 라우트(requireAccountAccessAndUnlocked)에서 강제 — 타계좌 403 / 미로그인 401.
export default function AccountAnalysisPage({ params }: { params: { id: string } }) {
  const index = parseInt(params.id, 10);
  if (!Number.isInteger(index) || index < 1) notFound();

  return (
    <div className="max-w-3xl mx-auto px-5 py-10 space-y-6">
      <Link
        href={`/accounts/${index}`}
        className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary"
      >
        <ArrowLeft className="w-4 h-4" /> 계좌로 돌아가기
      </Link>
      <div>
        <h1 className="text-2xl font-bold text-neutral-900">관점 분석</h1>
        <p className="text-sm text-neutral-500 mt-1">
          내 투자 목적·관점에 따라 같은 데이터가 어떻게 다르게 해석되는지, 관점별 후보(A/B/C)와
          하락 징후 6축을 한눈에 봅니다. 모두 <b>조회 전용</b>입니다.
        </p>
      </div>
      <PerspectiveAnalysis accountId={index} />
    </div>
  );
}
