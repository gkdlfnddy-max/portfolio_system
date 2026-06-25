import Link from "next/link";
import { getProfile, getProfileHistory, getLatestPolicy, getCurrentSelection } from "@/lib/server/portfolioDb";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { ArrowLeft, Pencil } from "lucide-react";
import { policyTypeLabel } from "@/lib/policy/labels";
import PolicyDoc from "./PolicyDoc";

export const dynamic = "force-dynamic";

const RISK: Record<string, string> = { aggressive: "공격적", neutral: "중립", defensive: "방어적" };
const SHORT: Record<string, string> = { none: "숏 안 함", insurance: "숏 보험수준", active: "숏 적극" };
const PACE: Record<string, string> = { slow: "천천히 조정", normal: "보통 속도", fast: "빠른 조정" };
const DURATION: Record<string, string> = { short: "단기", intermediate: "중기", long: "장기", mixed: "혼합/사다리" };

function fmtRegion(json: string | null): { text: string; warn: boolean } | null {
  if (!json) return null;
  try {
    const t = JSON.parse(json) as Record<string, number>;
    const entries = Object.entries(t).filter(([, v]) => v != null);
    if (!entries.length) return null;
    const sum = entries.reduce((a, [, v]) => a + Number(v), 0);
    return { text: entries.map(([k, v]) => `${k} ${v}%`).join(" · "), warn: sum !== 100 };
  } catch { return null; }
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  if (value == null || value === "") return null;
  return (
    <div className="flex items-baseline gap-3 py-1.5 border-b border-neutral-50">
      <span className="w-32 shrink-0 text-xs text-neutral-400">{label}</span>
      <span className="text-sm text-neutral-800">{value}</span>
    </div>
  );
}

