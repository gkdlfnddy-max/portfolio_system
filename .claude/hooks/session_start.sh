#!/usr/bin/env bash
# .claude/hooks/session_start.sh
# SessionStart hook — Portfolio OS. Plan-First 트리거 (정적 안내).
set -e

cat <<'EOF'
=== Portfolio OS — session start ===
1) CLAUDE.md 를 가장 먼저 읽으세요 (500줄 이하 유지).
2) role 확인: agents/portfolio/broker-chief.md (포트폴리오 관리자 = 단일 agent).
3) 안전 규칙: docs/portfolio/safety_rules.md (모의투자 우선 · 사람 승인 기본값).
4) KIS 연결 상태: python -m main_mission.portfolio_os.broker.kis_check
5) 현재 모드: .env 의 KIS_MODE (mock|paper|live). live 는 KIS_LIVE_CONFIRM 필요.
==================================
EOF
