import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { SelectionFlow } from "@/components/SelectionFlow";

export const dynamic = "force-dynamic";

// 종목/ETF 선정 화면 (Step 1–8).
// 서버 컴포넌트는 라우팅·헤더만 담당하고, 실제 데이터는 클라이언트(SelectionFlow)가
// RBAC 라우트(/api/accounts/[id]/selection)로 조회한다. 데이터 없으면 "데이터 없음/미연동"(mock 0).
export default function SelectionPage({ params }: { params: { id: string } }) {
  const id = parseInt(params.id, 10);
  if (!Number.isInteger(id) || id < 1) notFound();

  return (
    <div className="max-w-5xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${id}/allocation`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 목표 포트폴리오 확정 (3안)
      </Link>
      <SelectionFlow accountId={id} />
    </div>
  );
}
