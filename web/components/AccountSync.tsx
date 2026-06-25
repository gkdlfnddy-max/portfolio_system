"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { CheckCircle, Circle, RefreshCw, ArrowRight, AlertCircle, Wallet, Clock } from "lucide-react";

type Step = { key: string; label: string; desc: string; done: boolean };
type Holding = { ticker: string; qty: number; avg_price: number; market_value: number };
type Snapshot = { cash_krw: number | null; total_value_krw: number | null; holdings_count: number | null; captured_at: string };
type View = {
  alias: string | null;
  mode: string | null;
  sync_status: string | null;
  last_error: string | null;
  last_synced_at: string | null;
  progress: number;
  isFresh: boolean;
  steps: Step[];
  snapshot: Snapshot | null;
  holdings: Holding[];
};

const won = (n: number) => Math.round(n).toLocaleString("ko-KR") + "원";

// ── 클라이언트 측 KIS 호출 조정자 (계좌별 단일 진행 신호) ──────────────────
// 같은 계좌에서 자동 sync(AccountSync)와 수동 연결 테스트(BrokerTestPanel)가
// *동시에* KIS 를 때리지 않도록, 둘이 공유하는 모듈 스코프 busy 신호를 둔다.
// 서버(kisGuard)의 single-flight/lock 와 더불어 호출 자체를 클라에서도 줄인다.
type Listener = () => void;
const busySet = new Set<number>();
const listeners = new Set<Listener>();
function emit() {
  listeners.forEach((l) => l());
}
export function isKisBusy(accountId: number): boolean {
  return busySet.has(accountId);
}
export function subscribeKisBusy(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}
// 진행 중 표시. 이미 진행 중이면 false(시작하지 말 것), 시작했으면 release 콜백 반환.
export function acquireKis(accountId: number): null | (() => void) {
  if (busySet.has(accountId)) return null;
  busySet.add(accountId);
  emit();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    busySet.delete(accountId);
    emit();
  };
}

