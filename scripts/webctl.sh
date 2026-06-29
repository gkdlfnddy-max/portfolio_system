#!/usr/bin/env bash
# Portfolio OS 웹 서버 관리 — 세션 의존도 없이(setsid 로 새 세션 분리) 기동/정지/상태.
#   start  : web(Next.js, :5001) 를 controlling terminal 과 분리해 백그라운드 기동.
#            이 스크립트를 호출한 셸/세션이 종료돼도 프로세스는 init 으로 reparent 되어 생존.
#   stop   : pidfile 기준으로 정지.
#   status : 포트/프로세스/HTTP 상태.
#   restart: stop 후 start.
# 비밀값은 .env / web/.env.local 에만. (Next 가 cwd=web/ 에서 .env.local 자동 로드)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/web"
PORT=5001
PIDFILE="$ROOT/.run/web.pid"
LOG="$ROOT/.run/web.log"

mkdir -p "$ROOT/.run"

is_up() { curl -s -m 3 -o /dev/null -w "%{http_code}" "http://localhost:$PORT" 2>/dev/null | grep -q "^[23]"; }
port_pid() { ss -ltnp 2>/dev/null | grep ":$PORT " | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2; }

start() {
  if is_up; then echo "[webctl] already up on :$PORT"; return 0; fi
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[webctl] pidfile live ($(cat "$PIDFILE")) — 기다리는 중"; return 0; fi
  echo "[webctl] starting web (:$PORT) detached ..."
  # setsid: 새 세션 리더로 만들어 controlling terminal/부모 셸과 완전히 분리.
  # </dev/null + 로그 redirect 로 stdio 도 분리 → Claude 세션 종료와 무관하게 생존.
  # npm 래퍼 대신 next 바이너리를 직접 exec (detached npm 래퍼가 즉시 종료되는 문제 회피).
  setsid bash -c "cd '$WEB' && exec node_modules/.bin/next dev -p $PORT" </dev/null >>"$LOG" 2>&1 &
  local launcher=$!
  # npm → next 자식까지 떠서 포트가 LISTEN 될 때까지 대기.
  for _ in $(seq 1 60); do
    if is_up; then break; fi
    sleep 1
  done
  local pid; pid="$(port_pid || true)"
  if [ -n "${pid:-}" ]; then echo "$pid" >"$PIDFILE"; fi
  if is_up; then
    echo "[webctl] up ✓  http://localhost:$PORT  (pid=${pid:-?}, launcher=$launcher)"
  else
    echo "[webctl] FAILED to come up — 최근 로그:"; tail -20 "$LOG"; return 1
  fi
}

stop() {
  local pid; pid="$(port_pid || true)"
  [ -z "${pid:-}" ] && [ -f "$PIDFILE" ] && pid="$(cat "$PIDFILE")"
  if [ -z "${pid:-}" ]; then echo "[webctl] not running"; rm -f "$PIDFILE"; return 0; fi
  echo "[webctl] stopping pid=$pid (+children)"
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  sleep 2
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "[webctl] stopped"
}

status() {
  echo "[webctl] port :$PORT pid=$(port_pid || echo none)"
  echo "[webctl] http=$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://localhost:$PORT 2>/dev/null || echo DOWN)"
  [ -f "$PIDFILE" ] && echo "[webctl] pidfile=$(cat "$PIDFILE")" || echo "[webctl] no pidfile"
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  *) echo "usage: $0 {start|stop|restart|status}"; exit 2 ;;
esac
