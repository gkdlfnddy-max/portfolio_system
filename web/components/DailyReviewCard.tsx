"use client";

import { useEffect, useState, useCallback } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { CalendarCheck, RefreshCw } from "lucide-react";

// 실시간 봇 아님 — 오늘의 점검. "관망"도 정상 결과.
const ACTION: Record<string, { label: string; cls: string }> = {
  rebalance: { label: "오늘 조정 후보 있음", cls: "bg-primary-50 text-primary-700" },
  hold: { label: "관망 (조정 불필요)", cls: "bg-neutral-100 text-neutral-600" },
  watch: { label: "관망 (점검만)", cls: "bg-neutral-100 text-neutral-500" },
  buy: { label: "매수 후보", cls: "bg-primary-50 text-primary-700" },
  sell: { label: "매도 후보", cls: "bg-warning/10 text-warning" },
};

export function DailyReviewCard({ accountId }: { accountId: number }) {
  const [rev, setRev] = useState<any | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/accounts/${accountId}/daily-review`, { cache: "no-store" });
      const j = await r.json();
      setRev(j?.review ?? null);
    } catch { setRev(null); }
    setLoaded(true);
  }, [accountId]);
  useEffect(() => { load(); }, [load]);

  const runToday = async () => {
    setBusy(true);
    try {
      const r = await fetch(`/api/accounts/${accountId}/daily-review`, { method: "POST" });
      const j = await r.json();
      setRev(j?.review ?? null);
    } catch { /* noop */ }
    setBusy(false);
  };

  const a = rev?.action_decision ? ACTION[rev.action_decision] ?? { label: rev.action_decision, cls: "bg-neutral-100" } : null;

  // 채권 듀레이션 추천 — generate_review(top-level) 또는 latest(payload) 어느 쪽이든 읽음.
  const dur = rev?.duration_recommendation ?? rev?.payload?.duration_recommendation ?? null;
  const DUR_KO: Record<string, string> = { short: "단기", intermediate: "중기", long: "장기", mixed: "사다리(혼합)" };

  // 국채(govbond) 점검 — 재검토 후보(자동 변경 0). top-level 또는 payload 어느 쪽이든 읽음.
  const gov = rev?.govbond_check ?? rev?.payload?.govbond_check ?? null;
  const govB = gov?.breakdown ?? null;
  const govCands: any[] = Array.isArray(gov?.candidates) ? gov.candidates : [];
  const govChecks: any[] = Array.isArray(gov?.checks) ? gov.checks : [];

  // 스윙/헤지 점검 — generate(top-level) 또는 latest(payload) 어느 쪽이든 읽음.
  const swing = rev?.swing_hedge ?? rev?.payload?.swing_hedge ?? null;
  const nextReview = rev?.next_review ?? rev?.payload?.next_review ?? null;
  // 오늘의 6축 상태 — 기술·거시·분산·이벤트·심리·정책. generate(top-level) 또는 latest(payload) 어느 쪽이든 읽음.
  const sixAxis = rev?.six_axis ?? rev?.payload?.six_axis ?? null;
  const sixAxes: any[] = Array.isArray(sixAxis?.axes) ? sixAxis.axes : [];
  // 수급(분산축) — 외국인/기관/개인 흐름+해석. generate(top-level) 또는 latest(payload) 어느 쪽이든 읽음.
  const supply = rev?.supply_demand ?? rev?.payload?.supply_demand ?? null;
  // 순매수/순매도 라벨 색상(설명용 — 단정 아님: '순매도≠매도').
  const FLOW_CLS: Record<string, string> = {
    순매수: "bg-primary-50 text-primary-700",
    순매도: "bg-warning/10 text-warning",
    중립: "bg-neutral-100 text-neutral-500",
  };
  // carry-over(직전 미체결 후보 재평가) + evidence(근거 연결) + stale 표기.
  const carry = rev?.carry_over ?? rev?.payload?.carry_over ?? null;
  const evidence = rev?.evidence ?? rev?.payload?.evidence ?? null;
  const stale = rev?.payload?.stale === true;
  const CARRY_KO: Record<string, { label: string; cls: string }> = {
    carry: { label: "이월", cls: "bg-neutral-100 text-neutral-600" },
    expire: { label: "만료", cls: "bg-warning/10 text-warning" },
  };
  const ACT_KO: Record<string, { label: string; cls: string }> = {
    maintain: { label: "유지", cls: "bg-neutral-100 text-neutral-600" },
    reduce: { label: "축소", cls: "bg-warning/10 text-warning" },
    expand: { label: "확대", cls: "bg-primary-50 text-primary-700" },
  };

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2"><CalendarCheck className="w-5 h-5 text-primary" /> 오늘의 포트폴리오 점검</CardTitle>
        <Button size="sm" variant="outline" onClick={runToday} disabled={busy}>
          <RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} /> 오늘 점검 실행
        </Button>
      </CardHeader>
      <CardBody className="space-y-2">
        {!loaded ? (
          <p className="text-sm text-neutral-400">불러오는 중…</p>
        ) : !rev ? (
          <p className="text-sm text-neutral-500">아직 점검 결과가 없습니다. <b>오늘 점검 실행</b>을 눌러 현재 비중과 목표비중을 비교해 보세요.</p>
        ) : (
          <>
            <div className="flex items-center gap-2 flex-wrap">
              {a && <Badge className={a.cls}>{a.label}</Badge>}
              {rev.drift_score != null && <span className="text-xs text-neutral-500">목표 대비 최대 차이 {rev.drift_score}%</span>}
              {rev.review_date && <span className="text-xs text-neutral-400">· {rev.review_date}</span>}
            </div>
            <p className="text-sm text-neutral-700">{rev.action_reason}</p>
            {rev.no_trade_reason && <p className="text-sm text-neutral-500">· {rev.no_trade_reason}</p>}
            {stale && (
              <p className="text-xs text-warning">· 스냅샷이 오래되어(stale) 안전을 위해 관망 처리했습니다 — 동기화 후 재점검하세요.</p>
            )}
            {rev.has_orders ? (
              <p className="text-xs text-primary-700">예약성 지정가 조정 후보가 생성되었습니다 (계획 #{rev.scheduled_order_plan_id}). 시장가 매수 없음 · 사람 승인 후에만 실행 · 미체결은 다음 cycle 재평가.</p>
            ) : (
              <p className="text-xs text-neutral-400">오늘은 주문 후보 없음 — 관망도 정상 결과입니다.</p>
            )}
          </>
        )}
        {sixAxis && sixAxes.length > 0 && (
          <div className="mt-2 border-t border-neutral-100 pt-2 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-neutral-700">오늘의 6축 상태</span>
              <span className="text-xs text-neutral-500">
                연동 {sixAxis.available_count ?? 0}/{sixAxis.total_axes ?? 6}축
              </span>
              {sixAxis.overall_confidence != null && (
                <span className="text-xs text-neutral-400">종합 신뢰 {(sixAxis.overall_confidence * 100).toFixed(0)}%</span>
              )}
              {sixAxis.holistic_risk != null && (
                <span className="text-xs text-neutral-400">종합 위험 {sixAxis.holistic_risk}</span>
              )}
            </div>
            <div className="grid grid-cols-3 gap-1.5">
              {sixAxes.map((ax: any) => (
                <div
                  key={ax.axis}
                  className={`rounded-md px-2 py-1.5 text-center border ${
                    ax.data_available ? "border-success/30 bg-success/5" : "border-neutral-200 bg-neutral-50"
                  }`}
                >
                  <div className="text-xs font-medium text-neutral-700">{ax.label}</div>
                  <div className={`text-[10px] mt-0.5 ${ax.data_available ? "text-success" : "text-neutral-400"}`}>
                    {ax.data_available ? "연동" : "미연동"}
                    {ax.data_available && ax.confidence != null ? ` · ${(ax.confidence * 100).toFixed(0)}%` : ""}
                  </div>
                </div>
              ))}
            </div>
            {Array.isArray(sixAxis.missing_axes) && sixAxis.missing_axes.length > 0 && (
              <p className="text-[11px] text-neutral-400">
                미연동 축({sixAxis.missing_axes.join(", ")})은 분석에서 제외했습니다 — "모든 데이터를 고려했다"고 말하지 않습니다.
              </p>
            )}
            {Array.isArray(sixAxis.portfolio_impact) && sixAxis.portfolio_impact.map((t: string, i: number) => (
              <p key={i} className="text-xs text-neutral-600">· {t}</p>
            ))}
            <p className="text-[11px] text-neutral-400">
              데이터 없는 축은 제외(정직) · 신뢰도 낮으면 단정하지 않음 · 자동 주문/정책 변경 없음(전부 후보, 승인 전 미반영).
            </p>
          </div>
        )}
        {dur && (
          <div className="mt-2 border-t border-neutral-100 pt-2 space-y-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-neutral-700">채권 듀레이션 점검</span>
              <Badge className="bg-sky-50 text-sky-700">권장: {DUR_KO[dur.recommended] ?? dur.recommended_ko ?? dur.recommended}</Badge>
              {dur.current_pref && dur.current_pref !== dur.recommended && (
                <span className="text-xs text-neutral-400">현재 {DUR_KO[dur.current_pref] ?? dur.current_pref}</span>
              )}
            </div>
            {dur.reason && <p className="text-xs text-neutral-600">{dur.reason}</p>}
            {dur.vs_current && <p className="text-xs text-neutral-500">· {dur.vs_current}</p>}
            {Array.isArray(dur.warnings) && dur.warnings.map((w: string, i: number) => (
              <p key={i} className="text-xs text-warning">· {w}</p>
            ))}
            {dur.data_connected === false && (
              <p className="text-[11px] text-neutral-400">※ 금리·경제 데이터 미연동 — 보수적 기본값(불확실) 기준 추천입니다.</p>
            )}
          </div>
        )}
        {gov && gov.data_available && (
          <div className="mt-2 border-t border-neutral-100 pt-2 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-neutral-700">국채(방어) 점검</span>
              {gov.candidate_count > 0 ? (
                <Badge className="bg-warning/10 text-warning">재검토 후보 {gov.candidate_count}건</Badge>
              ) : (
                <Badge className="bg-neutral-100 text-neutral-600">현 구성 유지 가능(관망)</Badge>
              )}
              {gov.rate_regime && (
                <span className="text-xs text-neutral-400">금리환경 {gov.rate_regime}</span>
              )}
            </div>
            {govB && (
              <p className="text-xs text-neutral-500">
                국채 {govB.govbond_pct}% (단기 {govB.short_govbond_pct}% / 장기 {govB.long_govbond_pct}%
                {govB.long_share_pct != null ? ` · 장기비중 ${govB.long_share_pct}%` : ""}) · 순현금 {govB.pure_cash_pct}% · 위험자산 {govB.risk_asset_pct}%
              </p>
            )}
            {govCands.length > 0 ? (
              <div className="space-y-1">
                {govCands.map((c: any, i: number) => (
                  <div key={i} className="rounded-md bg-warning/5 px-2 py-1.5 flex items-start gap-1.5">
                    <Badge className="bg-warning/10 text-warning shrink-0">재검토</Badge>
                    <span className="text-xs text-neutral-600">{c.candidate}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-neutral-400">국채 비중·단장 비율·유동성·환율·금리환경 점검 — 오늘은 재검토 후보 없음(관망도 정상).</p>
            )}
            {govChecks.filter((c: any) => c.status === "ok").slice(0, 2).map((c: any, i: number) => (
              <p key={`ok-${i}`} className="text-[11px] text-neutral-400">· {c.msg}</p>
            ))}
            <p className="text-[11px] text-neutral-400">
              국채 bucket 은 한 번 정하면 끝이 아니라 금리/환율 환경에 따라 재점검 대상입니다 — 단, <b>자동 변경은 없습니다</b>(전부 후보·사람 승인).
              국채 ETF 는 방어자산 구현 수단이며 수익 극대화 수단이 아닙니다. 데이터 없는 축은 정직하게 미연동으로 표기합니다.
            </p>
          </div>
        )}
        {swing && (
          <div className="mt-2 border-t border-neutral-100 pt-2 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-neutral-700">오늘의 스윙/헤지 점검</span>
              {swing.overall?.today_net_pct != null && (
                <span className="text-xs text-neutral-500">
                  순노출 {swing.overall.today_net_pct}% · 총노출 {swing.overall.today_gross_pct}% · 헤지비율 {swing.overall.today_hedge_ratio_pct}%
                </span>
              )}
            </div>
            {swing.overall?.defensive_pct != null && (
              <p className="text-xs text-neutral-500">
                방어자산(순현금+채권) {swing.overall.defensive_pct}% · 테마 노출 {swing.overall.theme_exposure_pct}%
              </p>
            )}
            {Array.isArray(swing.themes) && swing.themes.length > 0 ? (
              <div className="space-y-1">
                {swing.themes.map((t: any) => {
                  const act = ACT_KO[t.action] ?? { label: t.action, cls: "bg-neutral-100 text-neutral-600" };
                  return (
                    <div key={t.theme} className="rounded-md bg-neutral-50 px-2 py-1.5">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-medium text-neutral-700">{t.theme}</span>
                        <Badge className={act.cls}>{act.label}</Badge>
                        <span className="text-xs text-neutral-500">
                          롱 {t.long_pct}% / 헤지 {t.hedge_pct}% · 순 {t.net_pct}% / 총 {t.gross_pct}% · 헤지비율 {t.hedge_ratio_pct}%
                        </span>
                      </div>
                      {t.reason && <p className="text-xs text-neutral-500 mt-0.5">· {t.reason}</p>}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-neutral-400">롱·숏이 혼재된 스윙 테마가 없습니다 — 점검할 헤지 노출이 없습니다.</p>
            )}
            <p className="text-xs text-neutral-500">
              {rev.has_orders
                ? "오늘 주문 후보 있음 — 단, 스윙/헤지 노출은 점검용이며 그 자체가 주문 신호는 아닙니다."
                : "오늘 주문 안 함 — " + (rev.no_trade_reason || "조정 불필요(관망도 정상)") + "."}
            </p>
            {nextReview && <p className="text-[11px] text-neutral-400">다음 점검: {nextReview} (일·주 단위 판단)</p>}
          </div>
        )}
        {supply && (
          <div className="mt-2 border-t border-neutral-100 pt-2 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-neutral-700">오늘의 수급 (투자자별 매매동향)</span>
              {supply.data_available ? (
                <Badge className="bg-sky-50 text-sky-700">데이터 연동</Badge>
              ) : (
                <Badge className="bg-neutral-100 text-neutral-500">데이터 부족 — 판단 제외</Badge>
              )}
              {supply.data_available && supply.confidence != null && (
                <span className="text-xs text-neutral-400">신뢰 {supply.confidence}</span>
              )}
            </div>
            {supply.data_available && supply.aggregate ? (
              <>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-xs text-neutral-500">최근 {supply.aggregate.window_days}일</span>
                  <Badge className={FLOW_CLS[supply.aggregate.foreign] ?? "bg-neutral-100"}>외국인 {supply.aggregate.foreign}</Badge>
                  <Badge className={FLOW_CLS[supply.aggregate.institution] ?? "bg-neutral-100"}>기관 {supply.aggregate.institution}</Badge>
                  <Badge className={FLOW_CLS[supply.aggregate.retail] ?? "bg-neutral-100"}>개인 {supply.aggregate.retail}</Badge>
                </div>
                {Array.isArray(supply.interpretation) && supply.interpretation.map((t: string, i: number) => (
                  <p key={i} className="text-xs text-neutral-600">· {t}</p>
                ))}
                {Array.isArray(supply.portfolio_impact) && supply.portfolio_impact.map((t: string, i: number) => (
                  <p key={i} className="text-xs text-neutral-500">· {t}</p>
                ))}
                {Array.isArray(supply.candidates) && supply.candidates.length > 0 && (
                  <div className="space-y-0.5">
                    {supply.candidates.map((c: any, i: number) => (
                      <div key={i} className="flex items-center gap-1.5 flex-wrap">
                        <Badge className="bg-neutral-100 text-neutral-600">후보</Badge>
                        <span className="text-xs text-neutral-600">{c.candidate}</span>
                      </div>
                    ))}
                  </div>
                )}
                <p className="text-[11px] text-neutral-400">
                  ‘순매도=매도’ 식 단정이 아닙니다 — 진입 속도 조절·현금밴드 상향·hedge 검토는 전부 후보이며 자동 주문/정책 변경은 없습니다.
                </p>
              </>
            ) : (
              <p className="text-xs text-neutral-400">{supply.note ?? "투자자별 매매동향 데이터 부족 — 수급 판단을 정직하게 제외했습니다."}</p>
            )}
          </div>
        )}
        {carry && Array.isArray(carry.items) && carry.items.length > 0 && (
          <div className="mt-2 border-t border-neutral-100 pt-2 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-neutral-700">직전 미체결 후보 재평가</span>
              <span className="text-xs text-neutral-500">이월 {carry.carry_count ?? 0} · 만료 {carry.expire_count ?? 0}</span>
            </div>
            <div className="space-y-1">
              {carry.items.map((it: any) => {
                const v = CARRY_KO[it.verdict] ?? { label: it.verdict, cls: "bg-neutral-100 text-neutral-600" };
                return (
                  <div key={it.step_id} className="rounded-md bg-neutral-50 px-2 py-1.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm text-neutral-700">{it.ref ?? "후보"}</span>
                      {it.direction && <span className="text-xs text-neutral-500">{it.direction}</span>}
                      <Badge className={v.cls}>{v.label}</Badge>
                      {it.age_days != null && <span className="text-xs text-neutral-400">경과 {it.age_days}일</span>}
                    </div>
                    {it.note && <p className="text-xs text-neutral-500 mt-0.5">· {it.note}</p>}
                  </div>
                );
              })}
            </div>
            <p className="text-[11px] text-neutral-400">미체결 후보의 재평가일 뿐 자동 주문이 아닙니다 — 추격 매수 없음.</p>
          </div>
        )}
        {evidence && (
          <div className="mt-2 border-t border-neutral-100 pt-2 space-y-1">
            <span className="text-sm font-medium text-neutral-700">연결된 근거</span>
            {Array.isArray(evidence.links) && evidence.links.length > 0 ? (
              <div className="space-y-1">
                {evidence.links.map((e: any) => (
                  <div key={e.evidence_id} className="rounded-md bg-neutral-50 px-2 py-1.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      {e.stance && <Badge className="bg-sky-50 text-sky-700">{e.stance}</Badge>}
                      {e.theme && <span className="text-xs text-neutral-500">{e.theme}</span>}
                      {e.eff_confidence != null && <span className="text-[11px] text-neutral-400">신뢰 {e.eff_confidence}</span>}
                    </div>
                    {e.summary && <p className="text-xs text-neutral-600 mt-0.5">{e.summary}</p>}
                  </div>
                ))}
                <p className="text-[11px] text-neutral-400">근거는 입장(stance) 태깅일 뿐 그 자체가 주문 신호가 아닙니다.</p>
              </div>
            ) : (
              <p className="text-xs text-neutral-400">연결된 외부 근거가 없습니다 — 근거 없는 판단은 정직하게 표기합니다.</p>
            )}
          </div>
        )}
        <p className="text-[11px] text-neutral-400 mt-1">실시간 매매가 아니라 정기 점검입니다. 주문 후보는 확정된 목표비중(selected allocation)과 drift에서만 생성됩니다.</p>
      </CardBody>
    </Card>
  );
}
