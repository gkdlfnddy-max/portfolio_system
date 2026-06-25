"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { LogIn } from "lucide-react";

// 로그인 폼 — 이메일/비밀번호. 실패 시 계정 존재 여부를 노출하지 않는 일반 문구.
// 비밀번호는 state 로만 다루고 localStorage 저장·콘솔 출력 금지.
export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      const out = await res.json().catch(() => ({ ok: false }));
      if (res.ok && out.ok) {
        setPassword("");
        // reset_required(초기 admin/관리자 초기화) → 첫 로그인 시 비밀번호 변경 강제.
        if (out.reset_required || out.user?.reset_required) {
          router.replace("/security/password?first=1");
        } else {
          router.replace(next);
        }
        router.refresh();
        return;
      }
      // 계정 존재 여부 비노출 — 항상 동일한 일반 문구.
      setError("이메일 또는 비밀번호가 올바르지 않습니다.");
    } catch {
      setError("네트워크 오류가 발생했습니다. 잠시 후 다시 시도하세요.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-sm mx-auto px-5 py-16">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <LogIn className="w-5 h-5 text-primary" /> 로그인
          </CardTitle>
        </CardHeader>
        <CardBody>
          <form onSubmit={submit} className="space-y-4">
            <div>
              <Label htmlFor="login-email">ID 또는 이메일</Label>
              <Input
                id="login-email"
                type="text"
                autoComplete="username"
                autoFocus
                placeholder="admin 또는 you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="login-password">비밀번호</Label>
              <Input
                id="login-password"
                type="password"
                autoComplete="current-password"
                placeholder="비밀번호"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            {error && <div className="text-sm text-error">{error}</div>}
            <Button
              type="submit"
              size="lg"
              className="w-full"
              disabled={busy || !email.trim() || !password}
            >
              {busy ? "확인 중…" : "로그인"}
            </Button>
          </form>

          <div className="flex items-center justify-between mt-5 text-sm">
            <Link href="/reset" className="text-neutral-500 hover:text-primary">
              비밀번호 찾기
            </Link>
            <Link href="/signup" className="text-primary hover:underline">
              회원가입
            </Link>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
