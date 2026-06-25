"use client";

// 키움증권 계좌 연결 폼 (멀티 브로커). KIS 폼과 *분리* — credential 혼용 금지.
// 입력값은 /api/accounts (broker="kiwoom") 로만 전송 → 서버가 .env 의 KIWOOM_* 에 기록.
// 프론트에서 키움 API 를 직접 호출하지 않는다(웹 조회 전용 원칙).
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { ShieldCheck, Lock, KeyRound } from "lucide-react";
import { LiveModeConfirm } from "@/components/LiveModeConfirm";

const MODES = [
  { v: "paper", label: "모의투자 (추천)", desc: "가상 자금 · 실제 돈 X" },
  { v: "live", label: "실전", desc: "실제 계좌 · 추가 확인 필요" },
] as const;

type Form = {
  alias: string;
  mode: "paper" | "live";
  appKey: string;
  appSecret: string;
  accountNo: string;
};

function FieldMsg({ id, msg }: { id: string; msg?: string }) {
  if (!msg) return null;
  return (
    <p id={id} className="text-sm text-error mt-1">
      {msg}
    </p>
  );
}

export function BrokerKiwoomForm() {
  const router = useRouter();
  const [form, setForm] = useState<Form>({
    alias: "",
    mode: "paper",
    appKey: "",
    appSecret: "",
    accountNo: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [errors, setErrors] = useState<Partial<Record<keyof Form, string>>>({});
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [showLiveConfirm, setShowLiveConfirm] = useState(false);

  const set = (k: keyof Form, v: string) => {
    setForm((f) => ({ ...f, [k]: v }));
    if (errors[k]) setErrors((e) => ({ ...e, [k]: undefined }));
  };

  const pickMode = (m: "paper" | "live") => {
    if (m === "live" && form.mode !== "live") setShowLiveConfirm(true);
    else set("mode", m);
  };

  const validate = (): boolean => {
    const e: Partial<Record<keyof Form, string>> = {};
    if (!form.alias.trim()) e.alias = "계좌 별칭은 필수입니다";
    if (!form.appKey.trim()) e.appKey = "APP Key는 필수입니다";
    else if (form.appKey.trim().length < 8) e.appKey = "APP Key가 너무 짧습니다";
    if (!form.appSecret.trim()) e.appSecret = "APP Secret은 필수입니다";
    else if (form.appSecret.trim().length < 16) e.appSecret = "APP Secret이 너무 짧습니다";
    if (!form.accountNo.trim()) e.accountNo = "계좌번호는 필수입니다";
    setErrors(e);
    return Object.keys(e).length === 0;
  };

  const submit = async () => {
    setGlobalError(null);
    if (!validate()) return;
    setSubmitting(true);
    try {
      const res = await fetch("/api/accounts", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ broker: "kiwoom", ...form }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error ?? "연결 실패");
      router.push(`/accounts/${data.index}`);
      router.refresh();
    } catch (e: any) {
      setGlobalError(e?.message ?? "연결 실패");
      setSubmitting(false);
    }
  };

  return (
    <>
      {showLiveConfirm && (
        <LiveModeConfirm
          onConfirm={() => {
            set("mode", "live");
            setShowLiveConfirm(false);
          }}
          onCancel={() => setShowLiveConfirm(false)}
        />
      )}

      <Card>
        <CardHeader>
          <CardTitle>키움증권 정보</CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          <div className="flex items-start gap-2 rounded-lg border border-primary-100 bg-primary-50 p-3 text-xs text-primary-700">
            <KeyRound className="w-4 h-4 mt-0.5 shrink-0" />
            <span>
              키움 REST 앱키 발급 후 입력하세요(모의투자 우선). 키 없이도 안전하게 저장만 됩니다 —
              잔고/보유종목 동기화는 키가 있을 때 백엔드 어댑터가 연결합니다.
            </span>
          </div>

          <div>
            <Label htmlFor="kw-alias">계좌 별칭</Label>
            <Input
              id="kw-alias"
              placeholder="예: 내 키움 모의투자 계좌"
              value={form.alias}
              onChange={(e) => set("alias", e.target.value)}
              aria-invalid={!!errors.alias}
              aria-describedby={errors.alias ? "kw-err-alias" : undefined}
            />
            <FieldMsg id="kw-err-alias" msg={errors.alias} />
          </div>

          <div>
            <Label>모드</Label>
            <div className="flex gap-2 mt-1">
              {MODES.map((m) => (
                <button
                  key={m.v}
                  type="button"
                  onClick={() => pickMode(m.v)}
                  className={`flex-1 text-left rounded-xl border p-3 transition ${
                    form.mode === m.v ? "border-primary bg-primary-50" : "border-neutral-200 hover:border-primary-100"
                  }`}
                >
                  <div className="text-sm font-semibold text-neutral-900">{m.label}</div>
                  <div className="text-[11px] text-neutral-400">{m.desc}</div>
                </button>
              ))}
            </div>
            {form.mode === "live" && (
              <p className="text-xs text-error mt-1.5 flex items-center gap-1">
                <Lock className="w-3.5 h-3.5" /> 실전 모드. 주문은 여전히 승인 후에만 나갑니다.
              </p>
            )}
          </div>

          <div>
            <Label htmlFor="kw-appKey">APP Key</Label>
            <Input
              id="kw-appKey"
              placeholder="키움 REST 앱 키"
              value={form.appKey}
              onChange={(e) => set("appKey", e.target.value)}
              aria-invalid={!!errors.appKey}
              aria-describedby={errors.appKey ? "kw-err-appKey" : undefined}
            />
            <FieldMsg id="kw-err-appKey" msg={errors.appKey} />
          </div>

          <div>
            <Label htmlFor="kw-appSecret">APP Secret</Label>
            <Input
              id="kw-appSecret"
              type="password"
              placeholder="키움 REST 앱 시크릿"
              value={form.appSecret}
              onChange={(e) => set("appSecret", e.target.value)}
              aria-invalid={!!errors.appSecret}
              aria-describedby={errors.appSecret ? "kw-err-appSecret" : undefined}
            />
            <FieldMsg id="kw-err-appSecret" msg={errors.appSecret} />
          </div>

          <div>
            <Label htmlFor="kw-accountNo">계좌번호</Label>
            <Input
              id="kw-accountNo"
              placeholder="키움 계좌번호"
              value={form.accountNo}
              onChange={(e) => set("accountNo", e.target.value)}
              aria-invalid={!!errors.accountNo}
              aria-describedby={errors.accountNo ? "kw-err-accountNo" : undefined}
            />
            <FieldMsg id="kw-err-accountNo" msg={errors.accountNo} />
          </div>

          {globalError && (
            <div className="bg-error/10 text-error p-3 rounded-lg text-sm border border-error/20">{globalError}</div>
          )}

          <div className="flex items-center gap-3 pt-1">
            <Button onClick={submit} disabled={submitting} size="lg">
              {submitting ? "저장 중…" : "이 키움 계좌 연결하기"}
            </Button>
            <span className="text-[11px] text-neutral-400 flex items-center gap-1">
              <ShieldCheck className="w-3.5 h-3.5" /> 입력값은 .env(KIWOOM_*)에만 저장 · 화면엔 다시 노출 안 됨
            </span>
          </div>
        </CardBody>
      </Card>
    </>
  );
}