export function AccountSync({ accountId }: { accountId: number }) {
  const [view, setView] = useState<View | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [waiting, setWaiting] = useState(false); // rate-limit 자동 재시도 대기
  const autoTried = useRef(false);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/accounts/${accountId}`, { cache: "no-store" });
      setView(r.ok ? await r.json() : null);
    } catch {
      setView(null);
    }
  }, [accountId]);

  const runSync = useCallback(async () => {
    if (inFlight.current) return; // 연타/중복 방지
    // 다른 쪽(연결 테스트)이 같은 계좌 KIS 진행 중이면 잠시 대기 후 재시도.
    const release = acquireKis(accountId);
    if (!release) {
      setWaiting(true);
      if (retryTimer.current) clearTimeout(retryTimer.current);
      retryTimer.current = setTimeout(() => runSync(), 4000);
      return;
    }
    inFlight.current = true;
    setSyncing(true);
    setWaiting(false);
    try {
      const r = await fetch(`/api/accounts/${accountId}/sync`, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      // rate-limit(EGW00201/초당 거래건수/HTTP 429·500) → 실패 아닌 자동 재시도 대기.
      if (r.status === 429 || j?.rateLimited) {
        setWaiting(true);
        const delay = typeof j?.retryAfterMs === "number" ? j.retryAfterMs : 5000;
        if (retryTimer.current) clearTimeout(retryTimer.current);
        retryTimer.current = setTimeout(() => runSync(), delay);
      }
    } catch {
      /* DB 조회로 상태 확인 */
    } finally {
      release();
      inFlight.current = false;
      setSyncing(false);
    }
    await load();
  }, [accountId, load]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      await load();
      if (cancelled) return;
      setLoading(false);
    })();
    return () => {
      cancelled = true;
      if (retryTimer.current) clearTimeout(retryTimer.current);
    };
  }, [load]);

  // 최신 스냅샷이 아니면 1회 자동 동기화(백엔드 job 트리거 후 DB 재조회)
  useEffect(() => {
    if (!loading && view && !view.isFresh && !autoTried.current) {
      autoTried.current = true;
      runSync();
    }
  }, [loading, view, runSync]);

  const steps = view?.steps ?? [
    { key: "credentials", label: "자격증명 저장", desc: "", done: false },
    { key: "token", label: "KIS 토큰 검증", desc: "", done: false },
    { key: "balance", label: "잔고 동기화", desc: "", done: false },
    { key: "ready", label: "관리 준비 완료", desc: "", done: false },
  ];
  const progress = view?.progress ?? 0;
  const snap = view?.snapshot ?? null;
  const busy = loading || syncing;

  return (
    <>
      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle>연결 준비</CardTitle>
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-primary">
              {waiting ? "재시도 대기…" : busy ? "동기화 중…" : progress + "%"}
            </span>
            <Button size="sm" variant="outline" onClick={runSync} disabled={busy || waiting}>
              <RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} /> 동기화
            </Button>
          </div>
        </CardHeader>
        <CardBody className="space-y-4">
          <div className="h-2 bg-neutral-100 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${progress === 100 ? "bg-success" : "bg-primary"}`}
              style={{ width: `${Math.max(progress, 4)}%` }}
            />
          </div>
          <div className="space-y-2">
            {steps.map((s) => (
              <div key={s.key} className="flex items-start gap-3">
                {s.done ? (
                  <CheckCircle className="w-5 h-5 text-success shrink-0 mt-0.5" />
                ) : (
                  <Circle className="w-5 h-5 text-neutral-300 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className={`text-sm font-medium ${s.done ? "text-neutral-500" : "text-neutral-900"}`}>{s.label}</div>
                  {s.desc && <div className="text-xs text-neutral-400">{s.desc}</div>}
                </div>
              </div>
            ))}
          </div>

          {waiting && (
            <div className="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-200 p-3 text-sm text-amber-800">
              <Clock className="w-4 h-4 mt-0.5 shrink-0 animate-pulse" />
              <div>요청이 많아 잠시 후 자동 재시도합니다. (증권사 초당 호출 한도 — 실패 아님)</div>
            </div>
          )}

          {!waiting && view?.sync_status === "error" && view.last_error && (
            <div className="flex items-start gap-2 rounded-lg bg-error/5 border border-error/20 p-3 text-sm text-error">
              <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
              <div>연결 실패: {view.last_error}</div>
            </div>
          )}

          {view?.last_synced_at && (
            <div className="text-[11px] text-neutral-400">
              마지막 동기화: {new Date(view.last_synced_at).toLocaleString("ko-KR")} · 출처 DB 스냅샷
            </div>
          )}

          {progress === 100 && (
            <Link href={`/accounts/${accountId}/strategy`}>
              <Button className="w-full">
                다음: 운용 전략 설정 (어떻게 운용할지) <ArrowRight className="w-4 h-4" />
              </Button>
            </Link>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Wallet className="w-5 h-5 text-primary" /> 보유 종목 · 잔고
          </CardTitle>
        </CardHeader>
        <CardBody>
          {busy && !snap ? (
            <div className="text-sm text-neutral-400 py-6 text-center">DB 스냅샷을 불러오는 중…</div>
          ) : !snap ? (
            <div className="text-sm text-neutral-400 py-6 text-center">아직 동기화된 잔고가 없습니다. 위 “동기화”를 눌러주세요.</div>
          ) : (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-xl bg-neutral-50 p-3">
                  <div className="text-xs text-neutral-400">예수금</div>
                  <div className="text-lg font-bold text-neutral-900 tabular-nums">{won(snap.cash_krw ?? 0)}</div>
                </div>
                <div className="rounded-xl bg-neutral-50 p-3">
                  <div className="text-xs text-neutral-400">총 평가액</div>
                  <div className="text-lg font-bold text-neutral-900 tabular-nums">{won(snap.total_value_krw ?? 0)}</div>
                </div>
              </div>
              {(view?.holdings.length ?? 0) === 0 ? (
                <div className="text-sm text-neutral-500 text-center py-4">
                  보유 종목 없음 — 현금 100%. AI 관리자가 리밸런싱을 제안할 수 있습니다.
                </div>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-neutral-400 text-xs border-b border-neutral-100">
                      <th className="text-left py-1">종목</th>
                      <th className="text-right">수량</th>
                      <th className="text-right">평단</th>
                      <th className="text-right">평가액</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view!.holdings.map((h) => (
                      <tr key={h.ticker} className="border-b border-neutral-50">
                        <td className="py-1.5 font-mono text-xs">{h.ticker}</td>
                        <td className="text-right tabular-nums">{h.qty}</td>
                        <td className="text-right tabular-nums">{won(h.avg_price)}</td>
                        <td className="text-right tabular-nums font-medium">{won(h.market_value)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </CardBody>
      </Card>
    </>
  );
}
