"use client";

import { useState, useEffect, useRef, useSyncExternalStore } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { CheckCircle, XCircle, Circle, PlugZap, AlertCircle, Clock } from "lucide-react";
import { BrokerBadge } from "@/components/BrokerBadge";
import { acquireKis, isKisBusy, subscribeKisBusy } from "@/components/AccountSync";

// stage 라벨 + 순서(서버 conn_test.STAGE_ORDER 와 동일). 서버가 결과를 truth 로 제공.
const STAGE_LABEL: Record<string, string> = {
  credential: "자격증명",
  token: "토큰 발급",
  account: "계좌번호 유효",
  cash: "예수금 조회",
  balance: "잔고/보유종목",
  quote: "현재가 조회",
};
const STAGE_ORDER = ["credential", "token", "account", "cash", "balance", "quote"];

// 실패 원인(reason) → 사람이 읽는 안내.
const REASON_HINT: Record<string, string> = {
  credential: "키움 키 입력 필요 (NotConfigured) — .env 에 APP Key/Secret 추가 후 재시도",
  token: "토큰 발급 실패 — 키/시크릿 또는 모의투자 신청 상태 확인",
  account: "계좌번호 미설정/무효 — .env ACCOUNT_NO 확인",
  tr: "거래(TR) 응답 오류 — 응답 필드/권한 확인",
  network: "네트워크 오류 — 잠시 후 재시도",
  rate_limit: "호출 한도 초과 — 잠시 후 재시도",
};

type Stage = { stage: string; label?: string; ok: boolean; error: string | null; reason: string | null };
type TestResult = {
  account_index: number;
  broker: string;
  mode: string | null;
  stages: Stage[];
  ok: boolean;
  snapshot_saved: boolean;
  error?: string;
};

