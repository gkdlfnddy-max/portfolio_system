import Link from "next/link";
import { notFound } from "next/navigation";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { ArrowLeft, Wallet, ArrowRight, ShieldCheck, CalendarClock } from "lucide-react";
import { AccountSync } from "@/components/AccountSync";
import { BrokerBadge } from "@/components/BrokerBadge";
import { BrokerTestPanel } from "@/components/BrokerTestPanel";
import { DailyReviewCard } from "@/components/DailyReviewCard";
import { PortfolioTrendCard } from "@/components/PortfolioTrendCard";
import { AdviceHistoryCard } from "@/components/AdviceHistoryCard";
import { GrowthHistoryCard } from "@/components/GrowthHistoryCard";
import { getAccountView } from "@/lib/server/portfolioDb";

export const dynamic = "force-dynamic";

function ModeBadge({ mode }: { mode: string | null }) {
  const map: Record<string, { label: string; cls: string }> = {
    mock: { label: "데모(mock)", cls: "bg-neutral-100 text-neutral-600" },
    paper: { label: "모의투자", cls: "bg-primary-50 text-primary-700" },
    live: { label: "실전", cls: "bg-error/10 text-error" },
  };
  const m = map[mode ?? "paper"] ?? map.paper;
  return <Badge className={m.cls}>{m.label}</Badge>;
}

export default async function AccountDetailPage({ params }: { params: { id: string } }) {
  const index = parseInt(params.id, 10);
  if (!Number.isInteger(index) || index < 1) notFound();
  const view = await getAccountView(index); // DB 조회
  if (!view) notFound();

  return (
    <div className="max-w-3xl mx-auto px-5 py-10 space-y-6">
      <Link href="/" className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 관리 중인 계좌
      </Link>

      {/* 계좌 헤더 (DB) */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h1 className="text-2xl font-bold text-neutral-900">{view.alias}</h1>
            <BrokerBadge broker={view.broker} />
            <ModeBadge mode={view.mode} />
          </div>
          <div className="flex items-center gap-2 text-sm text-neutral-500 mt-1">
            <Wallet className="w-4 h-4" /> {view.broker === "kiwoom" ? "키움증권" : "한국투자증권"} · 계좌 {view.account_no_masked ?? "—"}
          </div>
        </div>
        <Badge className={view.sync_status === "ok" ? "bg-success/10 text-success" : "bg-warning/10 text-warning"}>
          {view.sync_status === "ok" ? "동기화됨" : view.sync_status === "error" ? "동기화 오류" : "미동기화"}
        </Badge>
      </div>

      {/* 연결 준비 + 실잔고 — 전부 DB 스냅샷에서 (클라이언트가 DB 조회) */}
      <AccountSync accountId={index} />

      {/* 증권사 연결 테스트 (stage별) — KIS/키움 공통. 주문 없음. 키 미설정이면 안전 차단 안내 */}
      <BrokerTestPanel accountId={index} broker={view.broker} />

      {/* 오늘의 포트폴리오 점검 (실시간 봇 아님 · 관망도 정상) */}
      <DailyReviewCard accountId={index} />

      {/* 일별 추이 — 총자산·자산군 비중·노출(net/gross/테마/hedge)·drift·종목 (History/Dashboard) */}
      <PortfolioTrendCard accountId={index} />

      {/* 조언 적용/무시 이력 — 대전제·중전제 조언에 대한 사람 결정 타임라인 */}
      <AdviceHistoryCard accountId={index} />

      {/* 성장 이력 — 자료조사 근거·교훈 후보·승격된 공통 교훈(익명)·회귀테스트 */}
      <GrowthHistoryCard accountId={index} />

      {/* 다가오는 일정·이벤트 — 데이터 소스 미연동(정직한 빈 상태) */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <CalendarClock className="w-5 h-5 text-primary" /> 다가오는 일정 · 이벤트
          </CardTitle>
        </CardHeader>
        <CardBody>
          <div className="text-sm text-neutral-500 text-center py-6">
            보유 종목의 <b>실적발표 · 배당락 · 공시 · 뉴스</b>를 표시할 예정입니다.
            <br />
            <span className="text-xs text-neutral-400">
              (DART · 뉴스 데이터 소스 연동 후 자동 표시 — 현재 미연동)
            </span>
          </div>
        </CardBody>
      </Card>

      {/* 액션 (계좌 종속) — 대전제 → 3안 확정 → 소전제 → 의사결정 */}
      <div className="flex flex-wrap gap-3">
        <Link href={`/accounts/${index}/strategy`}>
          <Button size="lg">
            1. 운용 전략 (대·중전제)
          </Button>
        </Link>
        <Link href={`/accounts/${index}/allocation`}>
          <Button size="lg" variant="outline">
            2. 목표 포트폴리오 확정 (3안)
          </Button>
        </Link>
        <Link href={`/accounts/${index}/universe`}>
          <Button size="lg" variant="outline">
            3. 종목 유니버스 (소전제)
          </Button>
        </Link>
        <Link href={`/accounts/${index}/selection`}>
          <Button size="lg" variant="outline">
            3-1. 종목·ETF 선정 (세부)
          </Button>
        </Link>
        <Link href={`/accounts/${index}/portfolio`}>
          <Button size="lg" variant="outline">
            4. 의사결정 <ArrowRight className="w-4 h-4" />
          </Button>
        </Link>
        <Link href={`/accounts/${index}/views`}>
          <Button size="lg" variant="outline">
            내 투자 견해
          </Button>
        </Link>
        <Link href={`/accounts/${index}/analysis`}>
          <Button size="lg" variant="outline">
            관점 분석 (6축 · A/B/C)
          </Button>
        </Link>
        <Link href={`/accounts/${index}/history`}>
          <Button size="lg" variant="ghost">
            지역·채권 변화 이력
          </Button>
        </Link>
      </div>

      <p className="text-xs text-neutral-400 flex items-center gap-1">
        <ShieldCheck className="w-3.5 h-3.5" /> 주문은 리스크 점검 + 사장님 승인 후에만 실행됩니다.
      </p>
    </div>
  );
}