export default async function StrategyViewPage({ params }: { params: { id: string } }) {
  const id = parseInt(params.id, 10);
  const p = getProfile(id);
  const history = getProfileHistory(id);
  const pol = getLatestPolicy(id);
  const sel = await getCurrentSelection(id); // 현재 active allocation 이 어느 정책 버전을 썼는지(provenance)

  let doc: any = null;
  try { doc = p?.doc ? JSON.parse(p.doc) : null; } catch { doc = null; }
  const keywords: string[] = doc?.keywords ?? [];
  const gaps: string[] = doc?.gaps ?? [];

  return (
    <div className="max-w-2xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${id}/strategy`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 전략 편집
      </Link>

      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-neutral-900">운용 전략 — 정리 문서</h1>
          <p className="text-sm text-neutral-500 mt-1">
            저장된 대전제를 읽기 전용 문서로 봅니다. 단단한 변수는 표로, 진화하는 내용은 문서로(하이브리드).
          </p>
        </div>
        <Link href={`/accounts/${id}/strategy`} className="text-sm text-primary flex items-center gap-1 shrink-0">
          <Pencil className="w-4 h-4" /> 수정
        </Link>
      </div>

      {!p ? (
        <Card><CardBody className="text-center py-10 text-sm text-neutral-500">
          아직 저장된 전략이 없습니다. <Link href={`/accounts/${id}/strategy`} className="text-primary">전략 작성하기</Link>
        </CardBody></Card>
      ) : (
        <>
          {p.posture_text && (
            <Card>
              <CardHeader><CardTitle>컨셉 (원문)</CardTitle></CardHeader>
              <CardBody><p className="text-sm text-neutral-700 whitespace-pre-wrap leading-relaxed">{p.posture_text}</p></CardBody>
            </Card>
          )}

          {/* 투자 정책서 — effective + 출처 + hard rule (백엔드 policy_rules CLI) */}
          <PolicyDoc id={id} />

          {/* 이 정책을 사용한 allocation/decision(provenance) — 기존 DB 값 사용 */}
          {sel && (
            <Card>
              <CardHeader><CardTitle>이 정책을 사용한 선택(provenance)</CardTitle></CardHeader>
              <CardBody className="py-2">
                <Row label="현재 선택안" value={sel.variant ? `${sel.variant} (#${sel.id})` : `#${sel.id}`} />
                <Row label="사용한 정책 버전" value={sel.policy_version != null ? `v${sel.policy_version}` : "—"} />
                <Row label="스타일(현재)" value={(p as { policy_type?: string }).policy_type ? policyTypeLabel((p as { policy_type?: string }).policy_type) : "—"} />
                <Row label="선택 시각" value={sel.selected_at ? new Date(sel.selected_at).toLocaleString("ko-KR") : "—"} />
                <p className="text-[11px] text-neutral-400 mt-2">
                  현재 active allocation 이 이 정책 버전을 기준으로 계산되었습니다. 정책을 바꿔 저장하면 새 버전이 생기고 재계산이 필요합니다.
                </p>
              </CardBody>
            </Card>
          )}

          <Card>
            <CardHeader><CardTitle>핵심 변수 (의사결정이 사용)</CardTitle></CardHeader>
            <CardBody className="py-2">
              <Row label="성향" value={p.risk_tolerance ? RISK[p.risk_tolerance] ?? p.risk_tolerance : ""} />
              <Row label="숏 정책" value={p.short_policy ? SHORT[p.short_policy] ?? p.short_policy : ""} />
              <Row label="현금 밴드" value={p.cash_min_pct != null || p.cash_max_pct != null ? `${p.cash_min_pct ?? "–"} ~ ${p.cash_max_pct ?? "–"}%` : ""} />
              <Row label="조정 속도" value={p.rebalance_pace ? PACE[p.rebalance_pace] ?? p.rebalance_pace : ""} />
              <Row label="개별주 한도" value={p.individual_cap_pct != null ? `${p.individual_cap_pct}%` : ""} />
              <Row label="개별 종목 수" value={p.individual_count != null ? `${p.individual_count}종목` : ""} />
              <Row label="지역 선호" value={p.region_pref} />
              <Row label="투자 기간" value={p.horizon} />
              <Row label="관심 분야" value={p.interests_text} />
              <Row label="내 생각" value={p.views_text} />
            </CardBody>
          </Card>

          {/* 지역 · 채권 구조 (현금과 채권은 둘 다 방어자산이나 역할이 다름 — 분리) */}
          <Card>
            <CardHeader><CardTitle>지역 · 채권 구조</CardTitle></CardHeader>
            <CardBody className="py-2">
              {(() => {
                const reg = fmtRegion(p.region_targets);
                return (
                  <Row label="지역 목표비중" value={
                    reg ? <span>{reg.text}{reg.warn && <span className="text-accent-600 text-xs ml-2">(합계 100% 아님 — 확인)</span>}</span>
                        : <span className="text-neutral-400">미설정 {p.region_pref ? `(선호: ${p.region_pref})` : ""}</span>
                  } />
                );
              })()}
              <Row label="방어자산 중 채권/국채" value={
                p.bond_target_pct != null
                  ? <span>{p.bond_target_pct}% <span className="text-xs text-neutral-400">(방어자산 대비 비율 — 나머지는 순현금)</span></span>
                  : <span className="text-neutral-400">미설정</span>
              } />
              <Row label="채권 듀레이션" value={p.bond_duration_pref ? DURATION[p.bond_duration_pref] ?? p.bond_duration_pref : ""} />
              <Row label="현금 vs 채권" value={<span className="text-xs text-neutral-500">현금=즉시 매수여력 · 채권/국채=방어자산 중 일부를 금리·경기 대응용으로 운용</span>} />
              <p className="text-[11px] text-neutral-400 mt-2">방어자산 bucket = 순현금 + 채권/국채 (현금밴드 총량 유지). 채권은 현금밴드에 무조건 더해지는 게 아니라 방어자산 안에서 순현금과 나누어 계산됩니다. 위험자산(주식/ETF/테마) = 100 − 방어자산.</p>
            </CardBody>
          </Card>

          {pol && (
            <Card>
              <CardHeader><CardTitle>투자 정책값 (컴파일됨 · v{pol.version})</CardTitle></CardHeader>
              <CardBody className="py-2">
                <Row label="현금 목표/밴드" value={`목표 ${pol.policy.cash_band?.target ?? "–"}% (밴드 ${pol.policy.cash_band?.min ?? "–"}~${pol.policy.cash_band?.max ?? "–"}%)`} />
                <Row label="단일 종목 상한" value={`${pol.policy.limits?.single_name_max_pct}%`} />
                <Row label="섹터 상한" value={`${pol.policy.limits?.sector_max_pct}%`} />
                <Row label="개별주 한도" value={pol.policy.limits?.individual_cap_pct != null ? `${pol.policy.limits.individual_cap_pct}% · ${pol.policy.limits?.individual_count ?? "?"}종목` : "—"} />
                <Row label="인버스/레버리지 상한" value={`${pol.policy.limits?.inverse_max_pct}% / ${pol.policy.limits?.leverage_max_pct}%`} />
                <Row label="1주문 상한" value={`${pol.policy.limits?.one_order_cap_pct}%`} />
                <Row label="조정 속도" value={pol.policy.pace} />
                <Row label="지역 목표(정책)" value={pol.policy.region_targets && Object.keys(pol.policy.region_targets).length
                  ? Object.entries(pol.policy.region_targets).map(([k, v]) => `${k} ${v}%`).join(" · ") : "—"} />
                <Row label="채권 목표(정책)" value={pol.policy.bond?.target_pct != null
                  ? `${pol.policy.bond.target_pct}% · ${DURATION[pol.policy.bond.duration_pref] ?? pol.policy.bond?.duration_pref ?? "—"}` : "—"} />
                <Row label="국가/신흥국 한도" value={`단일국가 ${pol.policy.limits?.max_single_country_pct ?? "–"}% · 신흥국 ${pol.policy.limits?.emerging_market_max_pct ?? "–"}%`} />
                <Row label="금지 자산" value={(pol.policy.forbidden_assets ?? []).length ? pol.policy.forbidden_assets.join(", ") : "없음"} />
                <p className="text-[11px] text-neutral-400 mt-2">이 정책값을 decision engine 이 그대로 사용합니다 (provenance 에 기록).</p>
              </CardBody>
            </Card>
          )}

          {pol && (pol.policy.accepted_advice ?? []).length > 0 && (
            <Card>
              <CardHeader><CardTitle>반영한 조언 (정책에 포함)</CardTitle></CardHeader>
              <CardBody>
                <ul className="space-y-1.5">
                  {pol.policy.accepted_advice.map((a: any, i: number) => (
                    <li key={i} className="text-sm text-neutral-700">✓ {String(a.title).replace(/^\[메모리\]\s*/, "")}</li>
                  ))}
                </ul>
                <p className="text-[11px] text-neutral-400 mt-2">반영한 조언은 정책 객체에 실려 allocation·decision provenance 로 하위 전제에 전파됩니다.</p>
              </CardBody>
            </Card>
          )}

          {keywords.length > 0 && (
            <Card>
              <CardHeader><CardTitle>정리된 키워드</CardTitle></CardHeader>
              <CardBody>
                <div className="flex flex-wrap gap-2">
                  {keywords.map((k, i) => <span key={i} className="text-xs rounded-full bg-primary-50 text-primary-700 px-2.5 py-1">{k}</span>)}
                </div>
              </CardBody>
            </Card>
          )}

          {gaps.length > 0 && (
            <Card>
              <CardHeader><CardTitle>보완하면 좋을 점</CardTitle></CardHeader>
              <CardBody>
                <ul className="space-y-1">{gaps.map((g, i) => <li key={i} className="text-sm text-neutral-600">· {g}</li>)}</ul>
              </CardBody>
            </Card>
          )}

          <Card>
            <CardHeader><CardTitle>변경 이력 (버전)</CardTitle></CardHeader>
            <CardBody>
              {history.length === 0 ? (
                <p className="text-sm text-neutral-400">이력 없음</p>
              ) : (
                <ul className="space-y-1.5">
                  {history.map((h) => (
                    <li key={h.id} className="text-xs text-neutral-500 flex items-center gap-2">
                      <span className="font-mono text-neutral-400">#{h.id}</span>
                      <span className="rounded bg-neutral-100 px-1.5 py-0.5">{h.source ?? "—"}</span>
                      <span>{new Date(h.created_at).toLocaleString("ko-KR")}</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-[11px] text-neutral-400 mt-2">매 저장마다 전체 스냅샷이 append-only 로 쌓입니다 — 되돌리기·감사 근거.</p>
            </CardBody>
          </Card>

          <p className="text-xs text-neutral-400">
            갱신 {p.updated_at ? new Date(p.updated_at).toLocaleString("ko-KR") : "—"} · 정리 주체 {p.refined_by ?? "—"} · 전부 DB 저장값(SQLite)
          </p>
        </>
      )}
    </div>
  );
}