export function BrokerTestPanel({ accountId, broker }: { accountId: number; broker?: string | null }) {
  const [result, setResult] = useState<TestResult | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false); // rate-limit 자동 재시도 대기
  const runningRef = useRef(false); // 연타/중복 방지
  const lastClick = useRef(0); // debounce
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 같은 계좌에서 자동 sync 가 KIS 진행 중인지 구독 → 진행 중이면 버튼 대기 표시.
  const otherBusy = useSyncExternalStore(
    subscribeKisBusy,
    () => isKisBusy(accountId),
    () => false,
  );

  useEffect(() => () => { if (retryTimer.current) clearTimeout(retryTimer.current); }, []);

  async function run(auto = false) {
    // debounce: 사용자 연타 시 800ms 내 재호출 무시.
    if (!auto) {
      const now = Date.now();
      if (now - lastClick.current < 800) return;
      lastClick.current = now;
    }
    if (runningRef.current) return; // 이미 진행 중
    // 같은 계좌 KIS 가 다른 작업(자동 sync)으로 점유 중이면 잠시 후 자동 재시도(실패 아님).
    const release = acquireKis(accountId);
    if (!release) {
      setWaiting(true);
      setErr(null);
      if (retryTimer.current) clearTimeout(retryTimer.current);
      retryTimer.current = setTimeout(() => run(true), 4000);
      return;
    }
    runningRef.current = true;
    setRunning(true);
    setWaiting(false);
    setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/broker-test`, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      // rate-limit(EGW00201/초당 거래건수/HTTP 429) → 실패 아닌 자동 재시도 대기.
      if (r.status === 429 || j?.rateLimited) {
        setWaiting(true);
        const delay = typeof j?.retryAfterMs === "number" ? j.retryAfterMs : 5000;
        if (retryTimer.current) clearTimeout(retryTimer.current);
        retryTimer.current = setTimeout(() => run(true), delay);
      } else if (!r.ok || j?.ok === false) {
        setErr(j?.error ?? "연결 테스트에 실패했습니다.");
        setResult(j?.test ?? null);
      } else {
        setResult(j.test as TestResult);
      }
    } catch {
      setErr("연결 테스트 요청에 실패했습니다.");
    } finally {
      release();
      runningRef.current = false;
      setRunning(false);
    }
  }

  // stage 결과를 고정 순서로 매핑(미실행 stage 는 pending).
  const byStage = new Map((result?.stages ?? []).map((s) => [s.stage, s]));
  const credFail = byStage.get("credential")?.ok === false && byStage.get("credential")?.reason === "credential";

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <PlugZap className="w-5 h-5 text-primary" /> 연결 테스트
          <BrokerBadge broker={broker} />
        </CardTitle>
        <Button size="sm" onClick={() => run()} disabled={running || waiting || otherBusy}>
          {running ? "테스트 중…" : waiting ? "재시도 대기…" : otherBusy ? "동기화 대기…" : "연결 테스트"}
        </Button>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-xs text-neutral-400">
          위 “연결 준비”가 잔고를 동기화한다면, 여기서는 연결을 <b>단계별로 점검</b>합니다 —
          자격증명 → 토큰 → 계좌 → 예수금 → 잔고 → 현재가.
          <b> 주문은 실행하지 않습니다.</b> (자동 동기화와 충돌하지 않도록 같은 계좌 호출은 순차 실행됩니다.)
        </p>

        {waiting && (
          <div className="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-200 p-3 text-sm text-amber-800">
            <Clock className="w-4 h-4 mt-0.5 shrink-0 animate-pulse" />
            <div>
              {otherBusy
                ? "동기화가 진행 중입니다 — 끝나면 자동으로 연결 테스트를 이어서 실행합니다."
                : "요청이 많아 잠시 후 자동 재시도합니다. (증권사 초당 호출 한도 — 실패 아님)"}
            </div>
          </div>
        )}

        {result === null && !running ? (
          <div className="text-sm text-neutral-400 py-4 text-center">
            “연결 테스트”를 누르면 단계별 점검 결과가 표시됩니다.
          </div>
        ) : (
          <div className="space-y-2">
            {STAGE_ORDER.map((key) => {
              const s = byStage.get(key);
              const label = STAGE_LABEL[key];
              return (
                <div key={key} className="flex items-start gap-3">
                  {!s ? (
                    <Circle className="w-5 h-5 text-neutral-300 shrink-0 mt-0.5" />
                  ) : s.ok ? (
                    <CheckCircle className="w-5 h-5 text-success shrink-0 mt-0.5" />
                  ) : (
                    <XCircle className="w-5 h-5 text-error shrink-0 mt-0.5" />
                  )}
                  <div>
                    <div
                      className={`text-sm font-medium ${
                        !s ? "text-neutral-400" : s.ok ? "text-neutral-500" : "text-error"
                      }`}
                    >
                      {label}
                    </div>
                    {s && !s.ok && (
                      <div className="text-xs text-error/80">
                        {s.reason && REASON_HINT[s.reason] ? REASON_HINT[s.reason] : s.error}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {credFail && (
          <div className="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-200 p-3 text-sm text-amber-800">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <div>
              {broker === "kiwoom" ? "키움" : "증권사"} 키 입력 필요 (NotConfigured) — .env 에 APP Key/Secret 을
              추가하면 실연동 테스트가 가능합니다. (현재는 안전 차단 상태)
            </div>
          </div>
        )}

        {result?.ok && (
          <div className="flex items-start gap-2 rounded-lg bg-success/5 border border-success/20 p-3 text-sm text-success">
            <CheckCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <div>
              모든 단계 통과 — 연결 정상.
              {result.snapshot_saved
                ? " 스냅샷 저장됨 · 대시보드에 반영됩니다."
                : " (스냅샷 저장은 생략됨)"}
            </div>
          </div>
        )}

        {err && !waiting && (
          <div className="flex items-start gap-2 rounded-lg bg-error/5 border border-error/20 p-3 text-sm text-error">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <div>{err}</div>
          </div>
        )}
      </CardBody>
    </Card>
  );
}
