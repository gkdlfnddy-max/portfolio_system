"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { KeyRound, ShieldAlert } from "lucide-react";

// 비밀번호 변경/초기설정 — POST /api/auth/password.
//   · 일반 변경(change)   : 현재 비번 검증 후 신규 비번.
//   · 초기 설정(first_login): reset_required 사용자(임시 비번)가 현재 비번 없이 신규 설정.
// 둘 다 성공 시 서버가 전 세션을 철회한다 → 재로그인 필요(안내 후 /login 이동).
// 비밀번호는 state 로만 다루고 저장/로그 금지.
// 응답코드 매핑: docs/portfolio/auth_response_codes.md.
const CODE_MESSAGES: Record<string, string> = {
  BAD_CURRENT: "현재 비밀번호가 올바르지 않습니다.",
  WEAK_PASSWORD: "비밀번호는 8자 이상이어야 합니다.",
  NOT_REQUIRED: "초기 설정 대상이 아닙니다. 일반 비밀번호 변경을 이용하세요.",
  NOT_FOUND: "사용자를 찾을 수 없습니다. 다시 로그인하세요.",
  UNAUTHENTICATED: "로그인이 필요합니다.",
};

function messageFor(code: string | undefined, fallback: string): string {
  if (code && CODE_MESSAGES[code]) return CODE_MESSAGES[code];
  return fallback;
}

export function PasswordChangeForm({ firstLogin = false }: { firstLogin?: boolean }) {
  const router = useRouter();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  // 변경 성공 → 전 세션 철회됨. 잠깐 안내 후 /login 으로 보낸다.
  useEffect(() => {
    if (!done) return;
    const t = setTimeout(() => {
      router.replace("/login");
      router.refresh();
    }, 1800);
    return () => clearTimeout(t);
  }, [done, router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    if (next.length < 8) return setError("새 비밀번호는 8자 이상이어야 합니다.");
    if (next !== confirm) return setError("새 비밀번호 확인이 일치하지 않습니다.");
    if (!firstLogin && next === current)
      return setError("새 비밀번호가 현재 비밀번호와 같습니다.");

    setBusy(true);
    try {
      const body = firstLogin
        ? { action: "first_login", next }
        : { action: "change", current, next };
      const res = await fetch("/api/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const out = await res.json().catch(() => ({ ok: false }));
      if (res.ok && out.ok) {
        setDone(true);
        setCurrent("");
        setNext("");
        setConfirm("");
        return;
      }
      setError(messageFor(out.code, out.error ?? "비밀번호 변경에 실패했습니다."));
    } catch {
      setError("네트워크 오류가 발생했습니다. 잠시 후 다시 시도하세요.");
    } finally {
      setBusy(false);
    }
  }

  const title = firstLogin ? "초기 비밀번호 설정" : "비밀번호 변경";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="w-5 h-5 text-primary" /> {title}
        </CardTitle>
      </CardHeader>
      <CardBody>
        {firstLogin && !done && (
          <div className="mb-4 flex items-start gap-2 rounded-lg bg-warning/10 px-3 py-2 text-sm text-warning">
            <ShieldAlert className="w-4 h-4 mt-0.5 shrink-0" />
            <span>
              임시 비밀번호로 로그인했습니다. 계속하려면 먼저 새 비밀번호를 설정하세요.
            </span>
          </div>
        )}
        {done ? (
          <div className="space-y-2 text-sm">
            <div className="text-accent">비밀번호가 변경되었습니다.</div>
            <div className="text-neutral-500">
              보안을 위해 모든 기기에서 로그아웃됩니다. 새 비밀번호로 다시 로그인하세요…
            </div>
          </div>
        ) : (
          <form onSubmit={submit} className="space-y-4">
            {!firstLogin && (
              <div>
                <Label htmlFor="pw-current">현재 비밀번호</Label>
                <Input
                  id="pw-current"
                  type="password"
                  autoComplete="current-password"
                  value={current}
                  onChange={(e) => setCurrent(e.target.value)}
                />
              </div>
            )}
            <div>
              <Label htmlFor="pw-new">새 비밀번호</Label>
              <Input
                id="pw-new"
                type="password"
                autoComplete="new-password"
                placeholder="8자 이상"
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="pw-confirm">새 비밀번호 확인</Label>
              <Input
                id="pw-confirm"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            {error && <div className="text-sm text-error">{error}</div>}
            <Button
              type="submit"
              className="w-full"
              disabled={busy || (!firstLogin && !current) || !next || !confirm}
            >
              {busy ? "처리 중…" : firstLogin ? "비밀번호 설정" : "변경"}
            </Button>
          </form>
        )}
      </CardBody>
    </Card>
  );
}
