"""growth — Portfolio OS 성장 스캐폴딩 (모든 Agent 공통 토대).

구성:
  registry  : Agent별 memory scope 선언 (prehook 검색 대상)
  memory    : scope/agent/freshness-가중 recall + negative feedback memory
  tasks     : 표준 task 상태머신 + provenance(prehook/posthook 결과 적재)
  prehooks  : 작업 전 안전 점검(정책·선택안·스냅샷 staleness) + 관련 memory 로드
  posthooks : 작업 후 정리(lesson candidate·next_action·unresolved_risk·feedback)

원칙(불변): Anthropic API 미사용. 지능은 Claude+메모리. 본 패키지는 메모리/안전/추적 토대만 제공.
"""
