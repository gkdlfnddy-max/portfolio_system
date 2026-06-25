import Link from "next/link";
import { Card, CardBody } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import {
  Sparkles, ShieldCheck, Lock, Plus,
  KeyRound, Repeat, ClipboardCheck, Building2, ListChecks,
} from "lucide-react";
import { MANAGER } from "@/lib/portfolio/manager";
import { HomeAccountList } from "@/components/HomeAccountList";

export const dynamic = "force-dynamic";

export default function HomePage() {
  return (
    <div className="max-w-5xl mx-auto px-5 py-10 space-y-14">
      {/* Hero */}
      <section className="text-center py-10">
        <div className="inline-flex items-center gap-1 bg-primary-50 text-primary-700 text-xs px-3 py-1 rounded-full mb-6">
          <Sparkles className="w-3.5 h-3.5" />
          포트폴리오 관리자 (MVP)
        </div>
        <h1 className="text-4xl md:text-5xl font-bold text-neutral-900 leading-tight">
          계좌 관리,
          <br />
          <span className="text-primary">포트폴리오 관리자에게 맡기세요.</span>
        </h1>
        <p className="text-neutral-700 mt-5 max-w-2xl mx-auto">
          한국투자증권 또는 키움증권 계좌를 연결하면, AI 포트폴리오 관리자가 투자 컨셉에 맞춰
          리밸런싱을 <b>제안</b>합니다. 주문은 <b>리스크 점검 + 사장님 승인</b> 후에만 나갑니다.
        </p>
        <div className="flex flex-wrap items-center justify-center gap-3 mt-8">
          <Link href="/accounts/new">
            <Button size="lg">
              <Plus className="w-4 h-4" /> 계좌 연결하기
            </Button>
          </Link>
          <Badge className="bg-warning/10 text-warning text-sm px-3 py-1">
            <Lock className="w-3.5 h-3.5 mr-1" /> 모의투자 우선 · 무승인 자동매매 없음
          </Badge>
        </div>
        <p className="text-xs text-neutral-400 mt-3">
          “계좌 연결하기”에서 <b>한국투자증권</b> 또는 <b>키움증권</b>을 선택해 연결할 수 있어요.
        </p>
      </section>

      {/* 관리자 — 단일 직원 */}
      <section>
        <Card className="bg-gradient-to-br from-primary-50 to-rose-50 border-primary-100">
          <CardBody>
            <div className="flex flex-col md:flex-row md:items-center gap-6">
              <div className="text-6xl shrink-0 text-center md:text-left">{MANAGER.emoji}</div>
              <div className="flex-1">
                <div className="inline-flex items-center gap-1 bg-white/70 text-primary-700 text-xs px-3 py-1 rounded-full mb-2">
                  담당 직원 한 명
                </div>
                <h2 className="text-2xl font-bold text-neutral-900">{MANAGER.name}</h2>
                <p className="text-neutral-700 mt-1">{MANAGER.oneLiner}</p>
                <ul className="mt-3 space-y-1">
                  {MANAGER.bullets.map((b) => (
                    <li key={b} className="text-sm text-neutral-700 flex items-start gap-2">
                      <ShieldCheck className="w-4 h-4 text-primary mt-0.5 shrink-0" />
                      {b}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </CardBody>
        </Card>
      </section>

      {/* 이렇게 동작합니다 (Valley 접근성/튜토리얼 패턴 차용) */}
      <section>
        <h2 className="text-2xl font-bold text-neutral-900 text-center mb-8">이렇게 동작합니다</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {[
            { n: "①", t: "계좌 연결", d: "한국투자증권 정보를 입력하면 .env 에 계좌별로 저장" },
            { n: "②", t: "AI가 종목 분석 후 제안", d: "투자 컨셉 → AI 종목·시장 분석 → 목표 비중 → 리밸런싱 제안" },
            { n: "③", t: "승인하면 주문", d: "리스크 점검 통과 + 사장님 승인 후에만 실행" },
          ].map((s) => (
            <div key={s.t} className="rounded-2xl border border-neutral-100 bg-white p-6 shadow-card text-center">
              <div className="text-3xl font-bold text-primary mb-2">{s.n}</div>
              <div className="font-bold text-neutral-900">{s.t}</div>
              <div className="text-sm text-neutral-400 mt-1">{s.d}</div>
            </div>
          ))}
        </div>
      </section>

      {/* 관리 중인 계좌 — 로그인/권한으로 필터된 목록(내 계좌 / admin 은 전체 계좌) */}
      <HomeAccountList />

      {/* 왜 안전한가요 (AlphaPrime/Valley 신뢰 섹션 패턴 — 단, 실제 안전장치로) */}
      <section>
        <h2 className="text-2xl font-bold text-neutral-900 text-center mb-2">왜 안전한가요</h2>
        <p className="text-sm text-neutral-400 text-center mb-8">
          과장된 수익률 대신, 시스템에 내장된 실제 안전장치를 보여드립니다.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {[
            { icon: Lock, t: "모의투자 우선", d: "실제 돈이 걸리기 전에 모의투자로 먼저 검증합니다." },
            { icon: ListChecks, t: "6중 리스크 게이트", d: "현금·단일종목·숏·레버리지·drawdown·1주문 한도를 자동 차단." },
            { icon: ClipboardCheck, t: "사람 승인 기본값", d: "무승인 자동매매 없음. 주문은 CEO 승인 후에만." },
            { icon: KeyRound, t: "자격증명 .env 전용", d: "API 키·계좌번호는 코드·DB·로그에 저장하지 않습니다." },
            { icon: Repeat, t: "모든 주문 추적", d: "idempotency 키로 중복·재전송을 막고 전 주문을 기록." },
            { icon: Building2, t: "한국투자증권 공식 API", d: "공인된 OpenAPI 로 연동. 비공식 크롤링 없음." },
          ].map((f) => (
            <div key={f.t} className="rounded-2xl border border-neutral-100 bg-white p-5 shadow-card">
              <f.icon className="w-5 h-5 text-primary mb-2" />
              <div className="font-semibold text-neutral-900">{f.t}</div>
              <div className="text-sm text-neutral-500 mt-1">{f.d}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
