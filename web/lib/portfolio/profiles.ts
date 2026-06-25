// Portfolio OS — 투자자 프로필 (멀티유저, mock)
// 벤치마크 차용: 1 user = 1 컨셉 프리셋(핀트), anchor=개인별 기본배분(Wealthfront/BlackRock),
// per-user 제약(restricted securities, Aladdin/컴플라이언스).
// 사람마다 anchor·컨셉·리스크 성향·매매제한이 다르게 유지된다.
import type { InvestorProfile } from "./types";

// 엔진 mock 기준일 (매매제한 기간 판정용)
export const TODAY = "2026-06-19";

export const PROFILES: InvestorProfile[] = [
  {
    id: "ceo_kim",
    name: "김대표 (CEO)",
    persona: "성장형",
    preset: "국내+미국 성장 혼합",
    anchor: { samsung: 10, hynix: 7, samsung_sdi: 8, battery_etf: 8, sp500_etf: 18, aapl: 10, bio_etf: 9, inverse_etf: 0, cash: 30 },
    current: { samsung: 16, hynix: 12, samsung_sdi: 10, battery_etf: 6, sp500_etf: 18, aapl: 11, bio_etf: 7, inverse_etf: 0, cash: 20 },
    defaultConcept: "반도체 과열, 배터리 장기 회복, 바이오 분산, 현금 30%, 숏은 보험 수준",
    riskTolerance: "moderate",
    limitsOverride: { cashMinPct: 10, singleNameMaxPct: 15 },
    restricted: [
      {
        key: "samsung_sdi",
        reason: "esop_lockup",
        label: "삼성SDI 우리사주 — 약 1년 매매제한(보호예수). 종료일 CEO 확인 필요",
        from: "2026-06-19",
        until: "2027-06-19",
        scope: "all",
        hard: true,
      },
    ],
    signals: { overheating: 72, crashRisk: 45, eventRisk: 38 },
    totalValueKrw: 100_000_000,
    drawdownPct: 4.2,
  },
  {
    id: "park",
    name: "박부장",
    persona: "안정 배당형",
    preset: "국내 배당·방어 중심",
    anchor: { samsung: 10, hynix: 6, samsung_sdi: 0, battery_etf: 6, sp500_etf: 16, aapl: 6, bio_etf: 16, inverse_etf: 0, cash: 40 },
    current: { samsung: 12, hynix: 8, samsung_sdi: 0, battery_etf: 5, sp500_etf: 18, aapl: 7, bio_etf: 14, inverse_etf: 0, cash: 36 },
    defaultConcept: "안정 배당 중심, 반도체 비중 축소, 현금 40% 유지",
    riskTolerance: "conservative",
    limitsOverride: { cashMinPct: 20, singleNameMaxPct: 12 },
    restricted: [],
    signals: { overheating: 50, crashRisk: 40, eventRisk: 30 },
    totalValueKrw: 300_000_000,
    drawdownPct: 2.1,
  },
  {
    id: "lee",
    name: "이주임",
    persona: "글로벌 공격형",
    preset: "미국·글로벌 적극",
    anchor: { samsung: 8, hynix: 8, samsung_sdi: 0, battery_etf: 6, sp500_etf: 30, aapl: 18, bio_etf: 8, inverse_etf: 0, cash: 22 },
    current: { samsung: 9, hynix: 10, samsung_sdi: 0, battery_etf: 5, sp500_etf: 28, aapl: 20, bio_etf: 6, inverse_etf: 0, cash: 22 },
    defaultConcept: "미국 대형주 비중 확대, 반도체 약간 축소, 급락 위험 대비 헷지 강화, 현금 15%",
    riskTolerance: "aggressive",
    limitsOverride: { cashMinPct: 10, singleNameMaxPct: 20 },
    restricted: [],
    signals: { overheating: 65, crashRisk: 78, eventRisk: 55 },
    totalValueKrw: 50_000_000,
    drawdownPct: 6.8,
  },
];

export const PROFILE_BY_ID: Record<string, InvestorProfile> = Object.fromEntries(
  PROFILES.map((p) => [p.id, p]),
);
