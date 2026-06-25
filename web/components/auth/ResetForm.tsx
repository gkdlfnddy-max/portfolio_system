"use client";

import { useState } from "react";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { KeyRound, MailQuestion } from "lucide-react";

// 비밀번호 찾기(MVP) — 메일 발송 자동화가 아직 없으므로 관리자 초기화 요청 안내.
// reset-request 는 호출하되, 응답에 관계없이 계정 존재 여부를 노출하지 않는 동일 문구를 보여준다.
export function ResetForm() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      await fetch("/api/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "reset_request", email: email.trim() }),
      });
    } catch {
      // 네트워크 오류여도 계정 존재 여부를 추론할 수 없도록 동일 안내를 노출한다.
    } finally {
      setBusy(false);
      setSent(true);
    }
  }

  return (
    <div className="max-w-sm mx-auto px-5 py-16">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="w-5 h-5 text-primary" /> 비밀번호 찾기
          </CardTitle>
        </CardHeader>
        <CardBody>
          {sent ? (
            <div className="space-y-4">
              <div className="flex items-start gap-2 text-sm text-neutral-700">
                <MailQuestion className="w-5 h-5 text-primary shrink-0 mt-0.5" />
                <p>
                  입력하신 이메일이 등록되어 있다면 안내가 전달됩니다.
                  <br />
                  <b>메일 발송이 아직 없으면 관리자에게 비밀번호 초기화를 요청하세요.</b>
                </p>
              </div>
              <Link href="/login" className="block">
                <Button variant="outline" className="w-full">
                  로그인으로 돌아가기
                </Button>
              </Link>
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-4">
              <p className="text-sm text-neutral-500">
                가입하신 이메일을 입력하세요. MVP 단계에서는 자동 메일 발송 대신
                관리자가 비밀번호를 초기화합니다.
              </p>
              <div>
                <Label htmlFor="reset-email">이메일</Label>
                <Input
                  id="reset-email"
                  type="email"
                  autoComplete="username"
                  autoFocus
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <Button type="submit" size="lg" className="w-full" disabled={busy || !email.trim()}>
                {busy ? "요청 중…" : "초기화 요청"}
              </Button>
              <div className="text-sm text-center text-neutral-500">
                <Link href="/login" className="text-primary hover:underline">
                  로그인으로 돌아가기
                </Link>
              </div>
            </form>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
