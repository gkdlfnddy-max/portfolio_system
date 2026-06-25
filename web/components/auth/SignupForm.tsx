"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { UserPlus } from "lucide-react";

// 회원가입 — 이메일/비밀번호/비밀번호 확인/(선택)표시이름.
// 클라이언트에서 password != confirm 1차 검증. 비밀번호는 저장/로그 금지.
export function SignupForm() {
  const router = useRouter();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    if (!email.trim()) return setError("이메일을 입력하세요.");
    if (password.length < 8) return setError("비밀번호는 8자 이상이어야 합니다.");
    if (password !== confirm) return setError("비밀번호 확인이 일치하지 않습니다.");

    setBusy(true);
    try {
      const res = await fetch("/api/auth/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim(),
          password,
          password_confirm: confirm,
          display_name: displayName.trim() || undefined,
        }),
      });
      const out = await res.json().catch(() => ({ ok: false }));
      if (res.ok && out.ok) {
        setPassword("");
        setConfirm("");
        router.replace("/");
        router.refresh();
        return;
      }
      setError(out.error ?? "회원가입에 실패했습니다.");
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
            <UserPlus className="w-5 h-5 text-primary" /> 회원가입
          </CardTitle>
        </CardHeader>
        <CardBody>
          <form onSubmit={submit} className="space-y-4">
            <div>
              <Label htmlFor="signup-email">이메일</Label>
              <Input
                id="signup-email"
                type="email"
                autoComplete="username"
                autoFocus
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="signup-name">표시 이름 (선택)</Label>
              <Input
                id="signup-name"
                type="text"
                autoComplete="name"
                placeholder="예: 홍길동"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="signup-password">비밀번호</Label>
              <Input
                id="signup-password"
                type="password"
                autoComplete="new-password"
                placeholder="8자 이상"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="signup-confirm">비밀번호 확인</Label>
              <Input
                id="signup-confirm"
                type="password"
                autoComplete="new-password"
                placeholder="비밀번호 다시 입력"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            {error && <div className="text-sm text-error">{error}</div>}
            <Button
              type="submit"
              size="lg"
              className="w-full"
              disabled={busy || !email.trim() || !password || !confirm}
            >
              {busy ? "가입 중…" : "가입"}
            </Button>
          </form>

          <div className="text-sm mt-5 text-center text-neutral-500">
            이미 계정이 있으신가요?{" "}
            <Link href="/login" className="text-primary hover:underline">
              로그인
            </Link>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
