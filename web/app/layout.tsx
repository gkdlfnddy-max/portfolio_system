import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { LoginGate } from "@/components/LoginGate";

export const metadata: Metadata = {
  title: "Portfolio OS — 포트폴리오 관리자에게 계좌 관리 맡기기",
  description:
    "한국투자증권 계좌를 AI 포트폴리오 관리자에게. 모의투자 우선, 리스크 점검 + 사람 승인 후에만 주문.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ko">
      <body>
        <Nav />
        <main className="min-h-screen pb-24">
          {/* CEO 보안 모델 = 로그인 + RBAC(서버 enforce). PIN 은 전면 제거(CEO 결정).
              접근 통제는 LoginGate(여기) + 라우트의 requireLoginAndAccount(RBAC)가 담당. */}
          <LoginGate>{children}</LoginGate>
        </main>
        <footer className="border-t border-neutral-200 py-10 text-center text-sm text-neutral-400">
          Portfolio OS · © 2026 · 모의투자 우선 · 투자 책임은 본인에게 있습니다
        </footer>
      </body>
    </html>
  );
}
