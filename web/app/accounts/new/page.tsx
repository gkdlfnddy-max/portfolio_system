"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { ShieldCheck, ArrowLeft, Lock } from "lucide-react";
import { LiveModeConfirm } from "@/components/LiveModeConfirm";
import { BrokerKiwoomForm } from "@/components/account/BrokerKiwoomForm";
import { accountFormSchema, toFieldErrors, type AccountFormData, type FieldErrors } from "@/lib/forms/accountSchema";

const MODES = [
  { v: "paper", label: "모의투자 (추천)", desc: "가상 자금 · 실제 돈 X" },
  { v: "live", label: "실전", desc: "실제 계좌 · 추가 확인 필요" },
] as const;

function FieldMsg({ id, msg }: { id: string; msg?: string }) {
  if (!msg) return null;
  return (
    <p id={id} className="text-sm text-error mt-1">
      {msg}
    </p>
  );
}

export default function NewAccountPage() {
  const router = useRouter();
  const [form, setForm] = useState<AccountFormData>({
    alias: "",
    mode: "paper",
    appKey: "",
    appSecret: "",
    accountNo: "",
    productCode: "01",
  });
  const [submitting, setSubmitting] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [showLiveConfirm, setShowLiveConfirm] = useState(false);
  const [broker, setBroker] = useState<string>("kis");  // 멀티 브로커: 증권사 선택

  const set = (k: keyof AccountFormData, v: string) => {
    setForm((f) => ({ ...f, [k]: v }));
    if (errors[k]) setErrors((e) => ({ ...e, [k]: undefined }));
  };

  const pickMode = (m: "paper" | "live") => {
    if (m === "live" && form.mode !== "live") {
      setShowLiveConfirm(true); // 실전은 체크리스트 통과 후에만
    } else {
      set("mode", m);
    }
  };

  const submit = async () => {
    setGlobalError(null);
    const parsed = accountFormSchema.safeParse(form);
    if (!parsed.success) {
      setErrors(toFieldErrors(parsed.error));
      return;
    }
    setErrors({});
    setSubmitting(true);
    try {
      const res = await fetch("/api/accounts", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(parsed.data),
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
    <div className="max-w-xl mx-auto px-5 py-10 space-y-5">
      {showLiveConfirm && (
        <LiveModeConfirm
          onConfirm={() => {
            set("mode", "live");
            setShowLiveConfirm(false);
          }}
          onCancel={() => setShowLiveConfirm(false)}
        />
      )}

      <Link href="/" className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 홈으로
      </Link>

      <div>
        <h1 className="text-2xl font-bold text-neutral-900">계좌 연결</h1>
        <p className="text-sm text-neutral-500 mt-1">
          한국투자증권 또는 키움증권 정보를 입력하면 <b>이 PC의 .env 파일</b>에 계좌별로 안전하게 저장됩니다.
          여러 계좌를 추가할 수 있어요.
        </p>
      </div>

      {/* 증권사 선택 (멀티 브로커) — KIS 전용 코드 아님, broker 별 adapter */}
      <Card>
        <CardHeader><CardTitle>증권사 선택</CardTitle></CardHeader>
        <CardBody>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            {[["kis", "한국투자증권"], ["kiwoom", "키움증권"], ["manual", "수동 입력"], ["paper", "Paper"]].map(([v, label]) => (
              <button key={v} type="button" onClick={() => setBroker(v)}
                className={`rounded-xl border p-3 text-sm transition ${broker === v ? "border-primary bg-primary-50 text-primary-700" : "border-neutral-200 text-neutral-600 hover:border-primary-100"}`}>
                {label}
              </button>
            ))}
          </div>
          {broker !== "kis" && broker !== "kiwoom" && (
            <p className="text-xs text-neutral-500 mt-3">
              준비 중입니다. 현재는 한국투자증권 · 키움증권 연결을 지원합니다.
            </p>
          )}
        </CardBody>
      </Card>

      {/* 키움증권 선택 시 키움 전용 폼 (KIS 와 자격증명 분리) */}
      {broker === "kiwoom" && <BrokerKiwoomForm />}

      {broker === "kis" && (
      <Card>
        <CardHeader>
          <CardTitle>한국투자증권 정보</CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          <div>
            <Label htmlFor="alias">계좌 별칭</Label>
            <Input
              id="alias"
              placeholder="예: 내 모의투자 계좌"
              value={form.alias}
              onChange={(e) => set("alias", e.target.value)}
              aria-invalid={!!errors.alias}
              aria-describedby={errors.alias ? "err-alias" : undefined}
            />
            <FieldMsg id="err-alias" msg={errors.alias} />
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
                    form.mode === m.v
                      ? "border-primary bg-primary-50"
                      : "border-neutral-200 hover:border-primary-100"
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
            <Label htmlFor="appKey">APP Key</Label>
            <Input
              id="appKey"
              placeholder="KIS Developers 앱 키"
              value={form.appKey}
              onChange={(e) => set("appKey", e.target.value)}
              aria-invalid={!!errors.appKey}
              aria-describedby={errors.appKey ? "err-appKey" : undefined}
            />
            <FieldMsg id="err-appKey" msg={errors.appKey} />
          </div>

          <div>
            <Label htmlFor="appSecret">APP Secret</Label>
            <Input
              id="appSecret"
              type="password"
              placeholder="KIS Developers 앱 시크릿"
              value={form.appSecret}
              onChange={(e) => set("appSecret", e.target.value)}
              aria-invalid={!!errors.appSecret}
              aria-describedby={errors.appSecret ? "err-appSecret" : undefined}
            />
            <FieldMsg id="err-appSecret" msg={errors.appSecret} />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2">
              <Label htmlFor="accountNo">계좌번호 (앞 8자리)</Label>
              <Input
                id="accountNo"
                placeholder="예: 50071023"
                value={form.accountNo}
                onChange={(e) => set("accountNo", e.target.value)}
                aria-invalid={!!errors.accountNo}
                aria-describedby={errors.accountNo ? "err-accountNo" : undefined}
              />
              <FieldMsg id="err-accountNo" msg={errors.accountNo} />
            </div>
            <div>
              <Label htmlFor="productCode">상품코드</Label>
              <Input
                id="productCode"
                placeholder="01"
                value={form.productCode}
                onChange={(e) => set("productCode", e.target.value)}
                aria-invalid={!!errors.productCode}
                aria-describedby={errors.productCode ? "err-productCode" : undefined}
              />
              <FieldMsg id="err-productCode" msg={errors.productCode} />
            </div>
          </div>

          {globalError && (
            <div className="bg-error/10 text-error p-3 rounded-lg text-sm border border-error/20">
              {globalError}
            </div>
          )}

          <div className="flex items-center gap-3 pt-1">
            <Button onClick={submit} disabled={submitting} size="lg">
              {submitting ? "저장 중…" : "이 계좌 연결하기"}
            </Button>
            <span className="text-[11px] text-neutral-400 flex items-center gap-1">
              <ShieldCheck className="w-3.5 h-3.5" /> 입력값은 .env 에만 저장 · 화면엔 다시 노출 안 됨
            </span>
          </div>
        </CardBody>
      </Card>
      )}

      {broker === "kis" && (
      <p className="text-xs text-neutral-400">
        APP Key/Secret 발급 방법을 모르시면: <code>docs/portfolio/kis_onboarding.md</code> 참고.
        저장 후 연결 테스트: <code>python -m main_mission.portfolio_os.broker.kis_check</code>
      </p>
      )}
    </div>
  );
}
