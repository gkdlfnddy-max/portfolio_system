"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

// 로그인 게이트(클라이언트) — 보호 경로 진입 시 **로그인 여부만** 확인한다.
// 로그인 판별은 GET /api/auth/me (user!=null) 로 한다.
// 미로그인이면 /login?next=… 로 보낸다. PIN 은 전면 제거됨(접근 통제는 로그인 + RBAC).
// 주의: 표시 보조일 뿐, 실제 차단(authz)은 서버(RBAC 가드)가 한다.
//
// 게이트 대상 = 보호 경로(홈/계좌/관리자/포트폴리오 등). 로그인/회원가입/비번찾기 화면은 면제.
const PUBLIC_PREFIXES = ["/login", "/signup", "/reset"];

function isPublic(pathname: string): boolean {
  return PUBLIC_PREFIXES.some((p) => pathname === p || pathname.startsWith(p + "/"));
}

export function LoginGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const exempt = isPublic(pathname);
  const [ready, setReady] = useState(exempt);

  useEffect(() => {
    if (exempt) {
      setReady(true);
      return;
    }
    let alive = true;
    setReady(false);
    (async () => {
      try {
        const res = await fetch("/api/auth/me", { cache: "no-store" });
        if (!alive) return;
        const j = await res.json().catch(() => ({ user: null }));
        if (!j?.user) {
          // 미로그인(user=null) 만 /login 으로.
          const next = encodeURIComponent(pathname || "/");
          router.replace(`/login?next=${next}`);
          return;
        }
        // reset_required(초기 비번) → 비번 변경 외 접근 차단. 비번 화면은 통과시킨다.
        if (j.user.reset_required && !(pathname || "").startsWith("/security/password")) {
          router.replace("/security/password?first=1");
          return;
        }
        setReady(true);
      } catch {
        if (alive) setReady(true); // 네트워크 오류 시 화면은 보여주되 서버 가드가 실제 방어.
      }
    })();
    return () => {
      alive = false;
    };
  }, [pathname, exempt, router]);

  if (exempt) return <>{children}</>;
  if (!ready) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center text-sm text-neutral-400">
        로그인 상태 확인 중…
      </div>
    );
  }
  return <>{children}</>;
}
