"""작업 체크포인트 — API 529 등 중단 후 **안전 재개** 구조 (Agent 1 개선 3).

git 커밋이 이미 rollback_point 로 운영된다(sub-phase 마다 commit+push). 이 모듈은 그 위에
"마지막으로 검증된 지점"과 "다음에 할 일"을 기록해, 중단(예: Claude API 529) 후 처음부터
다시 꼬이지 않게 한다.

529(서버 과부하) 발생 시 원칙(이 모듈이 강제하는 정신):
  - 코드 실패로 단정하지 않는다.
  - 완료된 Phase 를 재수행하지 않는다.
  - 마지막 성공 체크포인트에서 재개한다.
  - 중복 패치를 만들지 않는다.

체크포인트 구조(JSON, CHECKPOINT_FIELDS):
  phase_checkpoint      현재/마지막 완료 phase 라벨
  last_successful_test  마지막으로 통과한 테스트 명령/요약
  changed_files         이번 phase 에서 바뀐 파일 목록
  pending_subphase      다음에 할 sub-phase(여기서 재개)
  resume_instruction    재개 지시(사람/에이전트가 읽는 한 줄)
  rollback_point        되돌아갈 git 커밋(기본 = 현재 HEAD)
  updated_at            기록 시각(ISO; 미지정 시 now)

저장 위치: data/checkpoint.json (data/ 는 .gitignore — 로컬 작업 상태이며 발행하지 않는다).
rollback_point 는 git 커밋 해시이므로 원격에 이미 보존된다.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

CHECKPOINT_FIELDS: tuple[str, ...] = (
    "phase_checkpoint", "last_successful_test", "changed_files",
    "pending_subphase", "resume_instruction", "rollback_point", "updated_at",
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _path() -> str:
    """체크포인트 파일 경로(환경변수 CHECKPOINT_PATH 로 override 가능 — 테스트 격리)."""
    return os.environ.get("CHECKPOINT_PATH") or os.path.join(_PROJECT_ROOT, "data", "checkpoint.json")


def git_head() -> str | None:
    """현재 git 커밋(짧은 해시). git 저장소가 아니면 None — graceful."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def save(*, phase_checkpoint: str, pending_subphase: str, resume_instruction: str,
         last_successful_test: str = "", changed_files: list[str] | None = None,
         rollback_point: str | None = None, updated_at: str | None = None) -> dict:
    """체크포인트 저장. rollback_point 미지정 시 현재 git HEAD 로 자동 채움.

    부수효과: data/checkpoint.json 쓰기. 주문/policy 변경 없음(작업 메타만).
    """
    record = {
        "phase_checkpoint": phase_checkpoint,
        "last_successful_test": last_successful_test,
        "changed_files": list(changed_files or []),
        "pending_subphase": pending_subphase,
        "resume_instruction": resume_instruction,
        "rollback_point": rollback_point if rollback_point is not None else git_head(),
        "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
    }
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def load() -> dict | None:
    """체크포인트 읽기. 없거나 손상 시 None — graceful(재개 불가가 아니라 '새로 시작')."""
    path = _path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
