import { notFound } from "next/navigation";

// 계좌 하위 화면(/accounts/[id]/*) 공통 래퍼.
// 계좌별 PIN 게이트 제거(CEO 결정) — 접근 통제는 로그인 + RBAC(서버 requireLoginAndAccount).
// (live 주문 hard lock 은 별개로 유지)
export default function AccountLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: { id: string };
}) {
  const id = parseInt(params.id, 10);
  if (!Number.isInteger(id) || id < 1) notFound();
  return <>{children}</>;
}
