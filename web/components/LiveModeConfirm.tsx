"use client";

import { useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { ShieldAlert } from "lucide-react";

// 실전(live) 전환 전 강제 위험 인식 체크리스트.
// 벤치마크: Fidelity GO / 증권앱 실전전환 경고 / Stripe sandbox→prod.
const CHECKS = [
  { key: "real_money", label: "이 계좌의 실제 자금이 투자에 사용됨에 동의합니다." },
  { key: "losses", label: "손실이 발생할 수 있으며 그 책임은 본인에게 있음을 인정합니다." },
  { key: "paper_first", label: "모의투자에서 충분히 검증한 뒤 실전으로 전환합니다." },
] as const;

export function LiveModeConfirm({
  onConfirm,
  onCancel,
}: {
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const allChecked = CHECKS.every((c) => checked[c.key]);

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-50">
      <Card className="max-w-md w-full">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlert className="w-5 h-5 text-error" /> 실전(live) 전환 확인
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          <div className="bg-error/5 border border-error/20 rounded-lg p-3 text-sm text-error">
            실전 모드는 <b>실제 자금</b>이 투입됩니다. 주문은 여전히 리스크 점검 + 승인 후에만 나가지만,
            모의투자(paper)로 먼저 검증하길 강력히 권장합니다.
          </div>
          <div className="space-y-2">
            {CHECKS.map((c) => (
              <label key={c.key} className="flex items-start gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!checked[c.key]}
                  onChange={(e) => setChecked((s) => ({ ...s, [c.key]: e.target.checked }))}
                  className="mt-1 accent-error"
                />
                <span className="text-sm text-neutral-700">{c.label}</span>
              </label>
            ))}
          </div>
          <div className="flex gap-2 pt-1">
            <Button variant="outline" onClick={onCancel} className="flex-1">
              모의투자로
            </Button>
            <Button
              onClick={onConfirm}
              disabled={!allChecked}
              className={`flex-1 ${allChecked ? "" : "opacity-50 cursor-not-allowed"}`}
            >
              실전 활성화
            </Button>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
