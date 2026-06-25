"""작업 체크포인트(Agent 1 개선 3) 테스트 — 중단 후 안전 재개 구조.

검증:
  - save→load 라운드트립, 7 표준 필드
  - rollback_point 미지정 시 git HEAD 자동(또는 None graceful)
  - changed_files 리스트 보존, updated_at 주입 가능
  - 손상/부재 시 load()=None (graceful)
"""
from __future__ import annotations

import json
import os

from main_mission.portfolio_os import checkpoint as cp


def _tmp(tmp_path):
    os.environ["CHECKPOINT_PATH"] = str(tmp_path / "checkpoint.json")


def test_save_load_roundtrip(tmp_path):
    _tmp(tmp_path)
    rec = cp.save(phase_checkpoint="3-B", pending_subphase="개선 3",
                  resume_instruction="개선 3부터 재개",
                  last_successful_test="pytest test_candidate_eval (5 passed)",
                  changed_files=["candidate.py", "guards.py"],
                  rollback_point="abc1234", updated_at="2026-06-25T00:00:00+00:00")
    for k in cp.CHECKPOINT_FIELDS:
        assert k in rec, k
    loaded = cp.load()
    assert loaded == rec
    assert loaded["changed_files"] == ["candidate.py", "guards.py"]
    assert loaded["rollback_point"] == "abc1234"
    assert loaded["updated_at"] == "2026-06-25T00:00:00+00:00"


def test_rollback_point_defaults_to_git_or_none(tmp_path):
    _tmp(tmp_path)
    rec = cp.save(phase_checkpoint="p", pending_subphase="next", resume_instruction="go")
    # git 저장소면 짧은 해시(str), 아니면 None — 어느 쪽이든 graceful.
    assert rec["rollback_point"] is None or isinstance(rec["rollback_point"], str)


def test_load_missing_is_none(tmp_path):
    os.environ["CHECKPOINT_PATH"] = str(tmp_path / "does_not_exist.json")
    assert cp.load() is None


def test_load_corrupt_is_none(tmp_path):
    p = tmp_path / "checkpoint.json"
    p.write_text("{ not json", encoding="utf-8")
    os.environ["CHECKPOINT_PATH"] = str(p)
    assert cp.load() is None


def test_file_is_valid_json(tmp_path):
    _tmp(tmp_path)
    cp.save(phase_checkpoint="p", pending_subphase="n", resume_instruction="r")
    with open(os.environ["CHECKPOINT_PATH"], encoding="utf-8") as f:
        assert isinstance(json.load(f), dict)


if __name__ == "__main__":
    import tempfile, pathlib
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(pathlib.Path(tempfile.mkdtemp()))
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} CHECKPOINT TESTS PASSED")
