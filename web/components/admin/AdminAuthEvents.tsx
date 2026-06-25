"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { ScrollText } from "lucide-react";

// 관리자 — 인증 이벤트(auth_events) 조회. 감사 로그(append-only)이며 PIN/비밀번호 평문은 포함하지 않는다.
type AuthEvent = {
  id: number | string;
  user_id?: string | null;
  email?: string | null;
  event_type: string;
  success: boolean;
  reason?: string | null;
  created_at: string;
};

export function AdminAuthEvents() {
  const [events, setEvents] = useState<AuthEvent[] | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const res = await fetch("/api/admin/auth-events", { cache: "no-store" });
      if (res.status === 403) {
        setForbidden(true);
        setEvents([]);
        return;
      }
      const j = await res.json().catch(() => ({}));
      if (res.ok && (j.ok ?? true)) {
        setEvents(Array.isArray(j.events) ? j.events : []);
      } else {
        setErr(j.error ?? "인증 이벤트를 불러오지 못했습니다.");
        setEvents([]);
      }
    } catch {
      setErr("네트워크 오류가 발생했습니다.");
      setEvents([]);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <ScrollText className="w-5 h-5 text-primary" /> 인증 이벤트
        </CardTitle>
        <Button variant="outline" size="sm" onClick={load}>
          새로고침
        </Button>
      </CardHeader>
      <CardBody>
        {forbidden && (
          <div className="text-sm text-error">
            관리자 권한이 없습니다. 이 데이터는 서버에서 차단됩니다.
          </div>
        )}
        {err && <div className="text-sm text-error">{err}</div>}

        {events === null ? (
          <div className="text-sm text-neutral-400">불러오는 중…</div>
        ) : events.length === 0 && !forbidden && !err ? (
          <div className="text-sm text-neutral-400">기록된 이벤트가 없습니다.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-neutral-400 border-b border-neutral-100">
                  <th className="py-2 pr-3 font-medium">시각</th>
                  <th className="py-2 pr-3 font-medium">사용자</th>
                  <th className="py-2 pr-3 font-medium">이벤트</th>
                  <th className="py-2 pr-3 font-medium">결과</th>
                  <th className="py-2 font-medium">사유</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={String(e.id)} className="border-b border-neutral-50">
                    <td className="py-2 pr-3 text-neutral-500 whitespace-nowrap">
                      {new Date(e.created_at).toLocaleString("ko-KR")}
                    </td>
                    <td className="py-2 pr-3 text-neutral-700">
                      {e.email ?? e.user_id ?? "—"}
                    </td>
                    <td className="py-2 pr-3 text-neutral-700">{e.event_type}</td>
                    <td className="py-2 pr-3">
                      {e.success ? (
                        <Badge className="bg-success/10 text-success">성공</Badge>
                      ) : (
                        <Badge className="bg-error/10 text-error">실패</Badge>
                      )}
                    </td>
                    <td className="py-2 text-neutral-400">{e.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardBody>
    </Card>
  );
}
