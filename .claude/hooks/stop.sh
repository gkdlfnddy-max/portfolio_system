#!/usr/bin/env bash
# .claude/hooks/stop.sh
# Stop hook — Portfolio OS. 회고 + 안전 확인 (정적 안내).
set -e

cat <<'EOF'
=== Portfolio OS — session stop ===
회고 4질문:
  1) 무엇이 부족했는가?
  2) 어떤 리스크/기술 부채가 생겼는가?
  3) 다음에 자동화할 부분은?
  4) 어떤 노하우(lesson)가 누적되는가?

안전 확인: 실주문(live) 은 CEO 승인 + KIS_LIVE_CONFIRM 후에만. 자격증명은 .env 에만.
==================================
EOF
