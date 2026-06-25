import { PasswordChangeForm } from "@/components/auth/PasswordChangeForm";

export const dynamic = "force-dynamic";

// 계정 비밀번호 변경(로그인 비밀번호). PIN 은 전면 제거됨 — 로그인 비번만 운영.
// ?first=1 → 초기/임시 비밀번호 설정 흐름(reset_required). LoginGate 가 여기로 보낸다.
//   (LoginGate 의 redirect 로직은 건드리지 않는다 — 이 화면은 first 플래그만 읽어 폼 모드를 바꾼다.)
export default function PasswordPage({
  searchParams,
}: {
  searchParams?: { first?: string };
}) {
  const firstLogin = searchParams?.first === "1";
  return (
    <div className="max-w-sm mx-auto px-5 py-16 space-y-4">
      {firstLogin && (
        <div className="text-center space-y-1">
          <h1 className="text-xl font-bold text-neutral-900">환영합니다</h1>
          <p className="text-sm text-neutral-500">
            첫 로그인입니다. 임시 비밀번호를 새 비밀번호로 바꾸면 모든 기능을 사용할 수 있습니다.
          </p>
        </div>
      )}
      <PasswordChangeForm firstLogin={firstLogin} />
    </div>
  );
}
