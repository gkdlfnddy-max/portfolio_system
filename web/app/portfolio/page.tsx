import { redirect } from "next/navigation";

// 의사결정은 계좌 종속(/accounts/[id]/portfolio)으로 이전됨.
// 제네릭 mock 화면은 폐기 — 홈에서 계좌를 선택해 진입.
export default function PortfolioRedirect() {
  redirect("/");
}
