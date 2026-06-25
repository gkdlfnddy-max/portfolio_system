"""agent-scope 승격 익명화 테스트 — 개인/계좌 식별정보가 promoted lesson 에 안 섞임.

원칙(CEO): 전문 Agent 는 공통 성장(agent-scoped promoted lesson)하되,
  개인/계좌 식별 정보는 promoted lesson 에 섞이면 안 된다.
  account-scoped memory 는 다른 사용자/계좌에 노출 금지(교차 격리).
키 없이 임시 SQLite 로 전 경로 검증. (Anthropic API 미사용)
"""
from __future__ import annotations

import os
import re
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_mem_anon.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os.growth import memory


def setup():
    store_db.init()


# 식별정보 정규식 — 승격본에 절대 남으면 안 되는 패턴.
_LEAK_PATTERNS = [
    re.compile(r"\b\d{6,}\b"),                 # 계좌번호(6자리+)
    re.compile(r"\d[\d,]*\s*주\b"),            # 보유 수량
    re.compile(r"\d[\d,]*\s*(?:원|만원|억|KRW)"),  # 금액
    re.compile(r"@"),                          # email
    re.compile(r"\buser[_-]?[A-Za-z0-9]+\b", re.IGNORECASE),  # user_id/별칭
    re.compile(r"\d+\s*번\s*계좌"),            # N번 계좌
]


def _assert_no_identifiers(text: str):
    for pat in _LEAK_PATTERNS:
        assert not pat.search(text), f"식별정보 누출: {pat.pattern!r} in {text!r}"


# ---- ① 식별정보 포함 memory 를 promote → 승격본에 식별정보 0 ----
def test_promote_strips_all_identifiers():
    raw = ("user_a의 1번 계좌(110-22-334455)는 삼성전자 500주 보유, 평가금액 12,000,000원. "
           "문의 gkdlfnddy@gmail.com. 반도체 hedge 원함.")
    m = memory.remember("agent", "user_a 1번 계좌 삼성전자 500주 hedge",
                        raw, agent_name="broker-chief", confidence=0.9)
    res = memory.promote_agent_memory(m["memory_id"])
    assert res["ok"], res
    _assert_no_identifiers(res["title"])
    _assert_no_identifiers(res["body"])
    # DB 에 실제 저장된 승격본도 클린.
    conn = store_db.connect()
    row = conn.execute("SELECT title, body, account_index, scope_id, promoted "
                       "FROM agent_memories WHERE id=?", (m["memory_id"],)).fetchone()
    conn.close()
    _assert_no_identifiers(row["title"])
    _assert_no_identifiers(row["body"])
    assert row["promoted"] == 1, row["promoted"]
    assert row["account_index"] is None, row["account_index"]
    assert row["scope_id"] is None, row["scope_id"]


# ---- ② 일반 투자원칙은 보존 ----
def test_general_principle_preserved():
    raw = "반도체 hedge: 롱/숏 분리해 net/gross 노출을 계산하면 슬리피지 줄어듦."
    m = memory.remember("agent", "반도체 hedge net/gross 분리 원칙",
                        raw, agent_name="broker-chief", confidence=0.9)
    res = memory.promote_agent_memory(m["memory_id"])
    body = res["body"]
    # 식별정보 없으니 원칙 텍스트는 그대로 살아남아야 한다.
    for keep in ("반도체", "hedge", "net/gross", "슬리피지"):
        assert keep in body, (keep, body)
    _assert_no_identifiers(body)


# ---- ③ account-scoped(계좌1)은 계좌2 recall 에 안 나옴 (교차 격리) ----
def test_account_scoped_not_leaked_cross_account():
    memory.remember("account", "계좌1 전용 정책", "삼성전자 500주 보유 현금 30%",
                    account_index=1, agent_name="broker-chief", confidence=0.8)
    a1 = memory.recall_scoped("broker-chief", 1)
    a2 = memory.recall_scoped("broker-chief", 2)
    assert any(i["title"] == "계좌1 전용 정책" for i in a1), a1
    assert not any(i["title"] == "계좌1 전용 정책" for i in a2), a2
    # 어떤 항목에도 계좌1 식별 body 가 계좌2 결과에 없어야 함.
    for it in a2:
        assert "500주" not in (it.get("body") or ""), it


# ---- ④ agent-scoped promoted 는 account 무관 공통 노출(일반화됨) ----
def test_promoted_agent_memory_common_across_accounts_and_clean():
    raw = "user_b 3번 계좌 1,500주 익절 후 방어자산 비중 상향 — risk 패턴 반복."
    m = memory.remember("agent", "방어자산 비중 상향 risk 패턴",
                        raw, agent_name="broker-chief", confidence=0.9)
    memory.promote_agent_memory(m["memory_id"])
    seen = []
    for acc in (1, 2, 99):
        items = memory.recall_scoped("broker-chief", acc)
        hit = [i for i in items if i["title"] == "방어자산 비중 상향 risk 패턴"]
        assert hit, (acc, items)             # 계좌 무관 공통 노출(공통 성장)
        seen.append(hit[0])
    for it in seen:
        _assert_no_identifiers(it["body"])   # 일반화되어 식별정보 0
        assert "방어자산" in it["body"], it   # 일반 risk 패턴은 보존


# ---- ⑤ 익명화가 원본 account memory 는 건드리지 않음 ----
def test_promote_does_not_mutate_account_origin():
    origin = memory.remember("account", "계좌5 원본", "user_c 5번 계좌 700주 보유 12,345,678원",
                             account_index=5, agent_name="broker-chief", confidence=0.7)
    # agent-scope 승격본 별도 적재 후 승격.
    promo = memory.remember("agent", "공통 패턴", "user_c 5번 계좌 700주 익절 패턴",
                            agent_name="broker-chief", confidence=0.9)
    memory.promote_agent_memory(promo["memory_id"])
    conn = store_db.connect()
    row = conn.execute("SELECT title, body, account_index FROM agent_memories WHERE id=?",
                       (origin["memory_id"],)).fetchone()
    conn.close()
    # 원본은 식별정보 그대로(불변) — 익명화는 승격본에만 적용.
    assert row["body"] == "user_c 5번 계좌 700주 보유 12,345,678원", row["body"]
    assert row["account_index"] == 5, row["account_index"]


# ---- 보너스: _anonymize 단위 규칙 ----
def test_anonymize_unit_rules():
    out = memory._anonymize("user_a의 1번 계좌는 삼성전자 500주 보유, 반도체 hedge 원함")
    assert "user_a" not in out and "500주" not in out and "1번 계좌" not in out, out
    assert "삼성전자" in out and "반도체" in out and "hedge" in out, out
    assert not memory.has_identifiers(out), out
    assert memory.has_identifiers("계좌 110-22-334455")
    assert not memory.has_identifiers("net/gross 노출 계산 원칙")


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} MEMORY-ANONYMIZE TESTS PASSED")
