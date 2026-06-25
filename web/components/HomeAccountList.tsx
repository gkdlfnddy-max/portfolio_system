"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Card, CardBody } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { BrokerBadge } from "@/components/BrokerBadge";
import { ArrowRight, ShieldCheck, Plus, Wallet } from "lucide-react";

// 홈 계좌 목록(클라이언트) — GET /api/accounts 는 서버에서 로그인/권한으로 *이미 필터된* 내 계좌만 반환.
// 라벨: admin 이면 "전체 계좌", 일반 사용자는 "내 계좌".
// 프론트는 표시만 — 실제 접근 차단(authz)은 서버(A 소유)가 한다. 프론트 숨김에 의존하지 않는다.
type AccountRow = {
  account_index: number;
  alias: string | null;
  mode: string | null;
  account_no_masked: string | null;
  sync_status: string | null;
  last_synced_at: string | null;
  broker?: string | null; // kis | kiwoom (서버 제공, 없으면 'kis' 기본)
};

// 증권사 표시명 — 카드 본문 텍스트용(배지는 BrokerBadge).
function brokerName(broker: string | null | undefined): string {
  return String(broker ?? "kis").toLowerCase() === "kiwoom" ? "키움증권" : "한국투자증권";
}

type AccountsResponse = {
  ok?: boolean;
  accounts?: AccountRow[];
  is_admin?: boolean;
  error?: string;
};

function ModeBadge({ mode }: { mode: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    mock: { label: "데모(mock)", cls: "bg-neutral-100 text-neutral-600" },
    paper: { label: "모의투자", cls: "bg-primary-50 text-primary-700" },
    live: { label: "실전", cls: "bg-error/10 text-error" },
  };
  const m = map[mode] ?? map.paper;
  return <Badge className={m.cls}>{m.label}</Badge>;
}

function SyncBadge({ status }: { status: string | null }) {
  if (status === "ok")
    return (
      <span className="text-xs flex items-center gap-1 text-success">
        <ShieldCheck className="w-3.5 h-3.5" /> 동기화됨
      </span>
    );
  if (status === "error") return <span className="text-xs text-error">동기화 오류</span>;
  return <span className="text-xs text-neutral-400">미동기화</span>;
}

function AccountCard({ a }: { a: AccountRow }) {
  return (
    <Link href={`/accounts/${a.account_index}`} className="block">
      <Card className="hover:border-primary-100 transition">
        <CardBody className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold text-neutral-900">{a.alias}</span>
            <div className="flex items-center gap-1.5 shrink-0">
              <BrokerBadge broker={a.broker} />
              <ModeBadge mode={a.mode ?? "paper"} />
            </div>
          </div>
          <div className="flex items-center gap-2 text-sm text-neutral-600">
            <Wallet className="w-4 h-4 text-neutral-400" />
            {brokerName(a.broker)} · 계좌 {a.account_no_masked ?? "—"}
          </div>
          <div className="flex items-center justify-between pt-2 border-t border-neutral-100">
            <SyncBadge status={a.sync_status} />
            <span className="text-xs text-primary flex items-center gap-1">
              관리 화면 <ArrowRight className="w-3 h-3" />
            </span>
          </div>
          <p className="text-[11px] text-neutral-400">
            {a.last_synced_at
              ? `마지막 동기화: ${new Date(a.last_synced_at).toLocaleString("ko-KR")}`
              : "계좌 화면에서 동기화하면 잔고가 채워집니다."}
          </p>
        </CardBody>
      </Card>
    </Link>
  );
}

export function HomeAccountList() {
  const [accounts, setAccounts] = useState<AccountRow[] | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch("/api/accounts", { cache: "no-store" });
        const j: AccountsResponse = await res.json().catch(() => ({}));
        if (!alive) return;
        if (res.ok) {
          setAccounts(Array.isArray(j.accounts) ? j.accounts : []);
          setIsAdmin(j.is_admin === true);
        } else {
          // 401 은 LoginGate 가 /login 으로 보낸다. 여기 도달하면 일반 오류 처리.
          setErr(true);
          setAccounts([]);
        }
      } catch {
        if (alive) {
          setErr(true);
          setAccounts([]);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // admin 이면 "전체 계좌", 일반 사용자는 "내 계좌".
  const title = isAdmin ? "전체 계좌" : "내 계좌";
  const subtitle = isAdmin
    ? "모든 사용자의 계좌입니다. 카드를 누르면 의사결정 화면으로 이동합니다."
    : "내가 접근 권한을 가진 계좌입니다. 카드를 누르면 의사결정 화면으로 이동합니다.";

  return (
    <section>
      <div className="flex items-end justify-between mb-4">
        <div>
          <h2 className="text-2xl font-bold text-neutral-900">{title}</h2>
          <p className="text-sm text-neutral-400 mt-1">{subtitle}</p>
        </div>
        <Link href="/accounts/new" className="text-sm text-primary hover:underline whitespace-nowrap">
          + 계좌 추가
        </Link>
      </div>

      {accounts === null ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {[0, 1].map((i) => (
            <Card key={i}>
              <CardBody>
                <div className="h-24 animate-pulse rounded-lg bg-neutral-100" />
              </CardBody>
            </Card>
          ))}
        </div>
      ) : err ? (
        <Card className="border-dashed">
          <CardBody className="text-center py-10 text-sm text-neutral-500">
            계좌 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.
          </CardBody>
        </Card>
      ) : accounts.length === 0 ? (
        <Card className="border-dashed">
          <CardBody className="text-center py-10 space-y-3">
            <div className="text-4xl">🪙</div>
            {isAdmin ? (
              <p className="text-neutral-600">
                아직 연결된 계좌가 없습니다. 한국투자증권 계좌를 연결하면
                여기에서 모든 사용자의 계좌를 관리할 수 있습니다.
              </p>
            ) : (
              <p className="text-neutral-600">
                아직 접근 권한이 있는 계좌가 없습니다. 직접 한국투자증권 계좌를
                연결하거나, 관리자에게 기존 계좌의 접근 권한을 요청하세요.
              </p>
            )}
            <Link href="/accounts/new">
              <Button>
                <Plus className="w-4 h-4" /> 계좌 연결하기
              </Button>
            </Link>
          </CardBody>
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {accounts.map((a) => (
            <AccountCard key={a.account_index} a={a} />
          ))}
        </div>
      )}

      <p className="text-xs text-neutral-400 mt-4">
        입력한 정보는 이 PC의 <code>.env</code> 에만 계좌별로 저장됩니다. APP Key 발급:
        docs/portfolio/kis_onboarding.md
      </p>
    </section>
  );
}
