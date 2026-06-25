// Portfolio OS — 포트폴리오 관리자(단일 agent broker-chief) 식별/설명 (제품 config).
// 운영 데이터(계좌/잔고)는 여기 두지 않는다 — DB(portfolioDb)에서 조회한다.
export const MANAGER = {
  slug: "broker-chief",
  name: "AI 포트폴리오 관리자",
  emoji: "🧑‍💼",
  title: "포트폴리오 관리자",
  oneLiner: "계좌를 맡기시면 한국투자증권에 연결해, 제안 + 승인 방식으로 관리합니다.",
  bullets: [
    "한국투자증권(KIS) 계좌 연결 — 모의투자 우선",
    "투자 컨셉 → 목표 비중 → 리밸런싱 제안",
    "리스크 게이트 통과 + 사장님 승인 후에만 주문",
  ],
};
