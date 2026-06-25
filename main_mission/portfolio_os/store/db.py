"""SQLite 연결 + 스키마 초기화 (data/portfolio.sqlite3)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def db_path() -> Path:
    # os.environ 가 우선 (테스트에서 임시 경로 주입). load_dotenv 는 override=False.
    env = ROOT / ".env"
    if env.exists():
        if load_dotenv is not None:
            load_dotenv(env)
        else:
            from ..envfallback import load_env_file
            load_env_file(env)
    raw = os.getenv("SQLITE_PATH", "./data/portfolio.sqlite3")
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / raw)


_bootstrapped = False
# 부트스트랩한 DB 경로를 추적 — SQLITE_PATH 가 바뀌면(테스트 모듈 전환 등) 새 경로에
# 스키마를 다시 보장한다. (이전엔 프로세스당 1회만 부트스트랩 → 경로 변경 시 'no such table' 발생)
_bootstrapped_path: str | None = None

# 기존 테이블에 나중에 추가된 컬럼 (CREATE IF NOT EXISTS 로는 안 붙음) — 멱등 마이그레이션.
_ADD_COLUMNS = [
    ("investor_profile", "individual_cap_pct", "REAL"),
    ("investor_profile", "individual_count", "INTEGER"),
    ("investor_profile", "region_pref", "TEXT"),
    ("investor_profile", "rebalance_pace", "TEXT"),
    ("investor_profile", "doc", "TEXT"),  # 진화하는 자유 문서(JSON): 키워드/보완점/지역분배/Claude 노트
    ("investor_profile", "hedge_themes", "TEXT"),  # 인버스/헤지 의도 테마(롱 tilt와 분리)
    ("investor_profile", "region_targets", "TEXT"),       # JSON {지역:비중} 구조화
    ("investor_profile", "bond_target_pct", "REAL"),      # 채권 목표 비중(현금과 별도 방어자산)
    ("investor_profile", "bond_duration_pref", "TEXT"),   # short|intermediate|long|mixed
    ("investor_profile", "bond_allowed_types", "TEXT"),   # 허용 채권 유형(기본 government_only=국채만)
    ("investor_profile", "bond_duration_split", "TEXT"),  # mixed 듀레이션 분할 JSON {short, long} 합100
    ("rebalance_plan_steps", "role", "TEXT"),       # long | hedge | anchor | cash | bond
    # --- 성장 스캐폴딩: lessons/lesson_candidates 에 freshness/archive/agent 부여 ---
    ("lessons", "last_seen_at", "TEXT"),            # 마지막 참조 시각 (decay 기준)
    ("lessons", "status", "TEXT"),                  # active | archived (오래되면 archive)
    ("lessons", "agent", "TEXT"),                   # 적재한 agent slug (agent scope)
    ("lesson_candidates", "last_seen_at", "TEXT"),  # 마지막 관찰/참조 시각
    ("lesson_candidates", "agent", "TEXT"),         # 관찰한 agent slug
    # --- Dynamic Policy (유연 투자기준): 고정값 강요 금지, 계좌별 스타일 ---
    ("portfolio_policies", "policy_type", "TEXT"),          # single_stock_focus|etf_diversified|cash_defensive|growth_theme|dividend_income|custom
    ("portfolio_policies", "policy_template", "TEXT"),      # 시작 템플릿 id
    ("portfolio_policies", "user_overrides_json", "TEXT"),  # 사용자가 바꾼 기본값
    ("portfolio_policies", "disabled_rules_json", "TEXT"),  # 끈 규칙(단, hard rule은 못 끔)
    ("portfolio_policies", "custom_rules_json", "TEXT"),    # 사용자 직접 규칙
    ("portfolio_policies", "policy_notes_json", "TEXT"),    # 메모/근거
    # investor_profile 에도 동적 정책(스타일/override) — UI(profile.save)가 쓰는 진리.
    ("accounts", "broker", "TEXT"),                         # 멀티 브로커: kis|kiwoom|manual|paper (없으면 kis 취급)
    ("investor_profile", "policy_type", "TEXT"),            # 투자 스타일 template id
    ("investor_profile", "user_overrides_json", "TEXT"),   # 사용자 override(JSON)
    ("investor_profile", "disabled_rules_json", "TEXT"),   # 끈 규칙(hard rule 제외, JSON)
    ("investor_profile", "theme_directions_json", "TEXT"), # 테마별 방향 override {theme: long_candidate|short_or_hedge_candidate|watch_only|avoid_or_exclude|unknown_direction}
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, typ in _ADD_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # 이미 존재 (duplicate column) — 무시
    conn.commit()


def connect() -> sqlite3.Connection:
    global _bootstrapped, _bootstrapped_path
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 최초이거나 SQLITE_PATH 가 바뀐 경우 스키마 보장(IF NOT EXISTS — 멱등) + 컬럼 마이그레이션.
    # 경로별 추적으로 테스트 모듈 전환 시 'no such table' 교차오염을 방지.
    if not _bootstrapped or _bootstrapped_path != str(p):
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        _migrate(conn)
        _bootstrapped = True
        _bootstrapped_path = str(p)
    return conn


def init(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        _migrate(conn)  # 신규(테스트) DB에도 additive 컬럼 보장 — connect/init 경로 동일.
    finally:
        if own:
            conn.close()
