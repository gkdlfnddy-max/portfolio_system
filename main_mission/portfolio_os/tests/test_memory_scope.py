"""CEO memory scope 지시 테스트 — agent_memories 통합 scoped 메모리.

원칙(CEO): "계좌별 실행은 분리, 전문 Agent 지식은 공통 성장, 최종 적용은 계좌별 정책 우선."
키 없이 임시 SQLite로 전 경로 검증. (Anthropic API 미사용)
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_memscope.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os.growth import memory, prehooks


def setup():
    store_db.init()


# ---- 계좌별 실행 분리 (account-scoped isolation) ----
def test_account_scope_isolated_across_accounts():
    memory.remember("account", "A 계좌 현금 상향", "계좌 A 정책 메모리", account_index=1,
                    agent_name="broker-chief", confidence=0.8)
    a_items = memory.recall_scoped("broker-chief", 1)
    assert any(i["title"] == "A 계좌 현금 상향" for i in a_items), a_items
    # 계좌 B(2)에서는 계좌 A 메모리가 보이지 않아야 한다 (실행 분리).
    b_items = memory.recall_scoped("broker-chief", 2)
    assert not any(i["title"] == "A 계좌 현금 상향" for i in b_items), b_items


# ---- 전문 Agent 지식 공통 성장 (agent-scoped reuse across accounts) ----
def test_agent_scope_promoted_reused_across_accounts():
    res = memory.remember("agent", "분할 진입이 슬리피지 줄임", "공통 Agent lesson",
                          agent_name="broker-chief", confidence=0.9, source="agent")
    assert res["account_index"] is None if "account_index" in res else True
    memory.promote_agent_memory(res["memory_id"])
    a = memory.recall_scoped("broker-chief", 1)
    b = memory.recall_scoped("broker-chief", 2)
    assert any(i["title"] == "분할 진입이 슬리피지 줄임" for i in a), a  # 계좌 A 재사용
    assert any(i["title"] == "분할 진입이 슬리피지 줄임" for i in b), b  # 계좌 B 재사용 (공통 성장 증거)
    # 미승격 agent 메모리는 공통 재사용 대상 아님.
    memory.remember("agent", "미승격 후보", "관찰만", agent_name="broker-chief", confidence=0.5)
    c = memory.recall_scoped("broker-chief", 3)
    assert not any(i["title"] == "미승격 후보" for i in c), c


# ---- 우선순위 (account > user > agent) ----
def test_priority_order_account_then_user_then_agent():
    acc = 7
    memory.remember("user", "CEO 방어적 성향", "공통 성향", agent_name="broker-chief", confidence=0.6)
    ag = memory.remember("agent", "리밸런싱 분할 권장", "공통 lesson", agent_name="broker-chief", confidence=0.9)
    memory.promote_agent_memory(ag["memory_id"])
    memory.remember("account", "7번 계좌 전용 메모", "계좌 정책", account_index=acc,
                    agent_name="broker-chief", confidence=0.5)
    items = memory.recall_scoped("broker-chief", acc)
    order = [i["scope_type"] for i in items]
    i_acc = order.index("account")
    i_user = order.index("user")
    i_agent = order.index("agent")
    assert i_acc < i_user < i_agent, order


# ---- conflict A: 현금밴드 하한 (account policy wins, clamp) ----
def test_conflict_cash_band_account_policy_wins():
    policy = {"cash_band": {"min": 40, "max": 60}, "forbidden_assets": []}
    items = [{"scope_type": "agent", "title": "공격 전환", "body": "현금 20% 로 낮추자",
              "source_label": "공통 Agent lesson"}]
    kept, conflicts = memory.resolve_conflicts(items, policy)
    assert len(conflicts) == 1 and conflicts[0]["resolution"] == "account_policy_wins", conflicts
    assert "cash_band.min=40" in conflicts[0]["policy_rule"], conflicts
    # clamp 되어 정책값으로 보정(드롭 아님).
    assert kept and kept[0].get("clamped_cash_pct") == 40, kept


# ---- conflict B: 테마 불허 정책 → agent theme-tilt 억제 ----
def test_conflict_themes_forbidden_suppresses_theme_tilt():
    policy = {"cash_band": {"min": 10}, "forbidden_assets": ["themes"]}
    items = [
        {"scope_type": "agent", "title": "AI 테마 비중확대", "theme": "AI", "body": "tilt"},
        {"scope_type": "user", "title": "현금 여유 유지", "body": "테마 무관"},
    ]
    kept, conflicts = memory.resolve_conflicts(items, policy)
    titles = [k["title"] for k in kept]
    assert "AI 테마 비중확대" not in titles, kept  # 테마 tilt 억제
    assert "현금 여유 유지" in titles, kept        # 테마와 무관한 항목은 유지
    assert any(c["policy_rule"] == "themes_forbidden" for c in conflicts), conflicts


# ---- explain_sources ----
def test_explain_sources_mentions_present_scopes():
    items = [
        {"scope_type": "agent", "title": "x"},
        {"scope_type": "account", "title": "y", "clamped_cash_pct": 40},
    ]
    s = memory.explain_sources(items, account_index=3, selected_allocation_id=12)
    assert "공통 Agent lesson" in s, s
    assert "3번 계좌 정책" in s, s
    assert "현금밴드" in s, s
    assert "최근 선택 allocation" in s, s


# ---- prehook hard-block: account_index None 인 decision ----
def test_prehook_hard_block_when_account_none_for_decision():
    pre = prehooks.prepare("broker-chief", "decision", account_index=None)
    assert pre["gate"] == "block", pre
    assert any("account_id 없음" in r for r in pre["reasons"]), pre["reasons"]


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} MEMORY-SCOPE TESTS PASSED")
