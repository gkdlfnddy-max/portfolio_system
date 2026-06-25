"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { LogIn, LogOut, KeyRound, ShieldAlert, Users, UserCircle } from "lucide-react";

// 헤더 인증 메뉴 — 로그인 상태/role 에 따라 표시.
//  · 미로그인: "로그인" 링크
//  · 로그인: "비밀번호 변경" / (admin) "관리자" / "로그아웃"
// 로그인·role 판별은 GET /api/auth/me 로 한다(role 의 정규 출처).
// 표시 보조일 뿐 — 실제 차단은 서버 authz(로그인 + RBAC).
type State = { authed: boolean; isAdmin: boolean; resetRequired: boolean; name?: string } | null;

export function AuthMenu() {
  const pathname = usePathname();
  const router = useRouter();
  const [s, setS] = useState<State>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch("/api/auth/me", { cache: "no-store" });
        if (!alive) return;
        const j = await res.json().catch(() => ({ user: null }));
        const u = j?.user ?? null;
        if (!u) {
          setS({ authed: false, isAdmin: false, resetRequired: false });
          return;
        }
        setS({
          authed: true,
          isAdmin: u.role === "admin",
          resetRequired: u.reset_required === true,
          name: u.login_id || u.display_name || u.email,
        });
      } catch {
        if (alive) setS({ authed: false, isAdmin: false, resetRequired: false });
      }
    })();
    return () => {
      alive = false;
    };
    // 경로가 바뀌면(로그인/로그아웃/비번변경 후) 상태를 다시 확인.
  }, [pathname]);

  async function logout() {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch {
      /* noop */
    }
    setS({ authed: false, isAdmin: false, resetRequired: false });
    router.replace("/login");
    router.refresh();
  }

  if (s === null) return null;

  if (!s.authed) {
    return (
      <Link
        href="/login"
        className="px-3 py-1.5 text-sm text-neutral-700 hover:text-primary hover:bg-neutral-50 rounded-lg flex items-center gap-1"
      >
        <LogIn className="w-4 h-4" /> 로그인
      </Link>
    );
  }

  // 초기 비밀번호 설정 전(reset_required): 비번 변경/로그아웃만 노출(다른 진입 차단은 LoginGate).
  if (s.resetRequired) {
    return (
      <>
        <Link
          href="/security/password?first=1"
          className="px-3 py-1.5 text-sm text-warning hover:bg-neutral-50 rounded-lg flex items-center gap-1"
        >
          <KeyRound className="w-4 h-4" /> 비밀번호 설정
        </Link>
        <button
          onClick={logout}
          className="px-3 py-1.5 text-sm text-neutral-500 hover:text-error hover:bg-neutral-50 rounded-lg flex items-center gap-1"
        >
          <LogOut className="w-4 h-4" /> 로그아웃
        </button>
      </>
    );
  }

  return (
    <>
      <span className="px-2 py-1 text-sm text-neutral-700 flex items-center gap-1" title={s.isAdmin ? "관리자로 로그인됨" : "로그인됨"}>
        <UserCircle className="w-4 h-4 text-primary" />
        <b className="font-medium">{s.name}</b>
        {s.isAdmin && <span className="text-[10px] rounded bg-primary-50 text-primary-700 px-1.5 py-0.5">관리자</span>}
      </span>
      {s.isAdmin && (
        <>
          <Link
            href="/admin"
            className="px-3 py-1.5 text-sm text-neutral-700 hover:text-primary hover:bg-neutral-50 rounded-lg flex items-center gap-1"
            title="사용자 관리 · 계좌 권한 · 인증 이벤트"
          >
            <ShieldAlert className="w-4 h-4" /> 관리자
          </Link>
          <Link
            href="/admin/accounts"
            className="px-3 py-1.5 text-sm text-neutral-700 hover:text-primary hover:bg-neutral-50 rounded-lg flex items-center gap-1"
            title="모든 사용자의 계좌 현황"
          >
            <Users className="w-4 h-4" /> 전체 계좌
          </Link>
        </>
      )}
      <Link
        href="/security/password"
        className="px-3 py-1.5 text-sm text-neutral-700 hover:text-primary hover:bg-neutral-50 rounded-lg flex items-center gap-1"
      >
        <KeyRound className="w-4 h-4" /> 비밀번호 변경
      </Link>
      <button
        onClick={logout}
        className="px-3 py-1.5 text-sm text-neutral-500 hover:text-error hover:bg-neutral-50 rounded-lg flex items-center gap-1"
      >
        <LogOut className="w-4 h-4" /> 로그아웃
      </button>
    </>
  );
}
