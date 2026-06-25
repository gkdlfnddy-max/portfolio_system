// Portfolio OS — 도메인 타입 (mock/paper 모드 전용, KIS live 없음)
// 순수 엔진(engine.ts)이 사용. UI 는 이 타입만 의존.

export type AssetClass =
  | "domestic_stock"
  | "domestic_etf"
  | "us_stock"
  | "us_etf"
  | "cash";

export type Sector =
  | "반도체"
  | "배터리"
  | "바이오"
  | "미국대형"
  | "헷지"
  | "현금";

export interface Instrument {
  key: string;            // 'samsung', 'cash' ...
  label: string;          // '삼성전자'
  ticker: string;         // '005930'
  market: "KRX" | "NASDAQ" | "NYSE" | "CASH";
  assetClass: AssetClass;
  sector: Sector;
  currency: "KRW" | "USD";
  isLeveraged: boolean;
  isInverse: boolean;
}

// 비중 맵: key -> percent (합 100)
export type Weights = Record<string, number>;

export interface PositionLine {
  key: string;
  pct: number;            // 현재 비중 %
  valueKrw: number;       // 평가액 (mock)
}

export type Confidence = "low" | "med" | "high";
export type VariantId = "conservative" | "base" | "aggressive";

// 컨셉 파싱 결과 — 하나의 tilt
export interface Tilt {
  sector: Sector;
  direction: 1 | -1 | 0;  // +확대 / -축소 / 0중립화(분산)
  rawMagnitude: number;   // %p 기준 기본 이동폭 (clamp 전)
  confidence: Confidence;
  sourceQuote: string;    // 어느 문장에서 나왔는지 (§9 추적)
}

export interface ParsedConcept {
  raw: string;
  tilts: Tilt[];
  cashTargetPct: number | null;  // '현금 30%' 같은 명시 레벨
  hedgeIntent: boolean;          // '숏/헷지' 언급
  ceoHedgeBoost: boolean;        // '헷지 강화' 명시
  notes: string[];
}

// 시장 신호 (mock, 헷지 임계점 판단용)
export interface MarketSignals {
  overheating: number;   // 과열도 0~100
  crashRisk: number;     // 급락위험 0~100
  eventRisk: number;     // 이벤트리스크 0~100
}

export interface HedgeDecision {
  hedgeScore: number;            // 0~100 합성
  warranted: boolean;            // 임계점 초과 여부
  strong: boolean;               // 강하게 충족 (10%까지 제안 근거)
  proposedShortPct: number;      // 0 | 5 | 10
  requiresCeoApproval: boolean;  // 5% 초과(=10%)면 true
  reason: string;
}

// 숏 포지션 명세 (CEO 요구: 진입/유지/축소/종료/최대보유)
export interface ShortPlan {
  active: boolean;
  pct: number;
  entryReason: string;
  maintainCondition: string;
  reduceCondition: string;
  exitCondition: string;
  maxHoldDays: number;
  status: "proposed" | "reduce_candidate" | "none";
}

export interface DriftLine {
  key: string;
  currentPct: number;
  targetPct: number;
  drift: number;            // current - target
  effectiveBand: number;    // min(절대5, target*0.25)
  exceeds: boolean;
  action: "매수" | "매도" | "유지";
  toEdgePct: number;        // band-edge 복귀 시 거래량(%)
}

export interface OrderCandidate {
  key: string;
  side: "buy" | "sell";
  pct: number;              // 총자산 대비 거래 비중
  valueKrw: number;
  splitInto: number;        // single_order_max 초과 시 분할 수
  rationale: string;
  status: "approval_pending" | "blocked_restricted"; // 승인 대기 or 매매제한 차단
  blockReason?: string;     // 매매제한 차단 사유
}

export interface Violation {
  limit: string;
  observed: number;
  threshold: number;
  detail: string;
  hard: boolean;
}

export interface RiskResult {
  passed: boolean;
  violations: Violation[];
  checked: { name: string; observed: number; threshold: number; ok: boolean }[];
}

export interface LessonCandidate {
  stage: "reflection";
  title: string;
  body: string;
  promotable: boolean;      // 일회성 vs 승격자격
  trigger: string;
}

// 한 변형안(보수/기준/공격) 전체 결과
export interface VariantResult {
  id: VariantId;
  label: string;
  scale: number;
  target: Weights;
  cashPct: number;
  maxSingleName: number;
  shortPct: number;
  leveragePct: number;
  drift: DriftLine[];
  orders: OrderCandidate[];
  risk: RiskResult;
  clamped: boolean;         // tilt 상한에 걸려 축소됨
}

// ── 멀티유저 (개인별 포트폴리오) ──────────────────────
export interface RestrictedInstrument {
  key: string;            // instrument key (예: samsung_sdi)
  reason: string;         // 'esop_lockup' | 'blackout' | 'insider' | 'user_set'
  label: string;          // 사람이 읽는 사유
  from: string;           // YYYY-MM-DD
  until: string | null;   // YYYY-MM-DD | null(무기한)
  scope: "all" | "buy" | "sell";
  hard: boolean;          // true=법적 제약, CEO 승인으로도 우회 불가
}

// 1 user = 1 프로필. anchor/concept/style/제약을 사람마다 분리 유지.
export interface InvestorProfile {
  id: string;
  name: string;
  persona: string;        // '성장형' '안정 배당형' '글로벌 공격형'
  preset: string;         // 핀트식 컨셉 프리셋 이름
  anchor: Weights;        // 개인별 CEO 기본배분
  current: Weights;       // 현재 보유 비중
  defaultConcept: string;
  riskTolerance: "conservative" | "moderate" | "aggressive";
  limitsOverride: { cashMinPct?: number; singleNameMaxPct?: number; fxHedgeRatio?: number };
  restricted: RestrictedInstrument[];
  signals: MarketSignals;
  totalValueKrw: number;
  drawdownPct: number;
}

export interface DecisionRun {
  profile: InvestorProfile;
  concept: ParsedConcept;
  anchorSource: "ceo_base" | "risk_parity_fallback";
  anchor: Weights;
  hedge: HedgeDecision;
  short: ShortPlan;
  variants: VariantResult[];
  selectedVariant: VariantId;
  lessons: LessonCandidate[];
  totalValueKrw: number;
}

// 후보 평가 공통 스키마(SSOT) — 백엔드 candidate.py CandidateEvaluation 와 1:1.
// 종목/ETF/국채/인버스 후보를 동일 구조로 비교 UI 가 소비한다(additive — 기존 필드 무변경).
// 안전 불변식: approval_required=true · auto_order_created=false · auto_applied=false.
// 미정 비중은 null(가짜 숫자 금지).
export interface CandidateEvaluation {
  candidate_type: "etf" | "stock" | "treasury" | "inverse" | string;
  candidate_id: string;
  display_name: string;
  bucket: string | null;
  fit_to_account: unknown;
  fit_to_allocation: unknown;
  data_quality: { available: boolean; level: string; [k: string]: unknown };
  confidence: number; // 0~1
  risk_summary: unknown;
  evidence_summary: unknown;
  suggested_weight: number | null;
  max_weight: number | null;
  reason_to_include: string;
  reason_to_exclude: string;
  approval_required: true;
  auto_order_created: false;
  auto_applied: false;
}
