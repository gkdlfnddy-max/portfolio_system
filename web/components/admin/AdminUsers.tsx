"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { Users, KeyRound, ShieldOff, ShieldCheck, Plus, Minus } from "lucide-react";

// 관리자 — 사용자 목록 / 계좌 권한 부여·회수 / 비밀번호 초기화 / 비활성화.
// 모든 호출은 서버 admin authz(A 소유)가 실제 차단. UI 는 표시·조작 보조일 뿐이다.
type AccountAccess = { account_index: number; access_role: string };
type AdminUser = {
  uid: string;
  email: string;
  display_name: string | null;
  is_admin?: boolean;
  is_active?: boolean;
  created_at?: string | null;
  accounts?: AccountAccess[];
};

const ROLES = ["viewer", "operator", "owner"];

function roleLabel(role: string): string {
  const map: Record<string, string> = {
    viewer: "조회",
    operator: "운영",
    owner: "소유",
    admin: "관리자",
  };
  return map[role] ?? role;
}

export function AdminUsers() {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busyUid, setBusyUid] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // 권한 부여 입력 상태(사용자별).
  const [grantInput, setGrantInput] = useState<Record<string, { idx: string; role: string }>>({});

  const load = useCallback(async () => {
    setErr(null);
    try {
      const res = await fetch("/api/admin/users", { cache: "no-store" });
      if (res.status === 403) {
        setForbidden(true);
        setUsers([]);
        return;
      }
      const j = await res.json().catch(() => ({}));
      if (res.ok && (j.ok ?? true)) {
        setUsers(Array.isArray(j.users) ? j.users : []);
      } else {
        setErr(j.error ?? "사용자 목록을 불러오지 못했습니다.");
        setUsers([]);
      }
    } catch {
      setErr("네트워크 오류가 발생했습니다.");
      setUsers([]);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const post = useCallback(
    async (url: string, body?: unknown, method = "POST"): Promise<{ ok: boolean; error?: string; data?: any }> => {
      try {
        const res = await fetch(url, {
          method,
          headers: { "Content-Type": "application/json" },
          body: body ? JSON.stringify(body) : undefined,
        });
        const j = await res.json().catch(() => ({ ok: false }));
        return { ok: res.ok && (j.ok ?? false), error: j.error, data: j };
      } catch {
        return { ok: false, error: "네트워크 오류" };
      }
    },
    [],
  );

  async function grant(uid: string) {
    const input = grantInput[uid] ?? { idx: "", role: "viewer" };
    const accountIndex = parseInt(input.idx, 10);
    if (!Number.isInteger(accountIndex) || accountIndex < 1) {
      setNotice("계좌 번호를 올바르게 입력하세요.");
      return;
    }
    setBusyUid(uid);
    setNotice(null);
    const r = await post(`/api/admin/users/${uid}/access`, {
      account_index: accountIndex,
      role: input.role || "viewer",
    });
    setBusyUid(null);
    if (r.ok) {
      setNotice("계좌 권한을 부여했습니다.");
      await load();
    } else {
      setNotice(r.error ?? "권한 부여 실패");
    }
  }

  async function revoke(uid: string, accountIndex: number) {
    setBusyUid(uid);
    setNotice(null);
    // 회수 = user_account_access row 삭제(서버 계약: DELETE /access {account_index}).
    const r = await post(`/api/admin/users/${uid}/access`, { account_index: accountIndex }, "DELETE");
    setBusyUid(null);
    if (r.ok) {
      setNotice("계좌 권한을 회수했습니다.");
      await load();
    } else {
      setNotice(r.error ?? "권한 회수 실패");
    }
  }

  async function resetPassword(uid: string, email: string) {
    if (!window.confirm(`${email} 사용자의 비밀번호를 초기화하시겠습니까?`)) return;
    setBusyUid(uid);
    setNotice(null);
    const r = await post(`/api/admin/users/${uid}`, { action: "reset_pw" }, "PATCH");
    setBusyUid(null);
    if (r.ok) {
      const tmp = r.data?.temp_password;
      setNotice(tmp ? `임시 비밀번호(1회용, 첫 로그인 시 변경 필수): ${tmp}` : "비밀번호를 초기화했습니다.");
      await load();
    } else setNotice(r.error ?? "초기화 실패");
  }

  async function setActive(uid: string, email: string, active: boolean) {
    const verb = active ? "활성화" : "비활성화";
    if (!window.confirm(`${email} 사용자를 ${verb}하시겠습니까?`)) return;
    setBusyUid(uid);
    setNotice(null);
    // 비활성/활성 토글 — 서버 계약: PATCH /api/admin/users/[uid] {action:"disable"|"enable"}.
    const r = await post(`/api/admin/users/${uid}`, { action: active ? "enable" : "disable" }, "PATCH");
    setBusyUid(null);
    if (r.ok) {
      setNotice(`사용자를 ${verb}했습니다.`);
      await load();
    } else {
      setNotice(r.error ?? `${verb} 실패`);
    }
  }

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Users className="w-5 h-5 text-primary" /> 사용자 관리
        </CardTitle>
        <Button variant="outline" size="sm" onClick={load}>
          새로고침
        </Button>
      </CardHeader>
      <CardBody className="space-y-4">
        {notice && <div className="text-sm text-primary">{notice}</div>}
        {forbidden && (
          <div className="text-sm text-error">
            관리자 권한이 없습니다. 이 화면의 데이터는 서버에서 차단됩니다.
          </div>
        )}
        {err && <div className="text-sm text-error">{err}</div>}

        {users === null ? (
          <div className="text-sm text-neutral-400">불러오는 중…</div>
        ) : users.length === 0 && !forbidden && !err ? (
          <div className="text-sm text-neutral-400">사용자가 없습니다.</div>
        ) : (
          <div className="space-y-4">
            {users.map((u) => {
              const gi = grantInput[u.uid] ?? { idx: "", role: "viewer" };
              const active = u.is_active !== false;
              return (
                <div key={u.uid} className="rounded-xl border border-neutral-100 p-4 space-y-3">
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div>
                      <div className="font-semibold text-neutral-900 flex items-center gap-2">
                        {u.display_name || u.email}
                        {u.is_admin && (
                          <Badge className="bg-primary-50 text-primary-700">관리자</Badge>
                        )}
                        {!active && <Badge className="bg-error/10 text-error">비활성</Badge>}
                      </div>
                      <div className="text-xs text-neutral-400">{u.email}</div>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busyUid === u.uid}
                        onClick={() => resetPassword(u.uid, u.email)}
                      >
                        <KeyRound className="w-4 h-4" /> 비밀번호 초기화
                      </Button>
                      {active ? (
                        <Button
                          variant="danger"
                          size="sm"
                          disabled={busyUid === u.uid}
                          onClick={() => setActive(u.uid, u.email, false)}
                        >
                          <ShieldOff className="w-4 h-4" /> 비활성화
                        </Button>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busyUid === u.uid}
                          onClick={() => setActive(u.uid, u.email, true)}
                        >
                          <ShieldCheck className="w-4 h-4" /> 활성화
                        </Button>
                      )}
                    </div>
                  </div>

                  {/* 계좌 권한 목록 */}
                  <div className="flex flex-wrap gap-2">
                    {(u.accounts ?? []).length === 0 ? (
                      <span className="text-xs text-neutral-400">부여된 계좌 권한 없음</span>
                    ) : (
                      (u.accounts ?? []).map((acc) => (
                        <span
                          key={acc.account_index}
                          className="inline-flex items-center gap-1 text-xs bg-neutral-100 text-neutral-700 rounded-full pl-2 pr-1 py-0.5"
                        >
                          계좌 #{acc.account_index} · {roleLabel(acc.access_role)}
                          <button
                            type="button"
                            disabled={busyUid === u.uid}
                            onClick={() => revoke(u.uid, acc.account_index)}
                            className="hover:text-error rounded-full p-0.5"
                            aria-label="권한 회수"
                          >
                            <Minus className="w-3 h-3" />
                          </button>
                        </span>
                      ))
                    )}
                  </div>

                  {/* 권한 부여 입력 */}
                  <div className="flex items-end gap-2 flex-wrap">
                    <div>
                      <label className="block text-xs text-neutral-500 mb-1">계좌 번호</label>
                      <Input
                        type="number"
                        min={1}
                        className="w-28"
                        placeholder="예: 1"
                        value={gi.idx}
                        onChange={(e) =>
                          setGrantInput((s) => ({
                            ...s,
                            [u.uid]: { ...gi, idx: e.target.value },
                          }))
                        }
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-neutral-500 mb-1">권한</label>
                      <select
                        className="px-3 py-2 rounded-xl border border-neutral-200 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary"
                        value={gi.role}
                        onChange={(e) =>
                          setGrantInput((s) => ({
                            ...s,
                            [u.uid]: { ...gi, role: e.target.value },
                          }))
                        }
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {roleLabel(r)}
                          </option>
                        ))}
                      </select>
                    </div>
                    <Button
                      size="sm"
                      disabled={busyUid === u.uid}
                      onClick={() => grant(u.uid)}
                    >
                      <Plus className="w-4 h-4" /> 계좌 권한 부여
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardBody>
    </Card>
  );
}
